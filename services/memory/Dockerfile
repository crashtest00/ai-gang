# AI Gang Memory — one image for memory-api, memory-copier and the one-shot
# migrate (Durable Agent Session Record §4), selected by the command each
# docker-compose.yml service passes. Packaged the way services/core/ is.
#
# git: memory-indexer reads the platform corpus from the Source checkout's
# git history (corpus-indexing.md REQ-08). The index uses PostgreSQL's
# full-text search, so the image carries no model weights and fetches none
# at runtime (build brief I-08).

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 9200

CMD ["gunicorn", "memory.wsgi:application", "--bind", "0.0.0.0:9200"]
