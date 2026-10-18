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
# Each entry: the file in the model's repository, the name it is saved as,
# and its SHA-256. The model is the 8-bit quantised ONNX export.
repo='Alibaba-NLP/gte-modernbert-base'
revision='e7f32e3c00f91d699e8c43b53106206bcc72bb22'
files=(
  'onnx/model_int8.onnx model.onnx bae96b276d342bf86eeee07c1bdbc0c75bb82bf4033941aab7fabc1e33ee3b44'
  'tokenizer.json tokenizer.json 6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30'
)

mkdir -p "$dest"
for entry in "${files[@]}"; do
  read -r path name sum <<<"$entry"
  out="$dest/$name"
  if [ -f "$out" ] && echo "$sum  $out" | sha256sum --check --status; then
    continue
  fi
  curl -fsSL --retry 3 -o "$out.part" "https://huggingface.co/$repo/resolve/$revision/$path"
  echo "$sum  $out.part" | sha256sum --check --status \
    || { echo "fetch-embedding-model.sh: $path does not match its pinned SHA-256" >&2; rm -f "$out.part"; exit 1; }
  mv "$out.part" "$out"
done
echo "$repo@$revision" > "$dest/MODEL"
