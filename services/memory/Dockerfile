# AI Gang Memory — one image (aigang-memory) for memory-api, memory-copier,
# memory-extraction, memory-indexer and the one-shot migrate (Durable Agent
# Session Record §4), selected by the command each
# docker-compose.yml service passes. Packaged the way services/core/ is.
#
# git: memory-indexer reads the platform corpus from the Source checkout's
# git history (corpus-indexing.md REQ-08).
#
# The embedding model (index/embedding.py) is baked in at build time, at a
# pinned revision verified by SHA-256 (scripts/fetch-embedding-model.sh), into
# MEMORY_EMBED_MODEL_DIR. Nothing fetches it at runtime (build brief I-08):
# Memory's code has no download path, and HF_HUB_OFFLINE is set in case any
# library would try.
#
# Nothing calls out either. ONNX Runtime's Linux wheel uploads usage
# telemetry to mobile.events.data.microsoft.com from a native thread unless
# ORT_DISABLE_TELEMETRY=1 is in the environment when it is imported
# (corpus-indexing.md REQ-06, knowledge-retrieval.md REQ-08).
# index/embedding.py sets it before its import; ENV below sets it for every
# process in the image as well.

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/fetch-embedding-model.sh /tmp/fetch-embedding-model.sh
RUN /tmp/fetch-embedding-model.sh /opt/memory-embedding-model && rm /tmp/fetch-embedding-model.sh

COPY . .

ENV PYTHONUNBUFFERED=1 \
    MEMORY_EMBED_MODEL_DIR=/opt/memory-embedding-model \
    HF_HUB_OFFLINE=1 \
    ORT_DISABLE_TELEMETRY=1

EXPOSE 9200

CMD ["gunicorn", "memory.wsgi:application", "--bind", "0.0.0.0:9200"]
