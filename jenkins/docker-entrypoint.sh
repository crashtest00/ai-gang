#!/bin/bash
# Root-then-drop-privileges entrypoint for the Jenkins controller.
#
# Problem: the `docker` group's GID baked into the image at build time
# (via `docker.io`'s postinst) won't generally match the GID that owns
# /var/run/docker.sock on whatever host bind-mounts it in
# (jenkins/docker-compose.yml mounts the host socket directly), and that
# GID varies from host to host. A static `usermod -aG docker jenkins` at
# build time can only ever be right by coincidence.
#
# Fix: run this as root (the image's final stage stays root — see
# Dockerfile), reconcile the in-container `docker` group's GID against the
# actual mounted socket at container start, make sure `jenkins` is a member
# of whichever group owns that GID, then step down to the `jenkins` user via
# gosu to run the real Jenkins startup.
set -euo pipefail

DOCKER_SOCK=/var/run/docker.sock

if [ -S "$DOCKER_SOCK" ]; then
  SOCK_GID="$(stat -c '%g' "$DOCKER_SOCK")"
  EXISTING_GROUP="$(getent group "$SOCK_GID" | cut -d: -f1 || true)"

  if [ -z "$EXISTING_GROUP" ]; then
    # No group in the container currently owns this GID — repoint the
    # `docker` group (created by the docker.io package) at it.
    groupmod -g "$SOCK_GID" docker
    EXISTING_GROUP=docker
  fi

  if [ "$EXISTING_GROUP" != "docker" ]; then
    # Some other group already claims this GID (e.g. it collided with a
    # pre-existing base-image group) — just add jenkins to that group too.
    usermod -aG "$EXISTING_GROUP" jenkins
  else
    usermod -aG docker jenkins
  fi
else
  echo "docker-entrypoint.sh: $DOCKER_SOCK not found — skipping docker group GID sync" >&2
fi

# Hand off to the real Jenkins startup as the unprivileged `jenkins` user.
# The base jenkins/jenkins image's own entrypoint is jenkins.sh; call it
# directly rather than relying on "$@" being it, since setting our own
# ENTRYPOINT above replaces the base image's ENTRYPOINT (CMD, if any, still
# arrives here as "$@").
exec gosu jenkins /usr/local/bin/jenkins.sh "$@"
