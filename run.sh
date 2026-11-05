#!/usr/bin/env bash
# Start Lecture Companion locally. Usage: ./run.sh [port]
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment (needs uv: https://docs.astral.sh/uv/)"
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -e ".[dev]"
fi
PORT="${1:-8765}"
echo "Lecture Companion → http://127.0.0.1:${PORT}"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" --log-level info
