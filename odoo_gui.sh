#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

case "${1:-}" in
  --restart)
    echo "La vue Bootstrap a ete archivee. Redemarrage de l'interface Next..."
    "$SCRIPT_DIR/odoo_next_gui.sh" --stop || true
    exec "$SCRIPT_DIR/odoo_next_gui.sh" --background
    ;;
  --stop)
    exec "$SCRIPT_DIR/odoo_next_gui.sh" --stop
    ;;
esac

echo "La vue Bootstrap a ete archivee."
echo "Lancement de l'interface Next/Tauri avec ./odoo_next_gui.sh --background"
exec "$SCRIPT_DIR/odoo_next_gui.sh" --background
