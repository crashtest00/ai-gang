#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DEPLOYMENT=""
FRAMEWORK=""
TARGET_DIR=""

usage() {
  echo "Usage: $0 --target DIR --deployment TYPE [--desktop-framework tauri|electron]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET_DIR="${2:?missing value for --target}"; shift 2 ;;
    --deployment) DEPLOYMENT="${2:?missing value for --deployment}"; shift 2 ;;
    --desktop-framework) FRAMEWORK="${2:?missing value for --desktop-framework}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$TARGET_DIR" ]] || { echo "--target is required" >&2; exit 2; }
[[ -n "$DEPLOYMENT" ]] || { echo "--deployment is required" >&2; exit 2; }

# Boilerplate selection by target deployment — the same branch point is
# also addressable as a graph node,
# setup/graphs/deployment-target-boilerplate.graph.yaml
# (deployment-target-boilerplate#select-target-deployment), for callers
# walking the graph-based initialization procedure instead of invoking this
# script directly.
case "$DEPLOYMENT" in
  web)
    # Scaffold the checked-in, minimal, independently runnable and
    # deployable web boilerplate instead of an empty repository.
    # templates/web/ is the scaffold source; the running,
    # checked-in reference instance lives at projects/hello-web/ (built
    # from the same content, paralleling projects/hello-desktop/).
    if [[ -n "$(ls -A "$TARGET_DIR" 2>/dev/null)" ]]; then
      echo "  $TARGET_DIR is not empty — copying the web boilerplate alongside existing content (no overwrite of same-named files skipped)." >&2
    fi
    mkdir -p "$TARGET_DIR"
    # Copy contents of templates/web/ (including dotfiles) into TARGET_DIR.
    cp -R "$REPO_ROOT/templates/web/." "$TARGET_DIR/"
    echo "  Installed the web boilerplate (templates/web/) into $TARGET_DIR."
    ;;
  desktop)
    if [[ "$FRAMEWORK" != "tauri" && "$FRAMEWORK" != "electron" ]]; then
      echo "desktop deployment requires --desktop-framework tauri|electron" >&2
      exit 2
    fi
    mkdir -p "$TARGET_DIR/.github/workflows"
    cp "$REPO_ROOT/templates/desktop/.github/workflows/build-desktop.yml" \
      "$TARGET_DIR/.github/workflows/build-desktop.yml"
    cp "$REPO_ROOT/templates/desktop/.github/workflows/release-desktop.yml" \
      "$TARGET_DIR/.github/workflows/release-desktop.yml"
    echo "  Installed desktop build and release workflows ($FRAMEWORK)."
    echo "  NOTE: desktop remains an experimental target in V2 — not generalized"
    echo "  further, and outside this feature's acceptance bar."
    ;;
  *)
    # An unsupported target deployment reaches guided remediation, not a
    # silent empty scaffold. This is the same remediation content as
    # setup/graphs/deployment-target-boilerplate.graph.yaml's
    # remediate-unsupported-target node, for a caller running this script
    # directly rather than walking the graph.
    echo "" >&2
    echo "  No boilerplate is defined for deployment target \"$DEPLOYMENT\"." >&2
    echo "  V2 officially supports the web target only; desktop remains available" >&2
    echo "  but experimental. $TARGET_DIR was left empty — this is not a failure," >&2
    echo "  but there is no starting scaffold for this target yet." >&2
    echo "" >&2
    echo "  To add support for a new target: create templates/<target>/ with a" >&2
    echo "  minimal, real, independently runnable and deployable application," >&2
    echo "  extend this script's case statement to scaffold it, and add a branch" >&2
    echo "  to setup/graphs/deployment-target-boilerplate.graph.yaml." >&2
    echo "" >&2
    exit 0
    ;;
esac
