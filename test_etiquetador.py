import json
import os
import subprocess

import pytest
from mutagen.flac import FLAC

import etiquetador as e


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(e, 'DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setattr(e, 'FREE_MARGIN', 0)


@pytest.fixture
def run():
    return e.Run(True, 'prueba')


def make_flac(path, seconds=2, freq=440, **tags):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'sine=frequency={freq}:duration={seconds}',
                    str(path)], check=True)
    audio = FLAC(path)
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return str(path)


def padding_of(path):
    return next((b.length for b in FLAC(path).metadata_blocks if b.code == 1), 0)


def strip_padding(path):
    audio = FLAC(path)
    audio.save(padding=lambda info: 0)
    assert padding_of(path) == 0


def test_rejects_tags_outside_the_list(tmp_path, run):
    path = make_flac(tmp_path / 'a.flac', ARTIST='Original', ALBUM='Original')
    for key in ('ARTIST', 'ALBUM', 'TITLE', 'ALBUMARTIST', 'artist'):
        with pytest.raises(ValueError):
            e.write_flac(path, {key: ['Otro']}, run)
    audio = FLAC(path)
    assert audio['ARTIST'] == ['Original'] and audio['ALBUM'] == ['Original']


def test_in_place_write_keeps_everything_else(tmp_path, run):
    path = make_flac(tmp_path / 'a.flac', ARTIST='A', ALBUM='B', TITLE='C', COMMENT='x')
    assert padding_of(path) > 200
    os.utime(path, (1000, 2000))
    size, inode, digest = os.path.getsize(path), os.stat(path).st_ino, e.audio_digest(path)
    e.write_flac(path, {'LABEL': ['Mute'], 'BPM': ['120']}, run)
    audio = FLAC(path)
    assert audio['LABEL'] == ['Mute'] and audio['BPM'] == ['120'] and audio['ARTIST'] == ['A']
    assert audio['COMMENT'] == ['x'] and audio['TITLE'] == ['C']
    assert (os.path.getsize(path), os.stat(path).st_ino) == (size, inode)
    assert e.audio_digest(path) == digest and os.path.getmtime(path) == 2000


def test_no_padding_rewrites_through_a_copy(tmp_path, run):
    path = make_flac(tmp_path / 'a.flac', ARTIST='A')
    strip_padding(path)
    inode, digest = os.stat(path).st_ino, e.audio_digest(path)
    e.write_flac(path, {'LABEL': ['Mute' * 200]}, run)
    assert os.stat(path).st_ino != inode
    assert e.audio_digest(path) == digest and FLAC(path)['ARTIST'] == ['A']
    assert os.listdir(tmp_path / '') == ['a.flac', 'data'] or 'data' in os.listdir(tmp_path)
    assert not [f for f in os.listdir(tmp_path) if f.startswith('.etiquetador-')]


def test_no_space_stops_and_leaves_file_alone(tmp_path, run, monkeypatch):
    path = make_flac(tmp_path / 'a.flac', ARTIST='A')
    strip_padding(path)
    before = open(path, 'rb').read()
    monkeypatch.setattr(e, 'FREE_MARGIN', 10 ** 18)
    with pytest.raises(e.NoSpace):
        e.write_flac(path, {'LABEL': ['Mute']}, run)
    assert open(path, 'rb').read() == before


@pytest.mark.parametrize('padded', [True, False])
def test_failed_verification_restores_the_file(tmp_path, run, monkeypatch, padded):
    path = make_flac(tmp_path / 'a.flac', ARTIST='A', LABEL='Vieja')
    if not padded:
        strip_padding(path)
    before = open(path, 'rb').read()
    real = e.tag_snapshot
    calls = {'n': 0}

    def sabotaged(audio):
        calls['n'] += 1
        snap = real(audio)
        if calls['n'] > 1:
            snap['ARTIST'] = ['Cambiado']
        return snap

    monkeypatch.setattr(e, 'tag_snapshot', sabotaged)
    with pytest.raises(e.VerifyError):
        e.write_flac(path, {'LABEL': ['Nueva' * 100]}, run)
    assert open(path, 'rb').read() == before
    assert not [f for f in os.listdir(tmp_path) if f.startswith('.etiquetador-')]


def test_undo_restores_previous_values(tmp_path, run):
    path = make_flac(tmp_path / 'a.flac', ARTIST='A', ORGANIZATION='Vieja')
    e.write_flac(path, {'LABEL': ['Nueva'], 'ORGANIZATION': None, 'LABEL_ANTERIOR': ['Vieja']}, run)
    assert 'ORGANIZATION' not in FLAC(path)
    e.cmd_undo('prueba')
    audio = FLAC(path)
    assert audio['ORGANIZATION'] == ['Vieja'] and 'LABEL' not in audio and 'LABEL_ANTERIOR' not in audio


def track(path='a.flac', **tags):
    return {'path': path, 'tags': {k: [v] for k, v in tags.items()}, 'length': 100}


def test_label_moves_old_values_and_clears_conflicting_tags():
    plan = e.label_changes([track(ORGANIZATION='Craft Recordings', PUBLISHER='Otro')], 'Mute')
    assert plan['a.flac'] == {'LABEL': ['Mute'], 'ORGANIZATION': None, 'PUBLISHER': None,
                              'LABEL_ANTERIOR': ['Craft Recordings', 'Otro']}


def test_equivalent_label_is_not_rewritten():
    assert e.label_changes([track(LABEL='Mute Records')], 'Mute') == {}


def test_equivalent_label_in_other_tag_is_moved_without_keeping_it_as_old():
    assert e.label_changes([track(ORGANIZATION='Mute Records')], 'Mute')['a.flac'] == {
        'LABEL': ['Mute'], 'ORGANIZATION': None}


def rel(date, *labels, status='Official'):
    return {'date': date, 'status': status, 'label-info': [{'label': {'name': n}} for n in labels]}


def test_original_label_ignores_later_remasters():
    assert e.earliest_label([rel('2019-03-01', 'Craft Recordings'), rel('1986-04-07', 'Mute')])['label'] == 'Mute'


def test_label_ignores_bootlegs_and_no_label():
    releases = [rel('1980', 'Pirata', status='Bootleg'), rel('1986', '[no label]', 'Mute')]
    assert e.earliest_label(releases)['label'] == 'Mute'


def test_label_goes_to_review_if_first_edition_has_no_label():
    assert 'review' in e.earliest_label([rel('1980'), rel('2019', 'Craft Recordings')])


def test_label_tie_goes_to_review_unless_current_label_breaks_it():
    releases = [rel('1986-04-07', 'Mute'), rel('1986-04-07', 'Sire')]
    assert 'review' in e.earliest_label(releases)
    assert e.earliest_label(releases, ['Sire Records'])['label'] == 'Sire'


def test_year_only_date_is_compatible_with_any_month():
    assert 'review' in e.earliest_label([rel('1986', 'Mute'), rel('1986-04-07', 'Sire')])


def test_label_sources_that_disagree_go_to_review():
    mb = {'label': 'Mute', 'all': ['mute']}
    assert 'review' in e.decide_label(mb, {'labels': ['Warner']}, [])
    assert e.decide_label(mb, {'labels': ['Mute Records']}, [])['label'] == 'Mute'
    assert e.decide_label(mb, {'review': 'x'}, [])['source'] == 'MusicBrainz'


def test_discogs_breaks_tie_and_acts_as_second_source():
    tie = {'review': 'empate en 1986: Mute, Sire', 'all': ['mute', 'sire']}
    assert e.decide_label(tie, {'labels': ['Sire']}, [])['label'] == 'Sire'
    assert e.decide_label({'review': 'nada'}, {'labels': ['Warp']}, [])['label'] == 'Warp'
    assert 'review' in e.decide_label({'review': 'nada'}, {'labels': ['Warp', 'Mute']}, [])


def test_genres_follow_picard_threshold_and_title_case():
    entity = {'genres': [{'name': 'synth-pop', 'count': 10}, {'name': 'pop', 'count': 9},
                         {'name': 'rock', 'count': 2}]}
    assert e.picard_genres(entity) == ['Synth-Pop', 'Pop']


def test_titles_and_labels_compare_without_edition_noise():
    assert e.base_title('Wonderland (Special Edition)') == e.base_title('wonderland')
    assert e.base_title('The Circus [2011 Remaster] (Deluxe)') == 'the circus'
    assert e.base_title('Nightbird') != e.base_title('Wonderland')
    assert e.norm_label('Mute (2)') == e.norm_label('Mute Records') == 'mute'


def test_durations_must_match_most_tracks():
    tracks = [{'length': 100.0}, {'length': 200.0}]
    assert e.durations_match(tracks, [101000, 199000])
    assert not e.durations_match(tracks, [150000, 199000])
    assert not e.durations_match(tracks, [100000])
    assert not e.durations_match(tracks, [None, None])


def test_mb_rejects_search_without_exact_artist(monkeypatch):
    monkeypatch.setattr(e, 'mb_get', lambda path, **k: {
        'release-groups': [{'id': 'x', 'title': 'Disco', 'artist-credit': [{'name': 'Otro Artista'}]}]})
    res = e.identify_mb([track(ARTIST='Artista', ALBUM='Disco')], 'Artista', 'Disco')
    assert 'review' in res


def test_mb_rejects_group_when_durations_differ(monkeypatch):
    def fake(path, **k):
        if path == 'release-group':
            return {'release-groups': [{'id': 'rg', 'title': 'Disco', 'artist-credit': [{'name': 'A'}]}]}
        if path == 'release':
            return {'release-count': 1, 'releases': [{'id': 'r', 'media': [{'track-count': 1}]}]}
        return {'media': [{'tracks': [{'length': 999000}]}]}
    monkeypatch.setattr(e, 'mb_get', fake)
    assert 'review' in e.identify_mb([track()], 'A', 'Disco')


def test_groups_join_disc_folders_and_split_loose_files(tmp_path, monkeypatch):
    album = tmp_path / 'col/T/Tortoise/Album'
    make_flac(album / 'Disc 1/1.flac')
    make_flac(album / 'Disc 2/1.flac')
    other = tmp_path / 'col/T/Tortoise/Otro'
    make_flac(other / '1.flac')
    mix = tmp_path / 'col/Mix/House'
    make_flac(mix / 'x.flac')
    make_flac(mix / 'y.flac')
    monkeypatch.setattr(e, 'LOOSE_ROOTS', [str(tmp_path / 'col/Mix')])
    groups = {g: (len(f), loose) for g, f, loose in e.iter_groups(str(tmp_path / 'col'))}
    assert groups[str(album)] == (2, False)
    assert groups[str(other)] == (1, False)
    assert groups[str(mix / 'x.flac')] == (1, True)
    assert groups[str(mix / 'y.flac')] == (1, True)


def test_album_gain_is_shared_and_differs_from_track_gain(tmp_path):
    quiet = make_flac(tmp_path / 'a/1.flac', seconds=5, freq=200)
    loud = make_flac(tmp_path / 'a/2.flac', seconds=5, freq=2000)
    plan = e.gain_plan([quiet, loud], loose=False)
    assert plan[quiet]['album_loudness'] == plan[loud]['album_loudness']
    assert plan[quiet]['loudness'] != plan[loud]['loudness']


def test_loose_song_album_gain_equals_its_track_gain(tmp_path, run):
    path = make_flac(tmp_path / 'mix/1.flac', seconds=5)
    entry = e.gain_plan([path], loose=True)[path]
    e.write_flac(path, e.gain_changes(entry), run)
    audio = FLAC(path)
    assert audio['REPLAYGAIN_ALBUM_GAIN'] == audio['REPLAYGAIN_TRACK_GAIN']
    assert audio['REPLAYGAIN_ALBUM_PEAK'] == audio['REPLAYGAIN_TRACK_PEAK']
    assert e.gain_plan([path], loose=True) == {}


def test_loose_song_with_only_track_gain_copies_it(tmp_path):
    path = make_flac(tmp_path / 'mix/1.flac', REPLAYGAIN_TRACK_GAIN='-3.00 dB', REPLAYGAIN_TRACK_PEAK='0.5')
    assert e.gain_changes(e.gain_plan([path], loose=True)[path]) == {
        'REPLAYGAIN_ALBUM_GAIN': ['-3.00 dB'], 'REPLAYGAIN_ALBUM_PEAK': ['0.5']}


def test_silent_track_is_not_amplified(tmp_path):
    path = tmp_path / 'silencio.flac'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo', '-t', '5', str(path)],
                   check=True)
    loudness, _peak = e.measure_track(str(path))
    assert loudness == e.RG_REFERENCE_LUFS


def test_album_peak_is_the_highest_of_its_tracks(tmp_path):
    one = make_flac(tmp_path / 'a/1.flac', seconds=5, freq=200)
    two = make_flac(tmp_path / 'a/2.flac', seconds=5, freq=2000)
    plan = e.gain_plan([one, two], loose=False)
    assert plan[one]['album_peak'] == plan[two]['album_peak'] == max(plan[one]['peak'], plan[two]['peak'])


def test_incomplete_album_is_measured_again_entirely(tmp_path):
    full = dict(REPLAYGAIN_TRACK_GAIN='-3 dB', REPLAYGAIN_TRACK_PEAK='0.5',
                REPLAYGAIN_ALBUM_GAIN='-3 dB', REPLAYGAIN_ALBUM_PEAK='0.5')
    one = make_flac(tmp_path / 'a/1.flac', seconds=3, **full)
    two = make_flac(tmp_path / 'a/2.flac', seconds=3, REPLAYGAIN_TRACK_GAIN='-3 dB')
    assert set(e.gain_plan([one, two], loose=False)) == {one, two}
    three = make_flac(tmp_path / 'b/1.flac', **full)
    assert e.gain_plan([three], loose=False) == {}


def test_dry_run_never_changes_files(tmp_path, monkeypatch):
    path = make_flac(tmp_path / 'disco/1.flac', seconds=3, ARTIST='A', ALBUM='B', ORGANIZATION='Vieja')
    before = open(path, 'rb').read()
    monkeypatch.setattr(e, 'identify_mb', lambda *a: {'review': 'x'})
    monkeypatch.setattr(e, 'identify_discogs', lambda *a: {'labels': ['Nueva'], 'genres': ['House'], 'how': 'master'})
    res = e.process_album(str(tmp_path / 'disco'), e.Run(False))
    assert res['sello']['label'] == 'Nueva' and res['ficheros_con_cambios'] == 1
    assert open(path, 'rb').read() == before
    assert not os.path.exists(os.path.join(e.DATA_DIR, 'diario-prueba.jsonl'))


def test_full_write_flow(tmp_path, monkeypatch):
    album = tmp_path / 'disco'
    one = make_flac(album / 'Disc 1/1.flac', seconds=3, ARTIST='A', ALBUM='B', TITLE='T1', ORGANIZATION='Vieja')
    two = make_flac(album / 'Disc 2/2.flac', seconds=3, ARTIST='A', ALBUM='B', TITLE='T2', ORGANIZATION='Vieja')
    monkeypatch.setattr(e, 'identify_mb', lambda *a: {'review': 'x'})
    monkeypatch.setattr(e, 'identify_discogs', lambda *a: {'labels': ['Nueva'], 'genres': ['House'], 'how': 'master'})
    run = e.Run(True, 'flujo')
    e.process_album(str(album), run)
    for path, title in ((one, 'T1'), (two, 'T2')):
        audio = FLAC(path)
        assert (audio['ARTIST'], audio['ALBUM'], audio['TITLE']) == (['A'], ['B'], [title])
        assert audio['LABEL'] == ['Nueva'] and audio['LABEL_ANTERIOR'] == ['Vieja'] and 'ORGANIZATION' not in audio
        assert audio['GENRE'] == ['House'] and 'REPLAYGAIN_ALBUM_GAIN' in audio
    assert FLAC(one)['REPLAYGAIN_ALBUM_GAIN'] == FLAC(two)['REPLAYGAIN_ALBUM_GAIN']
    lines = open(os.path.join(e.DATA_DIR, 'diario-flujo.jsonl'), encoding='utf-8').read().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])['previous']['ORGANIZATION'] == ['Vieja']


def test_unreadable_album_is_skipped_without_writing(tmp_path):
    good = make_flac(tmp_path / 'disco/1.flac', ARTIST='A', ALBUM='B')
    before = open(good, 'rb').read()
    (tmp_path / 'disco/2.flac').write_bytes(b'\0' * 1000)
    res = e.process_album(str(tmp_path / 'disco'), e.Run(True, 'x'))
    assert 'error' in res and open(good, 'rb').read() == before


def test_ep_suffix_is_ignored_when_comparing_titles():
    assert e.base_title('Silent State EP') == e.base_title('Silent State')


def test_album_that_vanishes_midway_does_not_stop_the_run(tmp_path, monkeypatch, capsys):
    good = make_flac(tmp_path / 'bueno/1.flac', seconds=3, ARTIST='A', ALBUM='B')

    def boom(album, run):
        if 'malo' in album:
            raise FileNotFoundError('desaparecido')
        return {'album': album, 'revisar': []}

    monkeypatch.setattr(e, 'process_album', boom)
    e.cmd_albums([str(tmp_path / 'malo'), os.path.dirname(good)], e.Run(False))
    assert 'desaparecido' in capsys.readouterr().out


def test_inbox_keeps_albums_but_ignores_loose_files_at_its_root(tmp_path, monkeypatch):
    inbox = tmp_path / 'temp/soulseek'
    make_flac(inbox / 'suelta.flac')
    make_flac(inbox / 'Disco/1.flac')
    monkeypatch.setattr(e, 'INBOX_ROOTS', [str(inbox)])
    monkeypatch.setattr(e, 'LOOSE_ROOTS', [])
    assert [g for g, _f, _l in e.iter_groups(str(inbox))] == [str(inbox / 'Disco')]


def test_album_never_analyses_bpm(tmp_path, monkeypatch):
    make_flac(tmp_path / 'disco/1.flac')
    monkeypatch.setattr(e, 'analyse_bpm', lambda path: pytest.fail('no debe analizar BPM'))
    res = e.process_album(str(tmp_path / 'disco'), e.Run(False))
    assert 'bpm' not in res


def test_song_from_an_album_gets_its_own_album_gain_in_mix(tmp_path, run):
    path = make_flac(tmp_path / 'mix/1.flac', REPLAYGAIN_TRACK_GAIN='-3.00 dB', REPLAYGAIN_TRACK_PEAK='0.5',
                     REPLAYGAIN_ALBUM_GAIN='-7.00 dB', REPLAYGAIN_ALBUM_PEAK='0.9')
    assert e.gain_changes(e.song_gain_plan(path)[path]) == {
        'REPLAYGAIN_ALBUM_GAIN': ['-3.00 dB'], 'REPLAYGAIN_ALBUM_PEAK': ['0.5']}


def test_mix_writes_bpm_and_gains_but_no_label_or_genre(tmp_path, monkeypatch, run):
    mix = tmp_path / 'Mix/House'
    path = make_flac(mix / '1.flac', seconds=5, GENRE='Mi genero', ARTIST='A')
    monkeypatch.setattr(e, 'LOOSE_ROOTS', [str(tmp_path / 'Mix')])
    monkeypatch.setattr(e, 'analyse_bpm', lambda p: (122.4, 3.0))
    assert e.cmd_mix([str(mix)], run) == 0
    audio = FLAC(path)
    assert audio['BPM'] == ['122'] and audio['GENRE'] == ['Mi genero'] and 'LABEL' not in audio
    assert audio['REPLAYGAIN_ALBUM_GAIN'] == audio['REPLAYGAIN_TRACK_GAIN']


def test_mix_refuses_paths_outside_mix(tmp_path, monkeypatch, run):
    monkeypatch.setattr(e, 'LOOSE_ROOTS', [str(tmp_path / 'Mix')])
    with pytest.raises(SystemExit):
        e.cmd_mix([str(tmp_path / 'temp/1.flac')], run)


def test_mix_dry_run_changes_nothing(tmp_path, monkeypatch):
    path = make_flac(tmp_path / 'Mix/1.flac', seconds=5)
    monkeypatch.setattr(e, 'LOOSE_ROOTS', [str(tmp_path / 'Mix')])
    monkeypatch.setattr(e, 'analyse_bpm', lambda p: (122.4, 3.0))
    before = open(path, 'rb').read()
    assert e.cmd_mix([path], e.Run(False)) == 0
    assert open(path, 'rb').read() == before
