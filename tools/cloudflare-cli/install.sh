#!/bin/bash
set -euo pipefail
source_root="$(cd "$(dirname "$0")" && pwd)"
umask 077
base="${STACKORION_RUNTIME_ROOT:-$HOME/.local/share/stackorion}"
case "$base/" in /*/../*|/*/./*) echo "Use an absolute runtime path without dot segments" >&2; exit 1;; /*) ;; *) exit 1;; esac
runtime_root="$base/providers/cloudflare-cli"
cache_root="$base/cache/cloudflare-cli"
mkdir -p "$runtime_root" "$cache_root/tmp" "$cache_root/npm-cache"
test ! -L "$runtime_root"
cp "$source_root/package.json" "$source_root/package-lock.json" "$runtime_root/"
TMPDIR="$cache_root/tmp" npm ci --prefix "$runtime_root" --cache "$cache_root/npm-cache" --no-audit --no-fund
"$runtime_root/node_modules/.bin/cf" --version
