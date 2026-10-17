"""Django settings for Memory (Durable Agent Session Record, §4).

Memory is its own Django project with its own PostgreSQL database. Every
connection setting below is Memory's own: `services/memory/.env`, which
scripts/startup/derive-env.sh writes from the platform `.env`'s
MEMORY_PGPASSWORD and MEMORY_DJANGO_SECRET_KEY (REQ-02). It names no Core
database host or credential.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-insecure-memory-dev-key-do-not-use-in-production')
DEBUG = os.environ.get('DJANGO_DEBUG', 'false').lower() in ('1', 'true', 'yes')
ALLOWED_HOSTS = [h.strip() for h in os.environ.get('DJANGO_ALLOWED_HOSTS', '*').split(',') if h.strip()]

INSTALLED_APPS = ['archive', 'extraction', 'index']
MIDDLEWARE = ['django.middleware.common.CommonMiddleware']
ROOT_URLCONF = 'memory.urls'
WSGI_APPLICATION = 'memory.wsgi.application'
APPEND_SLASH = False

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'HOST': os.environ.get('PGHOST', 'memory-postgres'),
        'PORT': os.environ.get('PGPORT', '5432'),
        'USER': os.environ.get('PGUSER', 'memory'),
        'PASSWORD': os.environ.get('PGPASSWORD', ''),
        'NAME': os.environ.get('PGDATABASE', 'memory'),
    }
}

USE_TZ = True
TIME_ZONE = 'UTC'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- The archive ---------------------------------------------------------
# The shared harness volume, mounted read-only by memory-copier; each
# project is one subdirectory of it (REQ-01, §4).
MEMORY_HARNESS_ROOT = os.environ.get('MEMORY_HARNESS_ROOT', '/harness')
# Memory's own archive volume (REQ-02).
MEMORY_ARCHIVE_ROOT = os.environ.get('MEMORY_ARCHIVE_ROOT', '/var/lib/aigang/memory/archive')

# REQ-06: a session none of whose files has grown for 35 minutes is quiet —
# the subscriber's 30-minute bound on a run plus margin. Fixed by the
# specification, not configurable.
MEMORY_QUIET_INTERVAL_SECONDS = 35 * 60


def _seconds(name, default):
    raw = os.environ.get(name, '')
    if raw == '':
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ImproperlyConfigured(f'{name} must be a number of seconds, not {raw!r}')
    if value <= 0:
        raise ImproperlyConfigured(f'{name} must be greater than zero, not {raw!r}')
    return value


# REQ-03: the reconciling pass's interval, default 30 minutes. It must stay
# below the quiet interval, so a session still growing is seen to grow
# before it could be marked quiet when the events fail; Memory refuses to
# start otherwise.
MEMORY_RECONCILE_INTERVAL_SECONDS = _seconds('MEMORY_RECONCILE_INTERVAL_SECONDS', 30 * 60)
if MEMORY_RECONCILE_INTERVAL_SECONDS >= MEMORY_QUIET_INTERVAL_SECONDS:
    raise ImproperlyConfigured(
        f'MEMORY_RECONCILE_INTERVAL_SECONDS is {MEMORY_RECONCILE_INTERVAL_SECONDS:g}; it must be below '
        f'the quiet interval of {MEMORY_QUIET_INTERVAL_SECONDS} seconds (35 minutes). Memory refuses to start.')

# How often memory-copier checks for sessions that have gone quiet by the
# interval, between passes. A database query, not a scan of the volume.
MEMORY_QUIET_SWEEP_SECONDS = _seconds('MEMORY_QUIET_SWEEP_SECONDS', 60)
# How long a burst of file-change events is left to settle before copying.
MEMORY_EVENT_SETTLE_SECONDS = _seconds('MEMORY_EVENT_SETTLE_SECONDS', 0.5)
# REQ-03's Acceptance runs with the watcher disabled.
MEMORY_WATCHER_ENABLED = os.environ.get('MEMORY_WATCHER_ENABLED', 'true').lower() not in ('0', 'false', 'no')
# Caps the watches memory-copier adds (unset: as many as the kernel allows).
MEMORY_MAX_WATCHES = int(os.environ['MEMORY_MAX_WATCHES']) if os.environ.get('MEMORY_MAX_WATCHES') else None

# --- Artifact text extraction (artifact-text-extraction.md) ------------------
# memory-extraction reads artifacts from Core's HTTP read surface, never from
# Core's database (REQ-01; Durable Agent Session Record REQ-02), and upload
# events from Redis. Both defaults are the deployment's addresses on the
# `ai-gang` network.
MEMORY_CORE_URL = os.environ.get('MEMORY_CORE_URL', 'http://core:9100').rstrip('/')
MEMORY_REDIS_URL = os.environ.get('MEMORY_REDIS_URL', 'redis://ai-gang-redis:6379')
# The spool shared with the extractor process, its only interface (REQ-05).
MEMORY_EXTRACTION_SPOOL = os.environ.get('MEMORY_EXTRACTION_SPOOL', '/var/lib/aigang/memory/extraction-spool')
# REQ-06's declared time limit for one file's extraction (Open Question 2).
# The extractor enforces it on each file's child process.
MEMORY_EXTRACTION_TIME_LIMIT_SECONDS = _seconds('MEMORY_EXTRACTION_TIME_LIMIT_SECONDS', 120)
# Brief I-26: how many attempts on the same content may end without a result
# (the extractor or the worker stopped mid-file, REQ-07) before the file is
# recorded as a failure (REQ-06) instead of being tried again.
MEMORY_EXTRACTION_ATTEMPT_LIMIT = int(_seconds('MEMORY_EXTRACTION_ATTEMPT_LIMIT', 3))
# How long beyond the time limit memory-extraction waits for the extractor:
# for it to claim a file, and for its result once claimed.
MEMORY_EXTRACTION_GRACE_SECONDS = _seconds('MEMORY_EXTRACTION_GRACE_SECONDS', 30)
# REQ-04's reconciliation sweep interval: the detection bound for in-place
# edits, lost events and files uploaded before extraction existed.
MEMORY_EXTRACTION_SWEEP_SECONDS = _seconds('MEMORY_EXTRACTION_SWEEP_SECONDS', 30 * 60)
# Names this worker's claims on jobs and its Redis consumer. Unset: the
# container's hostname, which survives a restart of the same container, so a
# restarted worker resumes its own unfinished job at once (REQ-07).
MEMORY_EXTRACTION_WORKER_ID = os.environ.get('MEMORY_EXTRACTION_WORKER_ID', '')

# --- Corpus indexing (corpus-indexing.md) ------------------------------------
# memory-indexer reads Core's enumerations (MEMORY_CORE_URL), Core's event
# streams (MEMORY_REDIS_URL), Memory's archive through memory-api's reads
# (never the harness volume, REQ-01), the extraction record in this
# database, and the platform corpus from the deployment's Source checkout,
# mounted read-only (REQ-08).
MEMORY_API_URL = os.environ.get('MEMORY_API_URL', 'http://memory-api:9200').rstrip('/')
MEMORY_PLATFORM_CORPUS_ROOT = os.environ.get('MEMORY_PLATFORM_CORPUS_ROOT', '/platform-source')
# How often the archive's session listing and notes read are followed
# (REQ-04's exception: sessions are Memory's own archive, announced by no
# event), and how often the extraction record is compared with the index.
MEMORY_INDEX_ARCHIVE_FOLLOW_SECONDS = _seconds('MEMORY_INDEX_ARCHIVE_FOLLOW_SECONDS', 5)
MEMORY_INDEX_LIBRARY_FOLLOW_SECONDS = _seconds('MEMORY_INDEX_LIBRARY_FOLLOW_SECONDS', 5)
# How often a new project's work-item event stream is looked for.
MEMORY_INDEX_STREAM_DISCOVERY_SECONDS = _seconds('MEMORY_INDEX_STREAM_DISCOVERY_SECONDS', 30)
# REQ-09: how often recorded failures are retried without a rebuild.
MEMORY_INDEX_RETRY_SECONDS = _seconds('MEMORY_INDEX_RETRY_SECONDS', 300)
# Names the indexer's Redis consumer. Unset: the container's hostname, which
# survives a restart, so a restarted indexer finishes its own unacknowledged
# entries first (REQ-04).
MEMORY_INDEX_CONSUMER = os.environ.get('MEMORY_INDEX_CONSUMER', '')

# --- Logging -------------------------------------------------------------
# REQ-08: every read is logged, to stderr, which the container log collects.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {'plain': {'format': '%(asctime)s %(name)s %(levelname)s %(message)s'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'formatter': 'plain'}},
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False}},
}
