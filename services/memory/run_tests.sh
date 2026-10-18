#!/usr/bin/env bash
# Runs Memory's pytest suite against the shared test PostgreSQL container
# (services/core/docker-compose.test.yml's workitem-test-postgres-django),
# in a database of Memory's own: pytest-django creates test_memory there and
# never touches Core's test_workitem.
#
# Memory has its own virtualenv, services/memory/.venv (git-ignored), never
# services/core/.venv, which every worktree shares. Setup is diagnosed, not
# performed, as Core's preflight does: a test command reports, it does not
# install.
#
# Run it the documented way, under the shared suite lock:
#   flock /tmp/v4-wis-suite.lock timeout 15m ./run_tests.sh -q
set -euo pipefail
cd "$(dirname "$0")"

export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-15432}"
export PGUSER="${PGUSER:-workitem}"
export PGPASSWORD="${PGPASSWORD:-workitem}"
export PGDATABASE="${PGDATABASE:-memory}"

# The embedding model the suite runs (index/embedding.py). Tests read it from
# here and never download it; the one-time setup below fetches it.
export MEMORY_EMBED_MODEL_DIR="${MEMORY_EMBED_MODEL_DIR:-/tmp/aigang-memory-embedding-model/bge-small-en-v1.5-5c38ec7}"
export HF_HUB_OFFLINE=1

EX_PREFLIGHT=78
if [ ! -x .venv/bin/python ] \
   || ! .venv/bin/python -c 'import django, psycopg2, pytest, pytest_django, onnxruntime, tokenizers, numpy' 2>/dev/null; then
  cat >&2 <<'MSG'

run_tests.sh preflight: services/memory/.venv is missing or lacks requirements.txt's packages.
Set it up, from services/memory:

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt

MSG
  exit "$EX_PREFLIGHT"
fi

if [ ! -f "$MEMORY_EMBED_MODEL_DIR/model.onnx" ] || [ ! -f "$MEMORY_EMBED_MODEL_DIR/tokenizer.json" ]; then
  cat >&2 <<MSG

run_tests.sh preflight: the embedding model is not in $MEMORY_EMBED_MODEL_DIR.
Fetch it once (pinned revision, SHA-256 checked), from services/memory:

    scripts/fetch-embedding-model.sh $MEMORY_EMBED_MODEL_DIR

MSG
  exit "$EX_PREFLIGHT"
fi

.venv/bin/python -m pytest "$@"
