#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

HOST="${ODOO_GUI_HOST:-127.0.0.1}"
PORT="${ODOO_GUI_PORT:-8765}"
URL="http://$HOST:$PORT/"

open_url() {
  if command -v open >/dev/null 2>&1; then
    open "$URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 || true
  elif command -v powershell.exe >/dev/null 2>&1; then
    powershell.exe Start-Process "$URL" >/dev/null 2>&1 || true
  fi
}

server_ready() {
  curl -fsS "$URL/api/overview" >/dev/null 2>&1
}

stop_existing() {
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
    if [ -n "$pids" ]; then
      echo "$pids" | xargs kill 2>/dev/null || true
      sleep 0.5
    fi
  fi
}

case "${1:-}" in
  --restart)
    stop_existing
    ;;
  --stop)
    stop_existing
    exit 0
    ;;
esac

if server_ready; then
  echo "Interface deja lancee : $URL"
  echo "Pour charger la derniere version : ./odoo_gui.sh --restart"
  open_url
  exit 0
fi

python3 odoo_manager_web.py
