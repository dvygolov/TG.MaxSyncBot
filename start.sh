#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
PY_BIN="$VENV_DIR/bin/python"
APP_FILE="$PROJECT_ROOT/app.py"
LOG_FILE="$PROJECT_ROOT/tg-vksyncbot.log"
LOG_PREFIX="[TG.VkSyncBot][start]"

if [[ ! -x "$PY_BIN" ]]; then
  echo "$LOG_PREFIX Virtualenv not ready. Run ./build.sh first."
  exit 1
fi

if [[ ! -f "$APP_FILE" ]]; then
  echo "$LOG_PREFIX ERROR: app.py not found at $APP_FILE"
  exit 1
fi

EXISTING_PIDS="$(pgrep -f "$PY_BIN $APP_FILE" || true)"
if [[ -n "$EXISTING_PIDS" ]]; then
  echo "$LOG_PREFIX Already running with PID(s): $EXISTING_PIDS"
  exit 0
fi

echo "$LOG_PREFIX Starting bridge..."
nohup "$PY_BIN" "$APP_FILE" >> "$LOG_FILE" 2>&1 &
PID=$!

echo "$LOG_PREFIX Started with PID $PID"
echo "$LOG_PREFIX Logs: $LOG_FILE"
