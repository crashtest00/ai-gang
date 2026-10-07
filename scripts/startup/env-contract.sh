#!/usr/bin/env bash
# The platform `.env` contract: which variables platform startup requires or
# defaults — which of them it refuses to start without, and what the rest
# default to.
#
# This is the single list of those. scripts/startup/validate-env.sh enforces
# it, scripts/startup/derive-env.sh builds each service's own environment file
# from it, and the test suite checks .env.template against it, so a required
# or defaulted variable cannot be added in one place and forgotten in the
# others.
#
# Not every variable startup reads is here, and the ones that are not need no
# entry. A variable startup copies through when the operator set it and omits
# when they did not is neither required nor defaulted, so there is nothing for
# validate-env.sh to refuse and no default for derive-env.sh to substitute.
# Those are defined at the place that copies them — WEBHOOK_SECRET and the
# JIRA_*_FIELD_ID set, in scripts/startup/derive-env.sh (JIRA_FIELD_ID_VARS and
# the two env_file_get blocks that write services/core/.env) — and documented,
# blank, in .env.template beside them.
#
# Output, one variable per line:
#   REQUIRED <NAME>
#   REQUIRED_IN_METHOD <method> <NAME>
#   OPTIONAL <NAME> <default>
#
# A REQUIRED variable is one .env.template ships empty: the operator must
# fill it in, and a value still equal to the template's is refused.
#
# A REQUIRED_IN_METHOD variable is held to the same rule, but only when the
# platform configuration's `authMethod` (ai-gang.config.json) is <method>.
# It is how the contract expresses the Claude credential: one per supported
# authentication method, and the only place those credential names are
# written. scripts/init-project.sh and scripts/startup/initialize-project.sh
# read the name for the configured method from here.

set -euo pipefail

cat <<'CONTRACT'
REQUIRED_IN_METHOD api-key ANTHROPIC_API_KEY
REQUIRED_IN_METHOD oauth-token CLAUDE_CODE_OAUTH_TOKEN
REQUIRED GH_TOKEN
REQUIRED AIGANG_ADMIN_USER
REQUIRED AIGANG_ADMIN_EMAIL
REQUIRED AIGANG_ADMIN_PASSWORD
REQUIRED PGPASSWORD
REQUIRED DJANGO_SECRET_KEY
OPTIONAL PGUSER workitem
OPTIONAL PGDATABASE workitem
OPTIONAL PGHOST core-postgres
OPTIONAL PGPORT 5432
CONTRACT
