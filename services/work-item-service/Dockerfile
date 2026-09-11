# AI Gang Internal Work-Item Service — production/deployment image.
# (the internal-work-item-service design — Django + PostgreSQL.)
#
# Not to be confused with docker-compose.test.yml, which only stands up
# ephemeral Postgres/Redis fixtures for `pytest`. This image runs the
# actual Django app, in three roles selected by the command each
# docker-compose.yml service passes (see docker-compose.yml in this
# directory): the HTTP API (gunicorn), the outbox relay
# (`manage.py relay`), and the Streams command/webhook consumers
# (`manage.py run_consumers`) — kept as separate containers/processes so
# the relay can be killed and restarted independently of the API and
# consumers (REQ-06 Open Question 1; also what AC-02's mid-batch-kill test
# in the V2-E2E-TEST-PLAN design exercises).

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

# Populates STATIC_ROOT (whitenoise serves it at runtime — see
# settings.py's STORAGES comment). Needs no DB/Redis, safe at build time;
# settings.py's env-var defaults are enough for this to run without a
# real .env present during the image build.
RUN python manage.py collectstatic --noinput

EXPOSE 9100

CMD ["gunicorn", "workitemservice.wsgi:application", "--bind", "0.0.0.0:9100"]
