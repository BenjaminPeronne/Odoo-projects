#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_HOST="${ODOO_GUI_HOST:-127.0.0.1}"
BACKEND_PORT="${ODOO_GUI_PORT:-18765}"
FRONTEND_HOST="${ODOO_NEXT_HOST:-127.0.0.1}"
FRONTEND_PORT="${ODOO_NEXT_PORT:-3000}"
BACKEND_URL="http://$BACKEND_HOST:$BACKEND_PORT"
FRONTEND_URL="http://$FRONTEND_HOST:$FRONTEND_PORT"
BACKEND_PID_FILE="$SCRIPT_DIR/.odoo_manager_web.pid"
NEXT_PID_FILE="$SCRIPT_DIR/.odoo_manager_next.pid"
TMUX_SESSION="${ODOO_NEXT_TMUX_SESSION:-odoo-manager-next}"
BACKEND_STARTED=0

open_url() {
  if command -v open >/dev/null 2>&1; then
    open "$FRONTEND_URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$FRONTEND_URL" >/dev/null 2>&1 || true
  elif command -v powershell.exe >/dev/null 2>&1; then
    powershell.exe Start-Process "$FRONTEND_URL" >/dev/null 2>&1 || true
  fi
}

backend_ready() {
  curl -fsS "$BACKEND_URL/api/overview" >/dev/null 2>&1
}

start_backend() {
  if backend_ready; then
    return
  fi

  BACKEND_STARTED=1
  echo "Demarrage backend Python: $BACKEND_URL"
  (
    cd "$SCRIPT_DIR"
    nohup python3 -u odoo_manager_web.py > .odoo_manager_web.log 2>&1 < /dev/null &
    echo $! > "$BACKEND_PID_FILE"
  )

  i=0
  while [ "$i" -lt 20 ]; do
    if backend_ready; then
      return
    fi
    sleep 0.5
    i=$((i + 1))
  done

  echo "Backend Python indisponible. Voir: $SCRIPT_DIR/.odoo_manager_web.log" >&2
  exit 1
}

case "${1:-}" in
  --stop)
    if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
      tmux kill-session -t "$TMUX_SESSION"
    fi
    pids="$(lsof -tiTCP:"$FRONTEND_PORT" -sTCP:LISTEN 2>/dev/null || true)"
    if [ -n "$pids" ]; then
      echo "$pids" | xargs kill 2>/dev/null || true
    fi
    exit 0
    ;;
esac

start_backend

case "${1:-}" in
  --background)
    if curl -fsS "$FRONTEND_URL" >/dev/null 2>&1; then
      echo "Interface Next deja lancee : $FRONTEND_URL"
      open_url
      exit 0
    fi

    if command -v tmux >/dev/null 2>&1; then
      tmux has-session -t "$TMUX_SESSION" 2>/dev/null && tmux kill-session -t "$TMUX_SESSION"
      tmux new-session -d -s "$TMUX_SESSION" "cd '$SCRIPT_DIR/odoo-manager-next' && ODOO_MANAGER_API='$BACKEND_URL' npm run dev -- --hostname '$FRONTEND_HOST' --port '$FRONTEND_PORT'"
    else
      (
        cd "$SCRIPT_DIR/odoo-manager-next"
        nohup env ODOO_MANAGER_API="$BACKEND_URL" npm run dev -- --hostname "$FRONTEND_HOST" --port "$FRONTEND_PORT" > "$SCRIPT_DIR/.odoo_manager_next.log" 2>&1 < /dev/null &
        echo $! > "$NEXT_PID_FILE"
      )
    fi

    i=0
    while [ "$i" -lt 30 ]; do
      if curl -fsS "$FRONTEND_URL" >/dev/null 2>&1; then
        echo "Interface Next lancee : $FRONTEND_URL"
        open_url
        exit 0
      fi
      sleep 0.5
      i=$((i + 1))
    done
    echo "Interface Next indisponible apres demarrage." >&2
    exit 1
    ;;
esac

cd "$SCRIPT_DIR/odoo-manager-next"

cleanup_foreground() {
  status=$?
  trap - EXIT INT TERM HUP
  if [ -n "${NEXT_PID:-}" ]; then
    kill "$NEXT_PID" 2>/dev/null || true
    wait "$NEXT_PID" 2>/dev/null || true
    rm -f "$NEXT_PID_FILE"
  fi
  if [ "$BACKEND_STARTED" -eq 1 ] && [ -f "$BACKEND_PID_FILE" ]; then
    backend_pid="$(cat "$BACKEND_PID_FILE" 2>/dev/null || true)"
    if [ -n "$backend_pid" ]; then
      kill "$backend_pid" 2>/dev/null || true
    fi
    rm -f "$BACKEND_PID_FILE"
  fi
  exit "$status"
}

trap cleanup_foreground EXIT
trap 'exit 130' INT TERM HUP

ODOO_MANAGER_API="$BACKEND_URL" npm run dev -- --hostname "$FRONTEND_HOST" --port "$FRONTEND_PORT" &
NEXT_PID=$!
echo "$NEXT_PID" > "$NEXT_PID_FILE"

i=0
while [ "$i" -lt 40 ]; do
  if curl -fsS "$FRONTEND_URL" >/dev/null 2>&1; then
    echo "Interface prete : $FRONTEND_URL"
    open_url
    echo "Appuie sur Ctrl+C pour arreter la previsualisation."
    wait "$NEXT_PID"
    exit $?
  fi
  if ! kill -0 "$NEXT_PID" 2>/dev/null; then
    echo "Interface Next arretee avant son demarrage." >&2
    wait "$NEXT_PID" || true
    exit 1
  fi
  sleep 0.5
  i=$((i + 1))
done

echo "Interface Next indisponible apres 20 secondes." >&2
exit 1
