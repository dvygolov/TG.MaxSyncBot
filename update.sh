#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="${SERVICE_NAME:-tg-maxsyncbot}"
LOG_PREFIX="[TG.MaxSyncBot][update]"

cd "$PROJECT_ROOT"

if [[ ! -d .git ]]; then
  echo "$LOG_PREFIX ERROR: $PROJECT_ROOT is not a git repository."
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "$LOG_PREFIX ERROR: working tree has local changes. Commit/stash first."
  exit 1
fi

if ! git rev-parse --abbrev-ref "@{u}" >/dev/null 2>&1; then
  echo "$LOG_PREFIX ERROR: no upstream branch configured for current branch."
  exit 1
fi

echo "$LOG_PREFIX Pulling latest changes (fast-forward only)..."
git fetch --all --prune
git merge --ff-only "@{u}"

echo "$LOG_PREFIX Rebuilding environment..."
"$PROJECT_ROOT/build.sh"

if command -v systemctl >/dev/null 2>&1; then
  if systemctl list-unit-files | grep -q "^${SERVICE_NAME}\\.service"; then
    echo "$LOG_PREFIX Restarting systemd service ${SERVICE_NAME}.service..."
    sudo systemctl restart "${SERVICE_NAME}.service"
    echo "$LOG_PREFIX Update complete. Service restarted."
  else
    echo "$LOG_PREFIX Service ${SERVICE_NAME}.service not found."
    echo "$LOG_PREFIX Run ./install-service.sh (or start manually via ./start.sh)."
  fi
else
  echo "$LOG_PREFIX systemctl is not available. Skipping service restart."
  echo "$LOG_PREFIX Update complete."
fi
