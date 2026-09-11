#!/usr/bin/env bash
#
# Installs Docker Engine + the Compose plugin via Docker's own official
# convenience script (get.docker.com), so this never has to track Docker's
# release process or per-distro package names itself — the same approach
# setup-beta-vm.sh already uses for the Beta VM's rootless install.
#
# Usage: ./scripts/install-docker.sh
#   - Idempotent: skips the install if `docker` is already on PATH.
#   - Adds the invoking user to the `docker` group. That only takes effect
#     on a fresh login (or `newgrp docker`), so verification below runs via
#     sudo instead of waiting on that.

set -euo pipefail

if command -v docker &>/dev/null; then
  echo "Docker already installed: $(docker --version)"
else
  echo "Installing Docker via get.docker.com..."
  curl -fsSL https://get.docker.com | sh
fi

sudo systemctl enable --now docker

NEEDS_RELOGIN=0
if ! id -nG "$USER" | grep -qw docker; then
  sudo usermod -aG docker "$USER"
  NEEDS_RELOGIN=1
fi

echo "--- verifying (via sudo — a docker-group change needs a fresh login first) ---"
sudo docker --version
sudo docker compose version
sudo docker run --rm hello-world >/dev/null && echo "docker run hello-world: OK"

if [[ "$NEEDS_RELOGIN" == "1" ]]; then
  echo
  echo "Added $USER to the docker group — an interactive shell needs to log"
  echo "out/in (or run 'newgrp docker') to pick that up. A non-interactive"
  echo "tool session (e.g. Claude Code's Bash tool) is a separate shell that"
  echo "started before this change and can't be logged out/in — for that,"
  echo "wrap each docker call instead: sg docker -c \"docker ...\". That"
  echo "applies the docker group per-invocation with no session restart."
fi
