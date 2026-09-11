FROM node:22-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates redis-tools \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
CMD ["sh", "-c", "tail -f /dev/null"]
