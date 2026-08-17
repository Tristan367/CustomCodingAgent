#!/usr/bin/env bash
# Start the MyriadCode server.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8219}"
[ -d .venv ] || { echo "No .venv found. Run: uv venv && uv pip install -r requirements.txt"; exit 1; }

# Free the port if a previous run is still holding it. -sTCP:LISTEN matters:
# without it this also matches browsers connected to the port and kills them.
if command -v lsof >/dev/null 2>&1; then
  lsof -ti:"$PORT" -sTCP:LISTEN 2>/dev/null | xargs -r kill 2>/dev/null || true
  sleep 0.5
fi

exec .venv/bin/python -m uvicorn agent_server.main:app \
  --host "${HOST:-127.0.0.1}" --port "$PORT" "$@"
