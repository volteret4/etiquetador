# etiquetador

Completa las etiquetas que suelen faltar en la música descargada: **sello original**, **género**, **ReplayGain** y, en canciones sueltas, **BPM**. Nunca toca artista, álbum ni título: solo puede escribir una lista cerrada de etiquetas (`WRITABLE` en `etiquetador.py`).

Sin `--write` no cambia nada: genera un informe con lo que haría. Con `--write`, cada ejecución guarda un diario para poder deshacerla.

| Orden | Sobre qué | Qué hace |
|---|---|---|
| `pendientes [--limit N] [--write]` | Discos de `TEMP_ROOTS` aún no procesados | Sello original, género y ReplayGain. Se salta lo ya procesado y lo modificado hace menos de 10 min |
| `album RUTA... [--write]` | Discos concretos | Lo mismo que `pendientes` |
| `mix RUTA... [--write]` | Canciones dentro de `LOOSE_ROOTS` | BPM si falta y ReplayGain con álbum = pista. Nunca sello ni género. Rechaza rutas fuera de esas carpetas |
| `ganancia RUTA... [--write] [--limit N]` | Árboles completos | Solo ReplayGain. Sin `--write` cuenta lo que falta sin medir |
| `deshacer RUN_ID` | Una ejecución | Restaura los FLAC desde su diario |

- **Sello original:** el de la primera edición oficial del disco en MusicBrainz (y en Discogs, si das `DISCOGS_TOKEN`), no el de la reedición que tengas. El sello anterior se guarda en `LABEL_ANTERIOR`.
- **Género:** los de MusicBrainz con al menos el 90 % de los votos del primero, como Picard, o los estilos de Discogs; hasta 5.
- Si MusicBrainz y Discogs se contradicen, no se escribe: queda en el informe para revisarlo.
- **ReplayGain 2.0** (−18 LUFS), medido con ffmpeg.
- **BPM** con Essentia; si la confianza es baja, no se escribe.

Escribe solo en FLAC. Conserva la fecha de modificación de los ficheros y se detiene si queda menos de 1 GiB libre.

## Instalación

Necesitas Docker.

```bash
git clone https://github.com/volteret4/etiquetador.git && cd etiquetador && cp .env.example .env
```

En `.env`, pon en `MUSICA` tu carpeta de música y en `TEMP_ROOTS` las subcarpetas de discos por procesar, con las rutas vistas desde dentro (`/music/...`). Construye la imagen:

```bash
docker compose build
```

Primero, un informe sin escribir nada:

```bash
docker compose run --rm etiquetador pendientes --limit 5
```

El informe queda en `data/informe-<run>.json`. Si te convence, vacía `MODO_MUSICA` en `.env` para montar la música con escritura y repite con `--write`:

```bash
docker compose run --rm etiquetador pendientes --limit 5 --write
```

Para deshacer una ejecución:

```bash
docker compose run --rm etiquetador deshacer RUN_ID
```

## Reglas de carpetas

- Las carpetas `Disc N` o `CD N` son parte del mismo disco.
- En `LOOSE_ROOTS`, cada canción es su propio álbum.
- En `INBOX_ROOTS`, lo suelto en la raíz no se procesa: se supone que lo revisas a mano.
- `mix` iguala la ganancia de álbum a la de pista aunque la canción traiga la de su disco. `ganancia` no toca una ganancia de álbum ya presente.

## Datos

`data/` guarda `estado.json` (lo ya procesado), `informe-<run>.json` y `diario-<run>.jsonl`.

## Pruebas

```bash
docker compose run --rm --entrypoint python etiquetador -m pytest -q test_etiquetador.py
```

Es un módulo de [REPEAT-ONE](https://volteret4.github.io/repeat-one-web/), y funciona igual por separado.
