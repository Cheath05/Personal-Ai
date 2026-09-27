#!/usr/bin/env bash
# Run Cardinal locally: API + web app on http://localhost:8000
# Pass --lan to let your phone on the same Wi-Fi reach it (no login yet, so only on a network you trust).
set -euo pipefail
cd "$(dirname "$0")/../services/api"
HOST=127.0.0.1
[[ "${1:-}" == "--lan" ]] && HOST=0.0.0.0
exec uv run uvicorn cardinal.main:app --reload --host "$HOST" --port 8000 --reload-dir cardinal --reload-dir config
