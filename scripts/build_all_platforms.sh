#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
WORKFLOW="build-desktop.yml"
DIST_ROOT="$ROOT/dist/all-platforms"
VERSION_FILE="$ROOT/odoo-manager-next/package.json"

RUN_CHECKS=1
LOCAL_BUILD=0
WAIT_FOR_CI=1
DOWNLOAD_ARTIFACTS=1
CLEAN_AFTER_CHECKS=1
TAG=""

usage() {
  cat <<'EOF'
Compile Odoo Manager pour toutes les plateformes.

Ce script utilise le pipeline existant :
  - checks locaux : Python + tests + build Next.js
  - build multi-plateformes : GitHub Actions via tag app-v*
  - téléchargement des artefacts : automatique si GitHub CLI (gh) est installé

Usage:
  sh scripts/build_all_platforms.sh [options]

Options:
  --tag NOM              Utilise un tag précis, par exemple app-v0.1.0-build14.
  --local                Compile aussi la plateforme courante en local.
  --skip-checks          Ne lance pas py_compile, unittest et npm run build.
  --no-wait              Ne surveille pas la fin du workflow GitHub Actions.
  --no-download          Ne télécharge pas les artefacts GitHub Actions.
  --no-clean             Conserve .next et les caches générés par les checks.
  -h, --help             Affiche cette aide.

Pré-requis:
  - git, python3, npm
  - GitHub remote origin configuré
  - droits de push sur le dépôt
  - recommandé : gh authentifié pour attendre et télécharger automatiquement

Sorties:
  - GitHub Actions produit macOS, Linux x64 et Windows x64.
  - Si gh est disponible, les artefacts sont téléchargés dans dist/all-platforms/<tag>/.
EOF
}

log() {
  printf '\n==> %s\n' "$*"
}

die() {
  printf 'Erreur: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "commande requise introuvable: $1"
}

run() {
  printf '+'
  for arg in "$@"; do
    printf ' %s' "$arg"
  done
  printf '\n'
  "$@"
}

clean_generated_files() {
  rm -rf "$ROOT/__pycache__" \
    "$ROOT/odoo_manager_core/__pycache__" \
    "$ROOT/tests/__pycache__" \
    "$ROOT/odoo-manager-next/.next" \
    "$ROOT/odoo-manager-next/tsconfig.tsbuildinfo"
}

package_version() {
  python3 - "$VERSION_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    print(json.loads(path.read_text(encoding="utf-8")).get("version") or "0.0.0")
except Exception:
    print("0.0.0")
PY
}

repo_slug() {
  url=$(git -C "$ROOT" config --get remote.origin.url || true)
  case "$url" in
    git@github.com:*)
      slug=${url#git@github.com:}
      slug=${slug%.git}
      printf '%s\n' "$slug"
      ;;
    https://github.com/*)
      slug=${url#https://github.com/}
      slug=${slug%.git}
      printf '%s\n' "$slug"
      ;;
    *)
      printf '%s\n' ""
      ;;
  esac
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tag)
      [ "$#" -ge 2 ] || die "--tag attend une valeur"
      TAG=$2
      shift 2
      ;;
    --local)
      LOCAL_BUILD=1
      shift
      ;;
    --skip-checks)
      RUN_CHECKS=0
      shift
      ;;
    --no-wait)
      WAIT_FOR_CI=0
      shift
      ;;
    --no-download)
      DOWNLOAD_ARTIFACTS=0
      shift
      ;;
    --no-clean)
      CLEAN_AFTER_CHECKS=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "option inconnue: $1"
      ;;
  esac
done

cd "$ROOT"

require_cmd git
require_cmd python3
require_cmd npm

[ -f "$ROOT/.github/workflows/$WORKFLOW" ] || die "workflow introuvable: .github/workflows/$WORKFLOW"
[ -f "$ROOT/scripts/build_desktop.py" ] || die "script introuvable: scripts/build_desktop.py"

if ! git diff --quiet -- . ':!dist' || ! git diff --cached --quiet -- . ':!dist' || [ -n "$(git ls-files --others --exclude-standard)" ]; then
  printf 'Erreur: le dépôt contient des changements non commités.\n' >&2
  printf '\nFichiers concernés:\n' >&2
  git status --short >&2
  printf '\nGitHub Actions compile uniquement l état Git poussé sur GitHub.\n' >&2
  printf 'Commite et pousse d abord les changements, puis relance ce script.\n\n' >&2
  printf 'Commandes typiques:\n' >&2
  printf '  git add README_next_odoo_manager.md odoo-manager-next/app/page.tsx odoo-manager-next/components/ui/button.tsx odoo_manager_web.py tests/test_module_layout.py scripts/build_all_platforms.sh\n' >&2
  printf '  git commit -m "Add all-platform desktop build launcher"\n' >&2
  printf '  git push origin main\n' >&2
  printf '  sh scripts/build_all_platforms.sh --tag %s\n' "${TAG:-app-v0.1.0-build14}" >&2
  exit 1
fi

if [ "$RUN_CHECKS" -eq 1 ]; then
  log "Checks locaux"
  run python3 -m py_compile odoo_manager_web.py odoo_manager_core/system.py odoo_manager_core/platform.py
  run python3 -m unittest discover -s tests -v
  (cd "$ROOT/odoo-manager-next" && run npm run build)
fi

if [ "$CLEAN_AFTER_CHECKS" -eq 1 ]; then
  log "Nettoyage des fichiers temporaires"
  clean_generated_files
fi

if [ "$LOCAL_BUILD" -eq 1 ]; then
  log "Build local de la plateforme courante"
  run python3 "$ROOT/scripts/build_desktop.py"
fi

if [ -z "$TAG" ]; then
  version=$(package_version)
  TAG="app-v${version}-$(date '+%Y%m%d-%H%M%S')"
fi

case "$TAG" in
  app-v*) ;;
  *) die "le tag doit commencer par app-v pour déclencher le workflow: $TAG" ;;
esac

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  die "le tag existe déjà localement: $TAG"
fi

HEAD_SHA=$(git rev-parse HEAD)

log "Création du tag $TAG"
run git tag "$TAG" "$HEAD_SHA"

log "Push du tag vers origin"
run git push origin "refs/tags/$TAG"

actions_url=""
slug=$(repo_slug)
if [ -n "$slug" ]; then
  actions_url="https://github.com/$slug/actions/workflows/$WORKFLOW"
  printf 'Workflow GitHub Actions: %s\n' "$actions_url"
fi

if ! command -v gh >/dev/null 2>&1; then
  log "GitHub CLI absent"
  printf 'Le build multi-plateformes est lancé via le tag %s.\n' "$TAG"
  printf 'Installe GitHub CLI pour attendre et télécharger automatiquement les artefacts: https://cli.github.com/\n'
  [ -n "$actions_url" ] && printf 'Artefacts à récupérer ici: %s\n' "$actions_url"
  exit 0
fi

log "Recherche du workflow GitHub Actions"
run_id=""
i=0
while [ "$i" -lt 30 ]; do
  run_id=$(gh run list \
    --workflow "$WORKFLOW" \
    --json databaseId,headSha,event,headBranch \
    --limit 20 \
    --jq ".[] | select(.headSha == \"$HEAD_SHA\" and .event == \"push\" and .headBranch == \"$TAG\") | .databaseId" | head -n 1)
  [ -n "$run_id" ] && break
  i=$((i + 1))
  sleep 5
done

[ -n "$run_id" ] || die "workflow GitHub Actions introuvable pour le tag $TAG"
printf 'Run GitHub Actions: %s\n' "$run_id"

if [ "$WAIT_FOR_CI" -eq 1 ]; then
  log "Attente de la fin du build GitHub Actions"
  run gh run watch "$run_id" --exit-status
fi

if [ "$DOWNLOAD_ARTIFACTS" -eq 1 ]; then
  output_dir="$DIST_ROOT/$TAG"
  rm -rf "$output_dir"
  mkdir -p "$output_dir"

  log "Téléchargement des artefacts"
  run gh run download "$run_id" --dir "$output_dir"

  log "Artefacts téléchargés"
  find "$output_dir" -maxdepth 5 \( -type f -o -type l \) | sort
fi

log "Terminé"
printf 'Tag: %s\n' "$TAG"
[ -n "$actions_url" ] && printf 'GitHub Actions: %s\n' "$actions_url"
