FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY etiquetador.py test_etiquetador.py ./

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["nice", "-n", "19", "ionice", "-c", "3", "python", "etiquetador.py"]
