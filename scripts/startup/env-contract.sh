#!/usr/bin/env bash
# The platform `.env` contract: which variables platform startup reads,
# which of them it refuses to start without, and what the rest default to.
#
# This is the single list. scripts/startup/validate-env.sh enforces it,
# scripts/startup/derive-env.sh builds each service's own environment file
# from it, and the test suite checks .env.template against it, so a
# variable cannot be added in one place and forgotten in the others.
#
# Output, one variable per line:
#   REQUIRED <NAME>
#   OPTIONAL <NAME> <default>
#
# A REQUIRED variable is one .env.template ships empty: the operator must
# fill it in, and a value still equal to the template's is refused.

set -euo pipefail

cat <<'CONTRACT'
REQUIRED ANTHROPIC_API_KEY
REQUIRED GH_TOKEN
REQUIRED AIGANG_ADMIN_USER
REQUIRED AIGANG_ADMIN_EMAIL
REQUIRED AIGANG_ADMIN_PASSWORD
REQUIRED PGPASSWORD
REQUIRED DJANGO_SECRET_KEY
OPTIONAL PGUSER workitem
OPTIONAL PGDATABASE workitem
OPTIONAL PGHOST workitem-postgres
OPTIONAL PGPORT 5432
CONTRACT
