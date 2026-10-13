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

INSTALLED_APPS = ['archive']
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
