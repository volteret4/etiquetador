#!/usr/bin/env python3
"""Completa etiquetas de discos sin tocar nunca artista, álbum ni título.

  album RUTA...      sello original, género y ReplayGain de esos discos
  pendientes         lo mismo con los discos de temp aún no procesados
  mix RUTA...        BPM y ReplayGain de canciones recién añadidas a Mix
  ganancia RUTA...   solo ReplayGain de un árbol completo
  deshacer RUN_ID    restaura los FLAC de una ejecución

Sin --write no escribe nada: solo genera el informe en DATA_DIR.
"""
import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

import mutagen
import r128gain
import requests
from mutagen.flac import FLAC, VCFLACDict

DATA_DIR = os.environ.get('DATA_DIR', '/data')
TEMP_ROOTS = [p for p in os.environ.get(
    'TEMP_ROOTS', '/music/entrada').split(':') if p]
# Carpetas de canciones sueltas: su carpeta no es un álbum.
LOOSE_ROOTS = [p for p in os.environ.get('LOOSE_ROOTS', '').split(':') if p]
# Bandejas de entrada: sus subcarpetas son discos, pero lo suelto en su raíz se revisa
# a mano y se mueve o se borra enseguida, así que no se procesa.
INBOX_ROOTS = [p for p in os.environ.get('INBOX_ROOTS', '').split(':') if p]
DISCOGS_TOKEN = os.environ.get('DISCOGS_TOKEN', '')
UA = 'etiquetador/1.0 ( https://github.com/volteret4/etiquetador )'
MB = 'https://musicbrainz.org/ws/2'
DISCOGS = 'https://api.discogs.com'

AUDIO_EXTS = {'.flac', '.mp3', '.ogg', '.m4a', '.wv', '.ape', '.wav', '.wma', '.opus'}
RG_KEYS = ('REPLAYGAIN_TRACK_GAIN', 'REPLAYGAIN_TRACK_PEAK', 'REPLAYGAIN_ALBUM_GAIN', 'REPLAYGAIN_ALBUM_PEAK')
LABEL_KEYS = ('LABEL', 'ORGANIZATION', 'PUBLISHER')
OLD_LABEL_KEY = 'LABEL_ANTERIOR'
# Lista cerrada: write_flac rechaza cualquier otra etiqueta.
WRITABLE = frozenset({*LABEL_KEYS, OLD_LABEL_KEY, 'GENRE', 'BPM', *RG_KEYS})

DISC_DIR = re.compile(r'^(cd|dis[ck])\W*\d+', re.I)
EDITION = re.compile(r'\s*[\(\[][^\)\]]*(deluxe|remaster|edition|expanded|anniversary|bonus|reissue|special|'
                     r'version|mono|stereo|\d{4})[^\)\]]*[\)\]]\s*$', re.I)
LABEL_NOISE = {'records', 'record', 'recordings', 'recording', 'music', 'musique', 'label', 'ltd', 'inc', 'gmbh',
               'llc', 'limited', 'the', 'discs', 'disques', 'productions', 'entertainment'}
MAX_DURATION_DIFF = 4.0
MIN_DURATION_MATCH = 0.8
MIN_GENRE_USAGE = 0.9  # como Picard: géneros con al menos el 90 % de votos del primero
MAX_GENRES = 5
MIN_BPM_CONFIDENCE = 1.5  # Essentia: por debajo, «baja» o «muy baja»
RG_REFERENCE_LUFS = -18  # ReplayGain 2.0
SILENCE_LUFS = -60
FREE_MARGIN = 1024 ** 3
SETTLE_SECONDS = 600


class NoSpace(Exception):
    """No queda espacio para escribir: hay que parar toda la ejecución."""


class VerifyError(Exception):
    """Tras escribir, el fichero no quedó como debía."""


# ── Texto ─────────────────────────────────────────────────────────────────────
def norm(text):
    """Forma comparable: sin acentos, mayúsculas ni puntuación."""
    text = unicodedata.normalize('NFKD', text or '')
    text = ''.join(c for c in text if not unicodedata.combining(c)).casefold().replace('&', ' and ')
    return ' '.join(re.sub(r'[^\w]+', ' ', text).split())


def base_title(title):
    """Título sin coletillas de edición: «Wonderland (Special Edition)» → «wonderland»."""
    previous = None
    while title != previous:
        previous, title = title, EDITION.sub('', title or '')
    return re.sub(r'\s+ep$', '', norm(title))


def norm_label(name):
    """Sello comparable: «Mute Records» y «Mute (2)» → «mute»."""
    name = re.sub(r'\s*\(\d+\)\s*$', '', (name or '').rstrip('* '))
    words = [w for w in norm(name).split() if w not in LABEL_NOISE]
    return ' '.join(words) or norm(name)


# ── Diario y estado ───────────────────────────────────────────────────────────
class Run:
    def __init__(self, write, run_id=None):
        self.write = write
        self.id = run_id or time.strftime('%Y%m%d-%H%M%S')
        self.lock = threading.Lock()
        os.makedirs(DATA_DIR, exist_ok=True)

    def journal(self, entry):
        with self.lock, open(os.path.join(DATA_DIR, f'diario-{self.id}.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=repr) + '\n')


def load_state():
    try:
        with open(os.path.join(DATA_DIR, 'estado.json'), encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    path = os.path.join(DATA_DIR, 'estado.json')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ── Escritura segura de FLAC ──────────────────────────────────────────────────
def flac_audio_offset(path):
    """Byte donde empieza el audio, tras los bloques de metadatos."""
    with open(path, 'rb') as f:
        head = f.read(10)
        start = 0
        if head[:3] == b'ID3':
            start = 10 + ((head[6] & 127) << 21 | (head[7] & 127) << 14 | (head[8] & 127) << 7 | (head[9] & 127))
        f.seek(start)
        if f.read(4) != b'fLaC':
            raise VerifyError(f'no es un FLAC válido: {path}')
        while True:
            header = f.read(4)
            if len(header) < 4:
                raise VerifyError(f'FLAC truncado: {path}')
            f.seek(int.from_bytes(header[1:4], 'big'), os.SEEK_CUR)
            if header[0] & 0x80:
                return f.tell()


def audio_digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        f.seek(flac_audio_offset(path))
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def tag_snapshot(audio):
    tags = collections.defaultdict(list)
    for key, value in (audio.tags or []):
        tags[key.upper()].append(value)
    return {k: sorted(v) for k, v in tags.items()}


def picture_digest(audio):
    return [hashlib.sha256(p.data).hexdigest() for p in audio.pictures]


def apply_changes(tags, changes):
    for key, values in changes.items():
        if key in tags:
            del tags[key]
        if values:
            tags[key] = list(values)


def fits_in_padding(audio, changes):
    """¿Caben las etiquetas nuevas sin mover el audio?"""
    if audio.tags is None:
        return False
    padding = next((b.length for b in audio.metadata_blocks if b.code == 1), 0)
    old = len(audio.tags.write(framing=False))
    probe = VCFLACDict()
    probe.vendor = audio.tags.vendor
    for key, value in audio.tags:
        probe.append((key, value))
    apply_changes(probe, changes)
    return len(probe.write(framing=False)) <= old + padding


def ensure_space(path, needed):
    if shutil.disk_usage(os.path.dirname(path)).free < needed + FREE_MARGIN:
        raise NoSpace(f'sin espacio para reescribir {path}')


def _check_written(path, audio_before, changes, digest, pictures):
    after = FLAC(path)
    expected = {k: v for k, v in audio_before.items()}
    for key, values in changes.items():
        if values:
            expected[key] = sorted(values)
        else:
            expected.pop(key, None)
    if tag_snapshot(after) != expected:
        raise VerifyError(f'las etiquetas no coinciden tras escribir: {path}')
    if picture_digest(after) != pictures:
        raise VerifyError(f'las carátulas cambiaron: {path}')
    if audio_digest(path) != digest:
        raise VerifyError(f'el audio cambió: {path}')


def write_flac(path, changes, run):
    """Aplica `changes` {ETIQUETA: [valores] | None} solo si todo queda verificado.

    Si cabe en el relleno se reescribe la cabecera en su sitio; si no, se trabaja
    sobre una copia que sustituye al original solo cuando se ha verificado.
    """
    changes = {k.upper(): v for k, v in changes.items()}
    forbidden = set(changes) - WRITABLE
    if forbidden:
        raise ValueError(f'etiquetas no permitidas: {sorted(forbidden)}')
    if not changes:
        return
    stat = os.stat(path)
    audio = FLAC(path)
    before = tag_snapshot(audio)
    digest = audio_digest(path)
    pictures = picture_digest(audio)
    previous = {k: before.get(k) for k in changes}

    if fits_in_padding(audio, changes):
        offset = flac_audio_offset(path)
        with open(path, 'rb') as f:
            header = f.read(offset)
        try:
            apply_changes(audio.tags, changes)
            audio.save(padding=lambda info: info.padding)
            if os.path.getsize(path) != stat.st_size or flac_audio_offset(path) != offset:
                raise VerifyError(f'el audio se movió: {path}')
            _check_written(path, before, changes, digest, pictures)
        except Exception:
            if os.path.getsize(path) == stat.st_size:
                with open(path, 'r+b') as f:
                    f.write(header)
            raise
    else:
        ensure_space(path, stat.st_size)
        fd, tmp = tempfile.mkstemp(prefix='.etiquetador-', suffix='.flac', dir=os.path.dirname(path))
        os.close(fd)
        try:
            shutil.copyfile(path, tmp)
            os.chmod(tmp, stat.st_mode)
            copy = FLAC(tmp)
            if copy.tags is None:
                copy.add_tags()
            apply_changes(copy.tags, changes)
            copy.save()
            _check_written(tmp, before, changes, digest, pictures)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    run.journal({'path': path, 'previous': previous, 'new': changes})


def write_other(path, track, run):
    """ReplayGain de un fichero que no es FLAC, sobre una copia verificada."""
    stat = os.stat(path)
    ensure_space(path, stat.st_size)
    suffix = os.path.splitext(path)[1]
    fd, tmp = tempfile.mkstemp(prefix='.etiquetador-', suffix=suffix, dir=os.path.dirname(path))
    os.close(fd)
    try:
        shutil.copyfile(path, tmp)
        os.chmod(tmp, stat.st_mode)
        r128gain.tag(tmp, track['loudness'], min(track['peak'], 1.0),
                     album_loudness=track['album_loudness'], album_peak=min(track['album_peak'], 1.0))
        if decoded_digest(tmp) != decoded_digest(path):
            raise VerifyError(f'el audio cambió: {path}')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    run.journal({'path': path, 'new': {k: track[k] for k in ('loudness', 'peak', 'album_loudness', 'album_peak')}})


def decoded_digest(path):
    res = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-map', '0:a', '-f', 'md5', '-'],
                         capture_output=True, text=True, check=True)
    return res.stdout.strip()


# ── Lectura de discos ─────────────────────────────────────────────────────────
def is_loose(path):
    path = os.path.abspath(path)
    return any(path == r or path.startswith(r.rstrip('/') + '/') for r in LOOSE_ROOTS)


def audio_files(directory):
    return sorted(os.path.join(directory, f) for f in os.listdir(directory)
                  if os.path.splitext(f)[1].lower() in AUDIO_EXTS and not f.startswith('.etiquetador-')
                  and os.path.isfile(os.path.join(directory, f)))


def iter_groups(root):
    """Agrupa un árbol en (álbum, ficheros, suelto). CD1/CD2 forman un solo álbum."""
    albums = collections.defaultdict(list)
    for current, dirs, _files in os.walk(root):
        dirs.sort()
        found = audio_files(current)
        if not found or os.path.abspath(current) in INBOX_ROOTS:
            continue
        if is_loose(current):
            for path in found:
                yield path, [path], True
            continue
        name = os.path.basename(current)
        albums[os.path.dirname(current) if DISC_DIR.match(name) else current].extend(found)
    for album, files in sorted(albums.items()):
        yield album, sorted(files), False


def first_int(value):
    match = re.match(r'\s*(\d+)', str(value or ''))
    return int(match.group(1)) if match else 0


def read_tracks(files):
    """Pistas FLAC en orden de disco y pista. Excepción si alguna está dañada."""
    tracks = []
    for path in files:
        if not path.lower().endswith('.flac'):
            continue
        audio = FLAC(path)
        tags = tag_snapshot(audio)
        if audio.info.length <= 0:
            raise VerifyError(f'FLAC sin audio: {path}')
        tracks.append({
            'path': path, 'tags': tags, 'length': audio.info.length,
            'disc': first_int((tags.get('DISCNUMBER') or [1])[0]),
            'track': first_int((tags.get('TRACKNUMBER') or [0])[0]),
        })
    return sorted(tracks, key=lambda t: (t['disc'], t['track'], t['path']))


def common_tag(tracks, *keys):
    """Valor de la primera etiqueta presente, si todas las pistas coinciden en él."""
    for key in keys:
        values = {t['tags'][key][0] for t in tracks if t['tags'].get(key)}
        if len(values) == 1:
            return values.pop()
        if len(values) > 1:
            return None
    return None


# ── HTTP ──────────────────────────────────────────────────────────────────────
class Throttle:
    def __init__(self, interval):
        self.interval, self.last = interval, 0.0

    def wait(self):
        pause = self.last + self.interval - time.time()
        if pause > 0:
            time.sleep(pause)
        self.last = time.time()


mb_rate = Throttle(1.1)
discogs_rate = Throttle(1.05)


def get_json(url, rate, params=None, headers=None):
    last = None
    for attempt in range(4):
        rate.wait()
        try:
            r = requests.get(url, params=params, headers={'User-Agent': UA, **(headers or {})}, timeout=25)
        except requests.RequestException as e:
            last = e
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = RuntimeError(f'HTTP {r.status_code}')
            time.sleep(min(int(r.headers.get('Retry-After') or 0) or 5 * 2 ** attempt, 65))
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    raise last


def mb_get(path, **params):
    return get_json(f'{MB}/{path}', mb_rate, {**params, 'fmt': 'json'})


def discogs_get(path, **params):
    return get_json(f'{DISCOGS}/{path}', discogs_rate, params, {'Authorization': f'Discogs token={DISCOGS_TOKEN}'})


# ── Sello original ────────────────────────────────────────────────────────────
def release_labels(release):
    names = []
    for info in release.get('label-info') or []:
        name = (info.get('label') or {}).get('name')
        if name and norm(name) != 'no label' and name not in names:
            names.append(name)
    return names


def earliest_label(releases, current_labels=()):
    """Sello de la primera edición oficial: {'label'} o {'review': motivo}."""
    official = [r for r in releases if r.get('date') and r.get('status') in (None, 'Official')]
    if not official:
        return {'review': 'ninguna edición oficial con fecha'}
    first_date = min(r['date'] for r in official)
    labelled = [r for r in official if release_labels(r)]
    if not labelled:
        return {'review': 'ninguna edición oficial con sello'}
    date = min(r['date'] for r in labelled)
    if date[:4] != first_date[:4]:
        return {'review': f'la primera edición ({first_date}) no tiene sello; la siguiente es de {date}'}
    # Una fecha solo con año es compatible con cualquier mes de ese año.
    candidates = [r for r in labelled if r['date'].startswith(date)]
    votes = collections.Counter()
    names = {}
    for release in candidates:
        for name in release_labels(release):
            key = norm_label(name)
            votes[key] += 1
            names.setdefault(key, name)
    ranked = votes.most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        tied = [k for k, n in ranked if n == ranked[0][1]]
        kept = [k for k in tied if k in {norm_label(c) for c in current_labels}]
        if len(kept) == 1:
            return {'label': names[kept[0]], 'date': date, 'all': sorted(votes)}
        return {'review': f'empate en {date}: ' + ', '.join(names[k] for k in tied), 'all': sorted(votes)}
    return {'label': names[ranked[0][0]], 'date': date, 'all': sorted(votes)}


def picard_genres(entity):
    genres = [g for g in entity.get('genres') or [] if g.get('count', 0) > 0]
    if not genres:
        return []
    top = max(g['count'] for g in genres)
    genres = [g for g in genres if g['count'] >= top * MIN_GENRE_USAGE]
    genres.sort(key=lambda g: (-g['count'], g['name']))
    return [g['name'].title() for g in genres[:MAX_GENRES]]


def durations_match(tracks, remote_ms):
    if len(tracks) != len(remote_ms):
        return False
    known = [(t['length'], ms / 1000) for t, ms in zip(tracks, remote_ms) if ms]
    if not known:
        return False
    close = sum(abs(a - b) <= MAX_DURATION_DIFF for a, b in known)
    return close / len(tracks) >= MIN_DURATION_MATCH


def mb_releases(rgid):
    releases, offset = [], 0
    while True:
        page = mb_get('release', **{'release-group': rgid, 'inc': 'labels+media', 'limit': 100, 'offset': offset})
        if not page:
            return releases
        releases += page.get('releases') or []
        offset += 100
        if offset >= (page.get('release-count') or 0) or offset >= 300:
            return releases


def mb_verified_release(tracks, releases):
    """Primera edición del grupo con las mismas pistas y duraciones, o None."""
    same_count = [r for r in releases
                  if sum(m.get('track-count') or 0 for m in r.get('media') or []) == len(tracks)]
    for release in same_count[:3]:
        full = mb_get(f"release/{release['id']}", inc='recordings')
        remote = [t.get('length') for m in (full or {}).get('media') or [] for t in m.get('tracks') or []]
        if durations_match(tracks, remote):
            return release
    return None


def credit_name(entity):
    return ''.join(c.get('name', '') + c.get('joinphrase', '') for c in entity.get('artist-credit') or [])


def lucene(text):
    return re.sub(r'([\\"])', r'\\\1', text)


def identify_mb(tracks, artist, title):
    """Grupo de ediciones de MusicBrainz: {'rgid','releases','how'} o {'review'}."""
    rgid = common_tag(tracks, 'MUSICBRAINZ_RELEASEGROUPID')
    how = 'etiqueta MUSICBRAINZ_RELEASEGROUPID'
    if not rgid:
        albumid = common_tag(tracks, 'MUSICBRAINZ_ALBUMID')
        if albumid:
            rel = mb_get(f'release/{albumid}', inc='release-groups') or {}
            rgid = (rel.get('release-group') or {}).get('id')
            how = 'etiqueta MUSICBRAINZ_ALBUMID'
    if rgid:
        return {'rgid': rgid, 'releases': mb_releases(rgid), 'how': how}

    wanted = EDITION.sub('', title).strip() or title
    found = mb_get('release-group', query=f'artist:"{lucene(artist)}" AND releasegroup:"{lucene(wanted)}"', limit=10)
    exact = [rg for rg in (found or {}).get('release-groups') or []
             if base_title(rg.get('title')) == base_title(title) and norm(credit_name(rg)) == norm(artist)]
    if not exact:
        return {'review': 'sin coincidencia exacta de artista y título en MusicBrainz'}
    verified = []
    for rg in exact[:4]:
        releases = mb_releases(rg['id'])
        if mb_verified_release(tracks, releases):
            verified.append({'rgid': rg['id'], 'releases': releases, 'how': 'artista, título y duraciones'})
    if not verified:
        return {'review': 'MusicBrainz no tiene una edición con las mismas pistas y duraciones'}
    if len(verified) > 1:
        return {'review': f'{len(verified)} grupos de ediciones coinciden'}
    return verified[0]


def discogs_tracks(entity):
    return [t for t in entity.get('tracklist') or [] if t.get('type_', 'track') == 'track']


def clean_discogs_artist(name):
    return re.sub(r'\s*\(\d+\)', '', (name or '').replace('*', ''))


def discogs_title_match(result, artist, title):
    credit, _, name = (result.get('title') or '').partition(' - ')
    return base_title(name) == base_title(title) and norm(clean_discogs_artist(credit)) == norm(artist)


def identify_discogs(tracks, artist, title):
    """Sello más antiguo y estilos en Discogs: {'labels','genres','how'} o {'review'}."""
    if not DISCOGS_TOKEN:
        return {'review': 'sin DISCOGS_TOKEN'}
    wanted = EDITION.sub('', title).strip() or title
    masters = (discogs_get('database/search', type='master', artist=artist, release_title=wanted, per_page=10) or {})
    for result in masters.get('results') or []:
        if not discogs_title_match(result, artist, title):
            continue
        master = discogs_get(f"masters/{result['id']}")
        if not master or len(discogs_tracks(master)) != len(tracks):
            continue
        versions = discogs_get(f"masters/{result['id']}/versions", sort='released', sort_order='asc', per_page=50) or {}
        rows = [(str(v.get('released') or ''), v.get('label')) for v in versions.get('versions') or []]
        return discogs_result(rows, master, 'master')
    releases = (discogs_get('database/search', type='release', artist=artist, release_title=wanted, per_page=25) or {})
    exact = [r for r in releases.get('results') or [] if discogs_title_match(r, artist, title) and r.get('year')]
    exact.sort(key=lambda r: str(r['year']))
    for result in exact[:3]:
        release = discogs_get(f"releases/{result['id']}")
        if not release or len(discogs_tracks(release)) != len(tracks):
            continue
        rows = [(str(r['year']), lab) for r in exact for lab in (r.get('label') or [])[:1]]
        return discogs_result(rows, release, 'edición')
    return {'review': 'sin coincidencia con las mismas pistas en Discogs'}


def discogs_result(rows, entity, how):
    dated = [(d[:4], label) for d, label in rows if d[:4].isdigit() and d[:4] != '0000'
             and label and not norm(label).startswith('not on label')]
    genres = list(entity.get('styles') or entity.get('genres') or [])[:MAX_GENRES]
    if not dated:
        return {'labels': [], 'genres': genres, 'how': how}
    first = min(d for d, _ in dated)
    votes = collections.Counter()
    names = {}
    for date, label in dated:
        if date == first:
            votes[norm_label(label)] += 1
            names.setdefault(norm_label(label), re.sub(r'\s*\(\d+\)\s*$', '', label.rstrip('* ')))
    return {'labels': [names[k] for k, _ in votes.most_common()], 'date': first, 'genres': genres, 'how': how}


def decide_label(mb_label, discogs, current):
    """Une ambas fuentes: si se contradicen, no se escribe y queda para revisar."""
    d_labels = discogs.get('labels') or []
    d_keys = {norm_label(n) for n in d_labels}
    if mb_label.get('label'):
        keys = set(mb_label.get('all') or [norm_label(mb_label['label'])])
        if d_keys and not keys & d_keys:
            return {'review': f"MusicBrainz dice {mb_label['label']}, Discogs dice {', '.join(d_labels)}"}
        return {'label': mb_label['label'], 'source': 'MusicBrainz+Discogs' if d_keys else 'MusicBrainz'}
    if mb_label.get('review') and 'empate' in mb_label['review'] and d_keys:
        tied = d_keys & set(mb_label.get('all') or [])
        if len(tied) == 1:
            return {'label': next(n for n in d_labels if norm_label(n) in tied), 'source': 'Discogs desempata'}
        return {'review': mb_label['review']}
    if len(d_labels) == 1:
        return {'label': d_labels[0], 'source': 'Discogs'}
    if len(d_labels) > 1:
        kept = [n for n in d_labels if norm_label(n) in {norm_label(c) for c in current}]
        if len(kept) == 1:
            return {'label': kept[0], 'source': 'Discogs'}
        return {'review': 'empate en Discogs: ' + ', '.join(d_labels)}
    return {'review': mb_label.get('review') or discogs.get('review') or 'sin sello'}


def label_changes(tracks, label):
    """Cambios para que LABEL sea `label`, guardando en LABEL_ANTERIOR lo que hubiera."""
    plan = {}
    for track in tracks:
        tags = track['tags']
        current = [v for k in LABEL_KEYS for v in tags.get(k, [])]
        if len(current) == 1 and tags.get('LABEL') == current and norm_label(current[0]) == norm_label(label):
            continue
        old = [v for v in dict.fromkeys(current + tags.get(OLD_LABEL_KEY, []))
               if norm_label(v) != norm_label(label)]
        changes = {'LABEL': [label]}
        if tags.get('ORGANIZATION'):
            changes['ORGANIZATION'] = None
        if tags.get('PUBLISHER'):
            changes['PUBLISHER'] = None
        if old:
            changes[OLD_LABEL_KEY] = old
        plan[track['path']] = changes
    return plan


# ── BPM ───────────────────────────────────────────────────────────────────────
def analyse_bpm(path):
    """(bpm, confianza) por análisis del audio; muy costoso, solo si falta."""
    import essentia.standard as es
    audio = es.MonoLoader(filename=path, sampleRate=44100)()
    bpm, _beats, confidence, _est, _intervals = es.RhythmExtractor2013(method='multifeature')(audio)
    return float(bpm), float(confidence)


def bpm_changes(tracks):
    plan, report = {}, {'analizadas': 0, 'dudosas': 0, 'ya_tenian': 0, 'valores': []}
    for track in tracks:
        if track['tags'].get('BPM'):
            report['ya_tenian'] += 1
            continue
        bpm, confidence = analyse_bpm(track['path'])
        report['analizadas'] += 1
        report['valores'].append([round(bpm, 1), round(confidence, 2)])
        if confidence >= MIN_BPM_CONFIDENCE and bpm > 0:
            plan[track['path']] = {'BPM': [str(round(bpm))]}
        else:
            report['dudosas'] += 1
    return plan, report


# ── ReplayGain ────────────────────────────────────────────────────────────────
def rg_tags(path):
    """Valores ReplayGain presentes en cualquier formato, con la clave normalizada."""
    audio = mutagen.File(path)
    found = {}
    for key in (audio.tags or {}).keys() if audio is not None else []:
        name = str(key).upper().replace('TXXX:', '').replace('----:COM.APPLE.ITUNES:', '')
        if name in RG_KEYS:
            found[name] = str(audio.tags[key]).strip("[]' ")
    return found


def rg_values(loudness, peak, album_loudness, album_peak):
    ref = RG_REFERENCE_LUFS
    return {
        'REPLAYGAIN_TRACK_GAIN': [f'{ref - loudness:.2f} dB'],
        'REPLAYGAIN_TRACK_PEAK': [f'{peak:.8f}'],
        'REPLAYGAIN_ALBUM_GAIN': [f'{ref - album_loudness:.2f} dB'],
        'REPLAYGAIN_ALBUM_PEAK': [f'{album_peak:.8f}'],
    }


def ebur128(inputs, graph):
    """(volumen integrado en LUFS, pico real lineal) del grafo de ffmpeg dado."""
    cmd = ['ffmpeg', '-hide_banner', '-nostats']
    for path in inputs:
        cmd += ['-i', path]
    res = subprocess.run(cmd + ['-filter_complex', graph, '-map', '[o]', '-f', 'null', os.devnull],
                         capture_output=True, text=True, errors='replace')
    loudness = re.findall(r'^\s*I:\s+(-?[\d.]+) LUFS', res.stderr, re.M)
    peak = re.findall(r'^\s*Peak:\s+(-?[\d.]+|-inf) dBFS', res.stderr, re.M)
    if res.returncode or not loudness or not peak:
        raise VerifyError(f'ffmpeg no pudo medir {inputs[0]}: {res.stderr[-200:]}')
    level = float(loudness[-1])
    # Una pista en silencio no se amplifica.
    if level <= SILENCE_LUFS:
        level = RG_REFERENCE_LUFS
    return level, 10 ** (float(peak[-1]) / 20)


def measure_track(path):
    return ebur128([path], '[0:a]ebur128=framelog=quiet:peak=true[o]')


def measure_album(files):
    """Volumen del álbum como una sola pieza; admite pistas de formatos distintos."""
    chains = ''.join(f'[{i}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{i}];'
                     for i in range(len(files)))
    joined = ''.join(f'[a{i}]' for i in range(len(files)))
    return ebur128(files, f'{chains}{joined}concat=n={len(files)}:v=0:a=1,ebur128=framelog=quiet:peak=true[o]')[0]


def gain_needed(files, loose):
    """Qué falta: 'todo', 'copiar_pista' (solo sueltas) o None."""
    existing = {p: rg_tags(p) for p in files}
    if loose:
        tags = existing[files[0]]
        if 'REPLAYGAIN_TRACK_GAIN' not in tags or 'REPLAYGAIN_TRACK_PEAK' not in tags:
            return 'todo', existing
        if 'REPLAYGAIN_ALBUM_GAIN' not in tags or 'REPLAYGAIN_ALBUM_PEAK' not in tags:
            # Solo se copia en FLAC; en otros formatos se mide y se reescriben ambas.
            return ('copiar_pista' if files[0].lower().endswith('.flac') else 'todo'), existing
        return None, existing
    complete = all(all(k in tags for k in RG_KEYS) for tags in existing.values())
    uniform = len({tags.get('REPLAYGAIN_ALBUM_GAIN') for tags in existing.values()}) == 1
    return (None if complete and uniform else 'todo'), existing


def gain_plan(files, loose):
    """{fichero: datos de ganancia} con ambas ganancias de cada fichero.

    Una canción suelta es su propio álbum: su ganancia de álbum es la de pista.
    Un álbum se mide entero, para que todas sus pistas compartan la de álbum.
    """
    need, existing = gain_needed(files, loose)
    if not need:
        return {}
    if need == 'copiar_pista':
        tags = existing[files[0]]
        return {files[0]: {'copy': {
            'REPLAYGAIN_ALBUM_GAIN': [tags['REPLAYGAIN_TRACK_GAIN']],
            'REPLAYGAIN_ALBUM_PEAK': [tags['REPLAYGAIN_TRACK_PEAK']]}}}
    measured = {path: measure_track(path) for path in files}
    album_loudness = None if loose else measure_album(files)
    album_peak = max(peak for _loudness, peak in measured.values())
    plan = {}
    for path, (loudness, peak) in measured.items():
        plan[path] = {'loudness': loudness, 'peak': peak,
                      'album_loudness': loudness if loose else album_loudness,
                      'album_peak': peak if loose else album_peak}
    return plan


def song_gain_plan(path):
    """Canción recién llegada a Mix: su ganancia de álbum pasa a ser la de pista,
    aunque traiga la del disco del que salió."""
    tags = rg_tags(path)
    track = tags.get('REPLAYGAIN_TRACK_GAIN'), tags.get('REPLAYGAIN_TRACK_PEAK')
    if all(track) and (tags.get('REPLAYGAIN_ALBUM_GAIN'), tags.get('REPLAYGAIN_ALBUM_PEAK')) == track:
        return {}
    if all(track) and path.lower().endswith('.flac'):
        return {path: {'copy': {'REPLAYGAIN_ALBUM_GAIN': [track[0]], 'REPLAYGAIN_ALBUM_PEAK': [track[1]]}}}
    loudness, peak = measure_track(path)
    return {path: {'loudness': loudness, 'peak': peak, 'album_loudness': loudness, 'album_peak': peak}}


def gain_changes(entry):
    if 'copy' in entry:
        return entry['copy']
    return rg_values(entry['loudness'], entry['peak'], entry['album_loudness'], entry['album_peak'])


# ── Procesos ──────────────────────────────────────────────────────────────────
def merge_plans(*plans):
    merged = collections.defaultdict(dict)
    for plan in plans:
        for path, changes in plan.items():
            merged[path].update(changes)
    return merged


def process_album(album, run):
    """Sello, género y ganancia de un disco. Devuelve el informe."""
    files = [f for f in sorted(
        os.path.join(root, name) for root, _d, names in os.walk(album) for name in names)
        if os.path.splitext(f)[1].lower() in AUDIO_EXTS and not os.path.basename(f).startswith('.etiquetador-')]
    result = {'album': album, 'ficheros': len(files), 'revisar': []}
    try:
        tracks = read_tracks(files)
    except Exception as e:
        result['error'] = f'no se pudo leer: {e}'
        return result
    if not tracks:
        result['error'] = 'sin FLAC'
        return result

    artist = common_tag(tracks, 'ALBUMARTIST', 'ARTIST')
    title = common_tag(tracks, 'ALBUM')
    result.update(artista=artist, titulo=title)
    current_labels = list(dict.fromkeys(v for t in tracks for k in LABEL_KEYS for v in t['tags'].get(k, [])))
    result['sello_actual'] = current_labels
    result['genero_actual'] = list(dict.fromkeys(v for t in tracks for v in t['tags'].get('GENRE', [])))
    plans = []

    if artist and title:
        plans += identify_metadata(tracks, artist, title, current_labels, result)
    else:
        result['revisar'].append('artista o álbum ausente o distinto entre pistas: no se busca sello ni género')

    try:
        gains = gain_plan(files, loose=False)
        result['ganancia'] = {'ficheros': len(gains)}
        sample = next((e for e in gains.values() if 'copy' not in e), None)
        if sample:
            result['ganancia']['album_db'] = round(RG_REFERENCE_LUFS - sample['album_loudness'], 2)
        plans.append({p: gain_changes(e) for p, e in gains.items() if p.lower().endswith('.flac')})
    except Exception as e:
        gains = {}
        result['revisar'].append(f'ganancia: {e}')

    merged = merge_plans(*plans)
    result['cambios_ejemplo'] = dict(list(merged.items())[:1])
    result['ficheros_con_cambios'] = len(merged)
    if run.write:
        for path, changes in merged.items():
            write_flac(path, changes, run)
        for path, entry in gains.items():
            if not path.lower().endswith('.flac') and 'copy' not in entry:
                write_other(path, entry, run)
    return result


def identify_metadata(tracks, artist, title, current_labels, result):
    plans = []
    mb = identify_mb(tracks, artist, title)
    mb_label = {'review': mb['review']} if mb.get('review') else earliest_label(mb['releases'], current_labels)
    result['musicbrainz'] = {k: mb_label.get(k) for k in ('label', 'date', 'review') if mb_label.get(k)}
    result['musicbrainz']['identificado_por'] = mb.get('how')

    discogs = identify_discogs(tracks, artist, title)
    result['discogs'] = {k: discogs.get(k) for k in ('labels', 'date', 'genres', 'how', 'review') if discogs.get(k)}

    decision = decide_label(mb_label, discogs, current_labels)
    result['sello'] = decision
    if decision.get('label'):
        plans.append(label_changes(tracks, decision['label']))
    else:
        result['revisar'].append(f"sello: {decision['review']}")

    genres, source = [], None
    if mb.get('rgid'):
        group = mb_get(f"release-group/{mb['rgid']}", inc='genres') or {}
        genres, source = picard_genres(group), 'MusicBrainz'
        if not genres:
            verified = mb_verified_release(tracks, mb['releases'])
            if verified:
                full = mb_get(f"release/{verified['id']}", inc='genres') or {}
                genres = picard_genres(full)
    if not genres and discogs.get('genres'):
        genres, source = discogs['genres'], 'Discogs'
    result['genero'] = {'valores': genres, 'fuente': source if genres else None}
    if genres:
        plans.append({t['path']: {'GENRE': genres} for t in tracks if t['tags'].get('GENRE') != sorted(genres)})
    else:
        result['revisar'].append('género: sin datos, se conserva el actual')
    return plans


def album_dirs(root):
    return [album for album, _files, loose in iter_groups(root) if not loose]


def settled(album):
    """False si algún fichero cambió hace poco: la copia podría seguir en curso."""
    limit = time.time() - SETTLE_SECONDS
    for current, _dirs, files in os.walk(album):
        for name in files:
            if os.stat(os.path.join(current, name)).st_ctime > limit:
                return False
    return True


def write_report(run, results):
    path = os.path.join(DATA_DIR, f'informe-{run.id}.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=1, default=repr)
    print(f'\nInforme: {path}')


def print_album(res):
    name = os.path.basename(res['album'])
    if res.get('error'):
        print(f"\n✗ {name}\n    {res['error']}")
        return
    print(f"\n{'!' if res['revisar'] else '✓'} {name}")
    label = res.get('sello') or {}
    print(f"    sello:   {res.get('sello_actual') or '—'} → {label.get('label') or 'sin cambios'}"
          f"{' (' + label['source'] + ')' if label.get('source') else ''}")
    genre = res.get('genero') or {}
    print(f"    género:  {res.get('genero_actual') or '—'} → {genre.get('valores') or 'sin cambios'}"
          f"{' (' + genre['fuente'] + ')' if genre.get('fuente') else ''}")
    gain = res.get('ganancia')
    if gain:
        print(f"    gain:    {gain['ficheros']} ficheros, álbum {gain.get('album_db', '—')} dB")
    for note in res['revisar']:
        print(f'    revisar: {note}')


def cmd_albums(albums, run):
    results, state = [], load_state()
    for album in albums:
        start = time.time()
        try:
            res = process_album(album, run)
        except NoSpace:
            raise
        except Exception as e:  # p. ej. un fichero borrado o movido a mitad de proceso
            res = {'album': album, 'revisar': [], 'error': f'{type(e).__name__}: {e}'}
        res['segundos'] = round(time.time() - start, 1)
        results.append(res)
        print_album(res)
        if run.write and not res.get('error'):
            state[album] = {'run': run.id, 'revisar': res['revisar']}
            save_state(state)
    write_report(run, results)
    if run.write:
        print(f'Para deshacer: etiquetador.py deshacer {run.id}')


def process_song(path, run):
    """BPM y ganancias de una canción de Mix; nunca sello ni género, que ahí es personal."""
    result = {'cancion': path, 'bpm': None, 'cambios': {}}
    gains = song_gain_plan(path)
    entry = gains.get(path)
    is_flac = path.lower().endswith('.flac')
    changes = gain_changes(entry) if entry else {}
    if is_flac:
        bpm_plan, report = bpm_changes(read_tracks([path]))
        result['bpm'] = report
        changes = {**changes, **bpm_plan.get(path, {})}
    result['cambios'] = changes
    if run.write and changes:
        if is_flac:
            write_flac(path, changes, run)
        else:
            write_other(path, entry, run)
    return result


def cmd_mix(paths, run):
    """Devuelve cuántas canciones fallaron. Admite ficheros o carpetas dentro de Mix."""
    outside = [p for p in paths if not is_loose(p)]
    if outside:
        sys.exit(f'Fuera de {LOOSE_ROOTS}, no se procesa: {outside}')
    songs = []
    for path in paths:
        songs += [path] if os.path.isfile(path) else [g for g, _f, _l in iter_groups(path)]
    if not songs:
        sys.exit(f'Sin canciones en {paths}')
    results, failed = [], 0
    for song in songs:
        try:
            res = process_song(song, run)
            print(f"✓ {song}: {', '.join(res['cambios']) or 'sin cambios'}")
        except NoSpace:
            raise
        except Exception as e:
            failed += 1
            res = {'cancion': song, 'error': f'{type(e).__name__}: {e}'}
            print(f"✗ {song}: {res['error']}")
        results.append(res)
    write_report(run, results)
    if run.write:
        print(f'Para deshacer: etiquetador.py deshacer {run.id}')
    return failed


def cmd_gain(roots, run, limit):
    """Ganancias de todo un árbol. Sin --write solo cuenta qué falta, sin medir."""
    totals = collections.Counter()
    done = 0
    for root in roots:
        for group, files, loose in iter_groups(root):
            kind = 'sueltas' if loose else 'albumes'
            try:
                need, _existing = gain_needed(files, loose)
            except Exception as e:
                totals[f'{kind}_ilegibles'] += 1
                print(f'  ilegible {group}: {e}')
                continue
            totals[f'{kind}_total'] += 1
            totals[f'{kind}_{need or "completos"}'] += 1
            if not need:
                continue
            totals[f'{kind}_ficheros_a_escribir'] += len(files)
            if run.write and (limit is None or done < limit):
                try:
                    plan = gain_plan(files, loose)
                    for path, entry in plan.items():
                        if path.lower().endswith('.flac'):
                            write_flac(path, gain_changes(entry), run)
                        elif 'copy' not in entry:
                            write_other(path, entry, run)
                    done += 1
                except NoSpace:
                    raise
                except Exception as e:
                    totals['errores'] += 1
                    print(f'  error {group}: {e}')
    print(json.dumps(totals, indent=1, ensure_ascii=False, sort_keys=True))


def cmd_undo(run_id):
    path = os.path.join(DATA_DIR, f'diario-{run_id}.jsonl')
    run = Run(True, f'{run_id}-deshecho')
    with open(path, encoding='utf-8') as f:
        entries = [json.loads(line) for line in f]
    skipped = 0
    for entry in reversed(entries):
        if 'previous' not in entry:
            skipped += 1
            continue
        write_flac(entry['path'], entry['previous'], run)
    print(f'Restaurados {len(entries) - skipped} ficheros; {skipped} sin FLAC conservan su ganancia.')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    p_album = sub.add_parser('album')
    p_album.add_argument('rutas', nargs='+')
    p_pending = sub.add_parser('pendientes')
    p_pending.add_argument('--limit', type=int)
    for p in (p_album, p_pending):
        p.add_argument('--write', action='store_true')
    p_mix = sub.add_parser('mix')
    p_mix.add_argument('rutas', nargs='+')
    p_mix.add_argument('--write', action='store_true')
    p_gain = sub.add_parser('ganancia')
    p_gain.add_argument('rutas', nargs='+')
    p_gain.add_argument('--write', action='store_true')
    p_gain.add_argument('--limit', type=int)
    p_undo = sub.add_parser('deshacer')
    p_undo.add_argument('run_id')
    args = parser.parse_args()

    try:
        if args.cmd == 'deshacer':
            cmd_undo(args.run_id)
        elif args.cmd == 'album':
            cmd_albums(args.rutas, Run(args.write))
        elif args.cmd == 'mix':
            sys.exit(1 if cmd_mix(args.rutas, Run(args.write)) else 0)
        elif args.cmd == 'pendientes':
            state = load_state()
            albums = [a for root in TEMP_ROOTS for a in album_dirs(root) if a not in state and settled(a)]
            print(f'{len(albums)} discos pendientes')
            cmd_albums(albums[:args.limit], Run(args.write))
        else:
            cmd_gain(args.rutas, Run(args.write), args.limit)
    except NoSpace as e:
        sys.exit(f'PARADO: {e}. Lo ya escrito queda en el diario.')


if __name__ == '__main__':
    main()
