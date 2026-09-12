#!/usr/bin/env bash
# The deployment target / stack profile -> container image template
# mapping, and nothing else.
#
# `Docker Templates/` is the source of the templates and is not modified;
# this is the one place that says which of them a configured stack
# selects. The supported pairs are the shipped catalog's
# (setup/graphs/engine/lib/config/catalog.js), so a configuration that
# validated has a template here by construction.
#
# Usage: stack-dockerfile.sh <type> <stack>
#   prints the template's path relative to the checkout, or fails naming
#   the pairs it does know.

set -euo pipefail

TYPE="${1:?usage: stack-dockerfile.sh <type> <stack>}"
STACK="${2:?usage: stack-dockerfile.sh <type> <stack>}"

case "${TYPE}/${STACK}" in
  web/node-express)
    # templates/web/ is a Node 22 + Express application, so the project
    # container is built from the Node template.
    printf 'Docker Templates/Dockerfile-node.template\n'
    ;;
  *)
    printf 'stack-dockerfile.sh: no container image template for deployment target "%s" with stack profile "%s" (known: web/node-express)\n' \
      "$TYPE" "$STACK" >&2
    exit 1
    ;;
esac
