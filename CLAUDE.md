# etiquetador

CLI (`etiquetador.py`), no servidor. Sin `--write` solo genera el informe en `DATA_DIR`.

- Nunca escribe ARTIST, ALBUM ni TITLE: `WRITABLE` es una lista cerrada y `write_flac` rechaza el resto.
- Carpetas por entorno: `TEMP_ROOTS` (discos por procesar), `LOOSE_ROOTS` (canciones sueltas), `INBOX_ROOTS` (lo suelto en la raíz no se procesa).
- Cada `--write` deja `diario-<run>.jsonl`; `deshacer RUN_ID` lo restaura.
- Pruebas: `python -m pytest -q test_etiquetador.py` (dentro de la imagen, que trae ffmpeg y essentia).
