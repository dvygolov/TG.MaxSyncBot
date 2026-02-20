#!/usr/bin/env bash
set -euo pipefail

ACTION="install"
if [[ "${1:-}" == "-u" || "${1:-}" == "--uninstall" ]]; then
  ACTION="uninstall"
  shift || true
fi

DIST_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME_FILE="$DIST_PATH/.service-name"
INPUT_SERVICE_NAME="${1:-}"
SAVED_SERVICE_NAME=""
if [[ -f "$SERVICE_NAME_FILE" ]]; then
  SAVED_SERVICE_NAME="$(tr -d '[:space:]' < "$SERVICE_NAME_FILE" || true)"
fi
SERVICE_NAME="${INPUT_SERVICE_NAME:-${SERVICE_NAME:-${SAVED_SERVICE_NAME:-tg-vksyncbot}}}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
LOG_FILE="$DIST_PATH/${SERVICE_NAME}.service.log"
PY_BIN="$DIST_PATH/.venv/bin/python"
APP_FILE="$DIST_PATH/app.py"
SERVICE_USER="${SERVICE_USER:-$USER}"
LOG_PREFIX="[TG.VkSyncBot][service]"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "$LOG_PREFIX ERROR: systemctl is not available on this host."
  exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
  echo "$LOG_PREFIX ERROR: sudo is not available on this host."
  exit 1
fi

if [[ "$ACTION" == "uninstall" ]]; then
  echo "$LOG_PREFIX Uninstalling ${SERVICE_NAME}.service"

  if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    sudo systemctl stop "${SERVICE_NAME}.service"
  fi

  if systemctl is-enabled --quiet "${SERVICE_NAME}.service"; then
    sudo systemctl disable "${SERVICE_NAME}.service"
  fi

  if [[ -f "$SERVICE_FILE" ]]; then
    sudo rm -f "$SERVICE_FILE"
    sudo systemctl daemon-reload
  fi

  if [[ -f "$SERVICE_NAME_FILE" && "$SAVED_SERVICE_NAME" == "$SERVICE_NAME" ]]; then
    rm -f "$SERVICE_NAME_FILE"
  fi

  echo "$LOG_PREFIX Uninstalled ${SERVICE_NAME}.service"
  exit 0
fi

echo "$LOG_PREFIX Installing ${SERVICE_NAME}.service"
echo "$LOG_PREFIX Project dir: $DIST_PATH"

if [[ ! -d "$DIST_PATH/.venv" || ! -x "$PY_BIN" ]]; then
  echo "$LOG_PREFIX Virtualenv not ready, running ./build.sh"
  "$DIST_PATH/build.sh"
fi

if [[ ! -x "$PY_BIN" ]]; then
  echo "$LOG_PREFIX ERROR: Python executable not found at $PY_BIN"
  exit 1
fi

if [[ ! -f "$APP_FILE" ]]; then
  echo "$LOG_PREFIX ERROR: app.py not found at $APP_FILE"
  exit 1
fi

sudo bash -c "cat > '$SERVICE_FILE'" <<EOF
[Unit]
Description=TG.VkSyncBot Telegram to VK wall bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIST_PATH
ExecStart=$PY_BIN $APP_FILE
EnvironmentFile=-$DIST_PATH/.env
User=$SERVICE_USER
Group=$SERVICE_USER
Restart=always
RestartSec=5
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"
sudo systemctl restart "${SERVICE_NAME}.service"
printf "%s\n" "$SERVICE_NAME" > "$SERVICE_NAME_FILE"

echo "$LOG_PREFIX Installed and started ${SERVICE_NAME}.service"
echo "$LOG_PREFIX Manage:"
echo "  Status:   sudo systemctl status ${SERVICE_NAME}"
echo "  Stop:     sudo systemctl stop ${SERVICE_NAME}"
echo "  Start:    sudo systemctl start ${SERVICE_NAME}"
echo "  Restart:  sudo systemctl restart ${SERVICE_NAME}"
echo "  Logs:     tail -f $LOG_FILE"
