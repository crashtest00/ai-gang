FROM jenkins/jenkins:lts-jdk21

USER root

RUN apt-get update && apt-get install -y \
    docker.io \
    curl \
    openssh-client \
    gosu \
    redis-tools \
    jq \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI — used by the release-flow pipelines to cut release/<sha> PRs
# and merge them into prod (release-candidate, production-promote jobs).
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Node.js — per-project Jenkinsfiles run `npm ci`/`npm run build` directly on
# this controller (agent any, no separate build agent), so it needs its own
# Node toolchain. Version 22 matches Docker Templates/Dockerfile-node.template.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# `redis` npm package for setup/lib/gateway-publish.js, which the Jenkinsfile
# invokes to publish the pipeline_retry message durably onto the gateway
# Streams transport instead of legacy Pub/Sub.
# gateway-publish.js itself reaches this image via setup/ being bind-mounted
# read-only at /agent-docs (jenkins/docker-compose.yml), the same mount every
# project container already uses for its own agent tooling. NODE_PATH must
# point at THIS image's actual global npm root (`npm root -g`) for
# `require('redis')` to resolve -- this NodeSource/Debian install uses
# /usr/lib/node_modules, not the /usr/local/lib/node_modules the
# node:22-alpine-based project containers use (Docker Templates/
# Dockerfile-node.template); confirmed by running `npm root -g` in the built
# image rather than assumed from that other template.
RUN npm install -g redis
ENV NODE_PATH=/usr/lib/node_modules

# Allow Jenkins to talk to the host Docker socket. The `docker` group's GID
# baked in here at build time won't generally match the GID of whatever
# /var/run/docker.sock is bind-mounted at container start (it varies host to
# host), so a static `usermod -aG docker jenkins` at build time silently
# breaks whenever they differ. That reconciliation now happens at runtime
# instead, in docker-entrypoint.sh (below): as root, it fixes up the
# `docker` group's GID against the mounted socket and ensures `jenkins` is a
# member, then drops to the `jenkins` user via gosu.

# Skip setup wizard — JCasC handles configuration
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false -Xmx512m -Xms256m"

# Install plugins at build time
COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

# Root-then-drop-privileges entrypoint: fixes up the docker.sock GID at
# container start, then execs the real Jenkins startup as the `jenkins`
# user via gosu. Must run as root, so no `USER jenkins` here — the
# entrypoint itself does the privilege drop.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Keep tini as PID 1 (as the base jenkins/jenkins image does) for correct
# signal forwarding and zombie reaping — Jenkins pipeline `sh` steps spawn
# lots of subprocesses. tini runs docker-entrypoint.sh as its child (still
# root at this point); the script does its GID fixup, then execs gosu to
# drop to `jenkins` for the actual Jenkins process.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
