# The AI Gang container — the one container `docker compose up` at this
# repository's root builds and starts.
#
# It is a client of the host's Docker daemon, not a host for a second one.
# The root docker-compose.yml gives it the daemon's socket and the
# operator's checkout at the checkout's own path; its entrypoint validates
# the configuration and the environment file, then runs the Initialization
# Agent, which brings AI Gang's service containers up as siblings on that
# same daemon. There is no Docker-in-Docker here and nothing runs nested.
#
# What the image has to carry, and why:
#   - Claude Code — the Initialization Agent is Claude Code.
#   - the Docker CLI and the Compose plugin — every service is started
#     through its own existing compose file.
#   - git and gh — scripts/init-project.sh pushes the project's first
#     commit and applies branch protection.
#   - node — the configuration validator is
#     setup/graphs/engine/lib/config/, and comes with the base image.
#   - jq — scripts/init-project.sh and the status record both need it.

FROM node:22-bookworm-slim

# Docker's own apt repository, for a CLI and Compose plugin that track
# Docker's releases rather than Debian's.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli.gpg \
    && chmod a+r /etc/apt/keyrings/githubcli.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
         docker-ce-cli \
         docker-compose-plugin \
         gh \
         git \
         jq \
         openssl \
         procps \
         util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# A git identity for the container, and the name of the branch `git init`
# creates. Initialization makes the project's first commit before it
# pushes, and git refuses to commit without an identity — so without one,
# project initialization fails on every fresh container, before the
# remote is ever contacted. Unset, git's built-in default would also make
# the project's first branch `master`, with dev/beta/prod cut from it.
# System-level, so an operator's own global or per-repository settings
# still win where they set them.
RUN git config --system user.name "AI Gang" \
    && git config --system user.email "ai-gang@localhost" \
    && git config --system init.defaultBranch main

# The entrypoint is baked in; every step it runs comes from the mounted
# checkout, so an operator's own checkout is what executes.
COPY scripts/startup/entrypoint.sh /opt/ai-gang/entrypoint.sh
RUN chmod +x /opt/ai-gang/entrypoint.sh

# A home for the unprivileged user the entrypoint switches to. The
# entrypoint chowns it to the checkout's own owner at run time, while it
# is still root, so this only has to be somebody's: a placeholder uid and
# the ordinary 0755, rather than a world-writable directory shipped in
# the image.
RUN mkdir -p /home/aigang \
    && chown 1000:1000 /home/aigang \
    && chmod 0755 /home/aigang
ENV HOME=/home/aigang

ENTRYPOINT ["/opt/ai-gang/entrypoint.sh"]
