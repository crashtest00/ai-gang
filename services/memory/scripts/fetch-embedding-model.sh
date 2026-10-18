#!/usr/bin/env bash
# Fetch Memory's embedding model into a directory, once, at a pinned revision,
# and verify every file by its SHA-256 (index/embedding.py, build brief I-08).
#
# This is the only place Memory's model is downloaded. Two callers:
#   - the Dockerfile, at image build time, into /opt/memory-embedding-model;
#   - a host test run's one-time setup, into the directory run_tests.sh
#     names (MEMORY_EMBED_MODEL_DIR), when its preflight reports it missing.
# Memory's code never calls it, and never downloads at runtime: a missing
# model is an error (index.embedding.ModelUnavailable).
#
#   scripts/fetch-embedding-model.sh <directory>
set -euo pipefail

dest="${1:?usage: fetch-embedding-model.sh <directory>}"

# Keep in step with index/embedding.py's MODEL.
repo='BAAI/bge-small-en-v1.5'
revision='5c38ec7c405ec4b44b94cc5a9bb96e735b38267a'
files=(
  'onnx/model.onnx 828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35'
  'tokenizer.json d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66'
)

mkdir -p "$dest"
for entry in "${files[@]}"; do
  path="${entry% *}"
  sum="${entry#* }"
  out="$dest/$(basename "$path")"
  if [ -f "$out" ] && echo "$sum  $out" | sha256sum --check --status; then
    continue
  fi
  curl -fsSL --retry 3 -o "$out.part" "https://huggingface.co/$repo/resolve/$revision/$path"
  echo "$sum  $out.part" | sha256sum --check --status \
    || { echo "fetch-embedding-model.sh: $path does not match its pinned SHA-256" >&2; rm -f "$out.part"; exit 1; }
  mv "$out.part" "$out"
done
echo "$repo@$revision" > "$dest/MODEL"
