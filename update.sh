#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME_FILE="$PROJECT_ROOT/.service-name"
LOG_PREFIX="[TG.VkSyncBot][update]"

resolve_service_name() {
  if [[ -n "${SERVICE_NAME:-}" ]]; then
    echo "$SERVICE_NAME"
    return
  fi

  if [[ -f "$SERVICE_NAME_FILE" ]]; then
    local saved_name
    saved_name="$(tr -d '[:space:]' < "$SERVICE_NAME_FILE" || true)"
    if [[ -n "$saved_name" ]]; then
      echo "$saved_name"
      return
    fi
  fi

  echo "tg-vksyncbot"
}

service_exists() {
  local name="$1"
  local load_state
  load_state="$(systemctl show "${name}.service" -p LoadState --value 2>/dev/null || true)"
  [[ -n "$load_state" && "$load_state" != "not-found" ]]
}

discover_service_by_execstart() {
  local service_file
  while IFS= read -r -d '' service_file; do
    if grep -Fq "ExecStart=$PROJECT_ROOT/.venv/bin/python $PROJECT_ROOT/app.py" "$service_file"; then
      basename "${service_file%.service}"
      return 0
    fi
  done < <(find /etc/systemd/system -maxdepth 1 -type f -name "*.service" -print0 2>/dev/null)
  return 1
}

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
  SERVICE_NAME_RESOLVED="$(resolve_service_name)"
  if ! service_exists "$SERVICE_NAME_RESOLVED"; then
    AUTO_SERVICE_NAME="$(discover_service_by_execstart || true)"
    if [[ -n "$AUTO_SERVICE_NAME" ]]; then
      SERVICE_NAME_RESOLVED="$AUTO_SERVICE_NAME"
    fi
  fi

  if service_exists "$SERVICE_NAME_RESOLVED"; then
    echo "$LOG_PREFIX Restarting systemd service ${SERVICE_NAME_RESOLVED}.service..."
    sudo systemctl restart "${SERVICE_NAME_RESOLVED}.service"
    printf "%s\n" "$SERVICE_NAME_RESOLVED" > "$SERVICE_NAME_FILE"
    echo "$LOG_PREFIX Update complete. Service restarted."
  else
    echo "$LOG_PREFIX Service ${SERVICE_NAME_RESOLVED}.service not found."
    echo "$LOG_PREFIX Run ./install-service.sh (or start manually via ./start.sh)."
  fi
else
  echo "$LOG_PREFIX systemctl is not available. Skipping service restart."
  echo "$LOG_PREFIX Update complete."
fi
