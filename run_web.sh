#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${WEB_HOST:-0.0.0.0}"
PORT="${WEB_PORT:-8000}"
export AUTH_RESET_ON_START="${AUTH_RESET_ON_START:-false}"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"

if [[ -n "${WEB_PYTHON:-}" ]]; then
  PYTHON_BIN="$WEB_PYTHON"
elif [[ -x "$VENV_PYTHON" ]]; then
  PYTHON_BIN="$VENV_PYTHON"
  export PATH="$ROOT_DIR/.venv/bin:$PATH"
else
  PYTHON_BIN="python3"
fi
export PYTHON_BIN

if ! "$PYTHON_BIN" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
  echo "Missing Web dependencies for: $PYTHON_BIN" >&2
  echo "Install them with:" >&2
  echo "  python3 -m venv \"$ROOT_DIR/.venv\"" >&2
  echo "  \"$ROOT_DIR/.venv/bin/python\" -m pip install -r \"$ROOT_DIR/requirements.txt\"" >&2
  exit 1
fi

exec "$PYTHON_BIN" -m uvicorn app:app --app-dir "$ROOT_DIR" --host "$HOST" --port "$PORT"
