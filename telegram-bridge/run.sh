#!/bin/zsh
set -euo pipefail
cd -- "${0:A:h}"
export UV_CACHE_DIR="${PWD:h}/.runtime/cache/uv"
if [[ ! -x .venv/bin/tg-bridge ]]; then
  uv sync --python 3.13
fi
exec .venv/bin/tg-bridge run
