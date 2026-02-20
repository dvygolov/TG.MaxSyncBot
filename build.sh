#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_PREFIX="[TG.VkSyncBot][build]"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "$LOG_PREFIX ERROR: $PYTHON_BIN not found in PATH."
  exit 1
fi

echo "$LOG_PREFIX Preparing virtual environment in $VENV_DIR"
if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

echo "$LOG_PREFIX Upgrading pip and wheel..."
python -m pip install --no-cache-dir --upgrade pip wheel >/dev/null

echo "$LOG_PREFIX Installing requirements..."
python -m pip install --no-cache-dir -r "$PROJECT_ROOT/requirements.txt"

echo "$LOG_PREFIX Installing Playwright browser (chromium)..."
python -m playwright install chromium

deactivate

echo "$LOG_PREFIX Done. Run: $VENV_DIR/bin/python $PROJECT_ROOT/app.py"
