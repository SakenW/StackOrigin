#!/bin/bash
set -euo pipefail
test "$(uname -s)" = Darwin
test "$(uname -m)" = arm64
umask 077
base="${STACKORION_RUNTIME_ROOT:-$HOME/.local/share/stackorion}"
case "$base/" in /*/../*|/*/./*) echo "Use an absolute runtime path without dot segments" >&2; exit 1;; /*) ;; *) exit 1;; esac
runtime_root="$base/providers/qshell"
cache_root="$base/cache/qshell"
mkdir -p "$runtime_root" "$cache_root"
test ! -L "$runtime_root"
archive="$cache_root/qshell-v2.22.1-darwin-arm64.tar.gz"
curl -fsSL 'https://github.com/qiniu/qshell/releases/download/v2.22.1/qshell-v2.22.1-darwin-arm64.tar.gz' -o "$archive"
test "$(shasum -a 256 "$archive" | cut -d ' ' -f 1)" = '94b3aa826bfe670d622741b65862bf14aa4ad1bab6ddaf497c6a9892ed40432b'
tar -xzf "$archive" -C "$runtime_root" qshell
test "$(shasum -a 256 "$runtime_root/qshell" | cut -d ' ' -f 1)" = '3157d0c873bc17d0ddd7968440e7a8bbe8fa0aa661f03f8be4b037f0f0d14476'
"$runtime_root/qshell" -v
