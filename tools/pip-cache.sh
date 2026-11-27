#!/usr/bin/env bash
# Run pip with the supplied virtual environment's Python.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo 'Usage: bash tools/pip-cache.sh <venv-python> <pip-command> [args...]' >&2
  exit 2
fi

python=$1
shift
if [[ "$(uname -s)" == Darwin && -z "${CI:-}" && -z "${GITHUB_ACTIONS:-}" ]]; then
  PIP_CACHE_DIR="$("$python" - "$(uname -m)" <<'PY'
from pathlib import Path
import sys

volume = Path("/Volumes/工作盘")
cache = (volume / ".缓存" / "依赖" / ("darwin-" + sys.argv[1]) / "pip").resolve()
if not volume.is_mount() or not cache.is_relative_to(volume):
    raise SystemExit("请先挂载工作盘，并确认依赖缓存位于工作盘内。")
print(cache)
PY
)"
  export PIP_CACHE_DIR
  mkdir -p "$PIP_CACHE_DIR"
fi

exec "$python" -m pip "$@"
