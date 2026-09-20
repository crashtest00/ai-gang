"""
Django settings for the AI Gang Internal Work-Item Service.

This service is implemented as a Django application, with PostgreSQL as
its datastore. Django is expected to also host the human-facing admin UI.

This service is the ONLY component that holds credentials/a client
library for its own datastore — every setting below that names a secret or
connection string is local to this settings module.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-workitemservice-dev-key-do-not-use-in-production',
)

DEBUG = os.environ.get('DJANGO_DEBUG', 'false').lower() in ('1', 'true', 'yes')

ALLOWED_HOSTS = [h.strip() for h in os.environ.get('DJANGO_ALLOWED_HOSTS', '*').split(',') if h.strip()]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.postgres',
    'workitems',
    'artifacts',
    'librarian',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'workitemservice.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'workitemservice.wsgi.application'


# --- Database -----------------------------------------------------------
# Mirrors the Node implementation's db.js precedence exactly: a single
# DATABASE_URL wins if set, otherwise PGHOST/PGPORT/PGUSER/PGPASSWORD/
# PGDATABASE (defaults match the old service's local-dev defaults so
# existing deploy tooling/env files keep working unchanged).

def _database_config():
    database_url = os.environ.get('DATABASE_URL')
    if database_url:
        try:
            import dj_database_url  # optional; not a hard dependency
            return dj_database_url.parse(database_url)
        except ImportError:
            pass
    return {
        'ENGINE': 'django.db.backends.postgresql',
        'HOST': os.environ.get('PGHOST', 'localhost'),
        'PORT': os.environ.get('PGPORT', '5432'),
        'USER': os.environ.get('PGUSER', 'workitem'),
        'PASSWORD': os.environ.get('PGPASSWORD', 'workitem'),
        'NAME': os.environ.get('PGDATABASE', 'workitem'),
    }


DATABASES = {'default': _database_config()}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# gunicorn (this service's own WSGI server, see docker-compose.yml) never
# serves static files itself, and DEBUG=False disables Django's dev-server
# auto-serving — with neither, django-admin's CSS/JS 404s and the admin UI
# renders unstyled. WhiteNoise (middleware above) serves STATIC_ROOT
# directly from the WSGI app, no separate nginx/CDN needed for this
# service's scale. Requires `collectstatic` to have populated STATIC_ROOT
# — see Dockerfile.
STORAGES = {
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# --- Internal Work-Item Service configuration ---------------------------
# Redis (Streams command channel / event stream / webhook consumer). Same
# env var names and default as the Node implementation's src/redis.js.
REDIS_URL = os.environ.get(
    'REDIS_URL',
    f"redis://{os.environ.get('REDIS_HOST', 'localhost')}:{os.environ.get('REDIS_PORT', '6379')}",
)

# Catalog-backed agent-assignment validation reads the SAME
# on-disk catalog files ScrumMaster's own registry.js reads — see
# workitems/registry.py's module comment for why this is a deliberate
# reimplementation, not a runtime call into ScrumMaster.
AGENTS_CATALOG_PATH = os.environ.get('AGENTS_CATALOG_PATH', '/app/config/agents.json')
PROJECTS_CONFIG_PATH = os.environ.get('PROJECTS_CONFIG_PATH', '/app/config/projects.json')

WORKITEM_HTTP_PORT = int(os.environ.get('PORT', '9100'))
WORKITEM_CONSUMER_ID = os.environ.get('WORKITEM_CONSUMER_ID') or os.uname().nodename

RELAY_POLL_INTERVAL_MS = int(os.environ.get('RELAY_POLL_INTERVAL_MS', '1000'))
RELAY_BATCH_SIZE = int(os.environ.get('RELAY_BATCH_SIZE', '20'))
RELAY_ROW_DELAY_MS = int(os.environ.get('RELAY_ROW_DELAY_MS', '0'))  # test-only knob, see workitems/relay.py


# --- Artifact ingress ----------------------------------------------------
# The root of the mounted volume that holds artifact files. This is the
# only authoritative copy of an artifact's bytes: a read resolves against
# the file here, and a file edited in place from the host is returned by
# the next read. Mounted as the named volume `artifact-data` in
# docker-compose.yml; the test suite points it at a temp directory instead.
# Layout under this root is documented in artifacts/README.md.
ARTIFACT_ROOT = os.environ.get('ARTIFACT_ROOT', '/var/lib/aigang/artifacts')


# --- Librarian (artifact delivery on request) ----------------------------
# The root of the mounted projects directory. Every project repository is
# one directory directly under it, which is what a delivery request's
# `destination_repo` names (librarian/README.md). Mounted read-write in
# docker-compose.yml (the librarian writes delivered files into a
# repository's working tree) from the Source checkout's own `projects/`
# directory, alongside the artifact volume mounted read-only.
PROJECTS_ROOT = os.environ.get('PROJECTS_ROOT', '/var/lib/aigang/projects')

# Test-only: milliseconds the librarian sleeps while holding its
# per-(artifact, repository) advisory lock, so a test can make two
# requests genuinely overlap. Zero in every deployment — the same knob
# shape as RELAY_ROW_DELAY_MS above.
LIBRARIAN_LOCK_HOLD_DELAY_MS = int(os.environ.get('LIBRARIAN_LOCK_HOLD_DELAY_MS', '0'))


# --- Logging -------------------------------------------------------------
# Left unset, Django falls back to its own default logging config, whose
# console handler is filtered to require_debug_true — with DEBUG off (as
# in every real deployment of this service), an unhandled exception in a
# request is reported nowhere at all: no traceback on screen (DEBUG is
# off), and none in the container's own logs either (the one handler that
# would have written it there is disabled). This minimal config restores
# just enough to fix that: an unhandled 500 (django.request, which Django's
# own handler always logs at ERROR before returning the response) reaches
# stderr regardless of DEBUG, which is what the container log collector
# actually captures.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django.request': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
    },
}
