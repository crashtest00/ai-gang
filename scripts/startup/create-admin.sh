#!/usr/bin/env bash
# Step 4 — create the Django admin account the operator writes stories
# with, from the platform .env.
#
# The work-item service is where an operator meets AI Gang, and until this
# account exists there is no way in. `manage.py ensure_admin` (added with
# this flow) is the idempotent form: it creates the account the first time
# and, on every run after that, leaves the existing account's password
# alone. Django's own `createsuperuser --noinput` cannot be used for the
# second run — it fails on an existing username — and re-running startup
# must not fail, and must not silently reset a password either.
#
# The three values are passed to the container as environment variables
# for the life of this one command, so they never land in a compose file,
# an image layer, or this run's log. They are handed over in a file the
# docker client reads, not on its command line: an argument is in the
# host's process table, readable by any local user for as long as the
# call lasts. The file is created 0600 and removed however this script
# ends.

set -euo pipefail
STARTUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$STARTUP_DIR/lib.sh"

begin_step create-admin
require_command docker

admin_user="$(env_value AIGANG_ADMIN_USER)"
admin_email="$(env_value AIGANG_ADMIN_EMAIL)"
admin_password="$(env_value AIGANG_ADMIN_PASSWORD)"

[[ -n "$admin_user" && -n "$admin_email" && -n "$admin_password" ]] \
  || die "AIGANG_ADMIN_USER, AIGANG_ADMIN_EMAIL and AIGANG_ADMIN_PASSWORD must all be set in $AIGANG_ENV_FILE"

env_file="$(mktemp)"
trap 'rm -f "$env_file"' EXIT
chmod 600 "$env_file"
{
  printf 'AIGANG_ADMIN_USER=%s\n' "$admin_user"
  printf 'AIGANG_ADMIN_EMAIL=%s\n' "$admin_email"
  printf 'AIGANG_ADMIN_PASSWORD=%s\n' "$admin_password"
} > "$env_file"

docker exec --env-file "$env_file" \
  work-item-service python manage.py ensure_admin \
  || die "could not create the Django admin account"

rm -f "$env_file"
trap - EXIT

log "Django admin account '$admin_user' is present"
end_step create-admin
