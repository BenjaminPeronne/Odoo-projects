#!/usr/bin/env sh
set -eu

APP_PATH=${1:-"/Applications/Odoo Manager.app"}

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Ce script est uniquement utile sur macOS." >&2
  exit 1
fi

if [ ! -d "$APP_PATH" ]; then
  echo "Application introuvable: $APP_PATH" >&2
  echo "Glisse d'abord Odoo Manager.app dans Applications, ou passe le chemin en argument." >&2
  exit 1
fi

echo "Nettoyage de la quarantaine macOS: $APP_PATH"
xattr -cr "$APP_PATH"

echo "Vérification de la signature locale"
if ! codesign --verify --deep --strict --verbose=2 "$APP_PATH" >/dev/null 2>&1; then
  echo "Signature locale incomplète, correction ad hoc du bundle privé"
  codesign --force --deep --sign - "$APP_PATH"
fi

codesign --verify --deep --strict --verbose=2 "$APP_PATH"

echo "Odoo Manager peut maintenant être ouvert depuis Applications."
echo "Note: ce contournement est réservé aux builds privés non notarizés."
