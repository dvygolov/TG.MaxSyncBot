#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_BIN="$PROJECT_ROOT/.venv/bin/python"
APP_FILE="$PROJECT_ROOT/app.py"
LOG_PREFIX="[TG.VkSyncBot][stop]"

PIDS="$(pgrep -f "$PY_BIN $APP_FILE" || true)"

if [[ -z "$PIDS" ]]; then
  echo "$LOG_PREFIX Not running"
  exit 0
fi

echo "$LOG_PREFIX Stopping PID(s): $PIDS"
kill $PIDS
echo "$LOG_PREFIX Stopped"
