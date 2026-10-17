# The extractor (artifact-text-extraction.md REQ-05): its own image, with
# no Django, no database driver and no Redis client. Everything a parser
# needs is installed here at build time; at run time the container has no
# network at all (docker-compose.yml, `network_mode: none`), so nothing
# could be fetched even by mistake (brief I-08).

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./extractor/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python", "-m", "extractor", "--spool", "/spool"]
