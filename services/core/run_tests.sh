#!/usr/bin/env bash
# Runs the Django/pytest test suite against the real Postgres+Redis test
# containers (docker-compose.test.yml) and the same scrummaster fixture
# catalog scrummaster's own tests use. Mirrors the Node reference
# implementation's `npm test` invocation environment
# (test/helpers/testDb.js) — see conftest.py for the equivalent env-var
# defaults this script makes explicit instead of relying on.
set -euo pipefail
cd "$(dirname "$0")"

export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-15432}"
export PGUSER="${PGUSER:-workitem}"
export PGPASSWORD="${PGPASSWORD:-workitem}"
export PGDATABASE="${PGDATABASE:-workitem}"
export REDIS_TEST_URL="${REDIS_TEST_URL:-redis://localhost:16399}"
export REDIS_URL="${REDIS_URL:-$REDIS_TEST_URL}"
export AGENTS_CATALOG_PATH="${AGENTS_CATALOG_PATH:-$(pwd)/../scrummaster/test/fixtures/agents.json}"
export PROJECTS_CONFIG_PATH="${PROJECTS_CONFIG_PATH:-$(pwd)/../scrummaster/test/fixtures/projects.json}"

# Refuses the run with a named error, before pytest, when this worktree has not
# been set up or a killed run has wedged the shared test database — the two
# failures that arrive as exit 127, exit 4 or a database-setup error and look
# like anything but what they are. Exit 78, outside pytest's 0-5. Runs after
# the exports above because it uses them. See preflight.sh.
./preflight.sh

.venv/bin/python -m pytest "$@"
