#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="${ODOO_WORKSPACE:-$SCRIPT_DIR}"
TRAEFIK_DIR="${TRAEFIK_DIR:-$HOME/docker-local-tools/traefik}"
TRAEFIK_REPO="${TRAEFIK_REPO:-ssh://git@gitlab.sudokeys.com:10022/devops/docker-local-tools.git}"
VENV_DIR="${VENV_DIR:-$HOME/venv_3.12}"
FAILURE_DIR="$WORKSPACE/.odoo_manager_failures"
DOCKER_BIN="${ODOO_MANAGER_DOCKER:-docker}"
BRAINKEYS_BIN="${ODOO_MANAGER_BRAINKEYS:-brainkeys}"

docker() {
  command "$DOCKER_BIN" "$@"
}

brainkeys() {
  command "$BRAINKEYS_BIN" "$@"
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

die() {
  echo "Erreur: $*" >&2
  exit 1
}

safe_name() {
  printf '%s' "$1" | tr '/ ,' '___' | tr -cd 'A-Za-z0-9_.-'
}

compact_name() {
  local raw="$1"
  local sanitized
  local checksum

  sanitized="$(safe_name "$raw")"
  if [ "${#sanitized}" -le 72 ]; then
    printf '%s' "$sanitized"
    return
  fi

  checksum="$(printf '%s' "$raw" | cksum | awk '{print $1}')"
  printf '%s_%s' "$(printf '%s' "$sanitized" | cut -c1-60)" "$checksum"
}

print_failure_context() {
  local file="$1"
  local first_match
  local start
  local end
  local total

  first_match="$(awk '
    /CRITICAL|Traceback|ParseError|Exception|ValidationError|UserError|ValueError|KeyError|AttributeError|AssertionError|while parsing|odoo\.tools\.convert|External ID|does not exist|already exists|Document does not comply/ {
      print NR
      exit
    }
  ' "$file" 2>/dev/null || true)"

  if [ -z "$first_match" ]; then
    first_match="$(awk '
      /^[0-9]{4}-[0-9]{2}-[0-9]{2} .* ERROR / || /ERROR:|DETAIL:|not found/ {
      print NR
      exit
    }
  ' "$file" 2>/dev/null || true)"
  fi

  if [ -n "$first_match" ]; then
    total="$(wc -l < "$file" | tr -d ' ')"
    start=$((first_match - 35))
    end=$((first_match + 140))
    [ "$start" -lt 1 ] && start=1
    [ "$end" -gt "$total" ] && end="$total"
    echo "Contexte de l'erreur Odoo (lignes $start-$end):"
    sed -n "${start},${end}p" "$file"
  else
    echo "Aucun marqueur d'erreur clair trouve. Dernieres lignes de la commande Odoo:"
    tail -n 220 "$file" 2>/dev/null || true
  fi
}

path_mtime() {
  local path="$1"

  stat -f '%m' "$path" 2>/dev/null || stat -c '%Y' "$path" 2>/dev/null || printf '0\n'
}

brainkeys_addon_candidates() {
  {
    find -H /tmp -maxdepth 1 -type d -name 'tmp-*' -print 2>/dev/null || true
    find /private/tmp -maxdepth 1 -type d -name 'tmp-*' -print 2>/dev/null || true
  } | while IFS= read -r dir; do
    [ -d "$dir/.git" ] || continue
    [ -f "$dir/odoo/odoo/release.py" ] && continue
    find "$dir" -maxdepth 2 \( -name "__manifest__.py" -o -name "__openerp__.py" \) -print -quit 2>/dev/null | grep -q . || continue
    (cd "$dir" 2>/dev/null && pwd -P)
  done | sort -u
}

print_brainkeys_failure_help() {
  local candidate
  local modules

  echo ""
  echo "Brainkeys n'a pas termine la creation du projet."
  echo ""

  candidate="$(
    brainkeys_addon_candidates | while IFS= read -r dir; do
      printf '%s %s\n' "$(path_mtime "$dir")" "$dir"
    done | sort -n | tail -n 1 | cut -d ' ' -f 2-
  )"

  if [ -n "$candidate" ]; then
    echo "Diagnostic probable:"
    echo "  Le depot clone ressemble a un depot d'addons Odoo, pas a un projet Odoo complet."
    echo "  Brainkeys cherche le fichier odoo/odoo/release.py pour detecter la version Odoo,"
    echo "  mais ce fichier est absent dans: $candidate"
    echo ""
    echo "Modules detectes dans le depot clone:"
    modules="$(
      find "$candidate" -maxdepth 2 \( -name "__manifest__.py" -o -name "__openerp__.py" \) -print 2>/dev/null |
      while IFS= read -r manifest; do basename "$(dirname "$manifest")"; done |
      sort
    )"
    if [ -n "$modules" ]; then
      printf '%s\n' "$modules" | sed 's/^/  - /'
    else
      echo "  - aucun manifeste detecte"
    fi
    echo ""
    echo "Que faire:"
    echo "  1. Pour creer un projet avec Brainkeys, choisis un environnement/projet complet."
    echo "  2. Pour ajouter ce depot d'addons a un projet existant, utilise ensuite"
    echo "     l'action 'Lier des modules' ou l'import ZIP du gestionnaire."
  else
    echo "Aucun depot temporaire d'addons n'a ete detecte."
    echo "Relance Brainkeys dans le terminal pour voir son message d'erreur complet."
  fi
}

compose_file_for() {
  local project_path="$1"

  if [ -f "$project_path/docker-compose.yml" ]; then
    printf '%s\n' "$project_path/docker-compose.yml"
  elif [ -f "$project_path/docker-compose.yaml" ]; then
    printf '%s\n' "$project_path/docker-compose.yaml"
  elif [ -f "$project_path/compose.yml" ]; then
    printf '%s\n' "$project_path/compose.yml"
  elif [ -f "$project_path/compose.yaml" ]; then
    printf '%s\n' "$project_path/compose.yaml"
  else
    return 1
  fi
}

fix_macos_localtime_mount() {
  local compose_file="$1"
  local backup_file

  [ "$(uname -s 2>/dev/null || true)" = "Darwin" ] || return 0
  grep -q '/etc/localtime:/etc/localtime:ro' "$compose_file" 2>/dev/null || return 0

  backup_file="$compose_file.localtime.bak.$(date '+%Y%m%d_%H%M%S')"
  cp "$compose_file" "$backup_file"
  sed '/\/etc\/localtime:\/etc\/localtime:ro/d' "$backup_file" > "$compose_file"
  echo "Mount /etc/localtime supprime du compose macOS: $compose_file"
  echo "Sauvegarde: $backup_file"
}

project_path() {
  printf '%s/%s\n' "$WORKSPACE" "$1"
}

list_projects() {
  local dir name

  find "$WORKSPACE" -maxdepth 1 -mindepth 1 -type d | while IFS= read -r dir; do
    if compose_file_for "$dir" >/dev/null 2>&1; then
      name="$(basename "$dir")"
      printf '%s\n' "$name"
    fi
  done | sort
}

require_docker() {
  has_cmd "$DOCKER_BIN" || die "Docker est introuvable dans ce terminal: $DOCKER_BIN"
  docker info >/dev/null 2>&1 || die "Docker ne repond pas. Demarre Docker Desktop ou le service Docker."
}

install_traefik() {
  local tools_dir
  local parent_dir

  require_docker
  has_cmd git || die "Git est introuvable. Installe Git avant d'installer Traefik."

  if [ -f "$TRAEFIK_DIR/docker-compose.yml" ] || [ -f "$TRAEFIK_DIR/docker-compose.yaml" ] || [ -f "$TRAEFIK_DIR/compose.yml" ] || [ -f "$TRAEFIK_DIR/compose.yaml" ]; then
    echo "Traefik deja present: $TRAEFIK_DIR"
    start_traefik
    return
  fi

  tools_dir="$(dirname "$TRAEFIK_DIR")"
  parent_dir="$(dirname "$tools_dir")"

  if [ "$(basename "$TRAEFIK_DIR")" != "traefik" ] || [ "$(basename "$tools_dir")" != "docker-local-tools" ]; then
    die "Dossier Traefik non standard: $TRAEFIK_DIR. Renseigne un chemin .../docker-local-tools/traefik ou installe le depot manuellement."
  fi

  if [ -e "$tools_dir" ] && [ ! -d "$tools_dir/.git" ]; then
    die "Le dossier $tools_dir existe deja mais n'est pas un clone Git exploitable."
  fi

  mkdir -p "$parent_dir"
  if [ ! -d "$tools_dir/.git" ]; then
    echo "Installation de docker-local-tools..."
    git clone "$TRAEFIK_REPO" "$tools_dir"
  else
    echo "Mise a jour de docker-local-tools..."
    (
      cd "$tools_dir"
      git pull --ff-only
    )
  fi

  [ -d "$TRAEFIK_DIR" ] || die "Le dossier Traefik reste introuvable apres installation: $TRAEFIK_DIR"
  start_traefik
}

container_status() {
  local container="$1"
  docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || printf 'absent'
}

is_running() {
  [ "$(container_status "$1")" = "running" ]
}

project_url() {
  local project="$1"
  local path file host host_port

  if has_cmd "$DOCKER_BIN" && docker info >/dev/null 2>&1; then
    host_port="$(docker port "odoo-$project" 8069/tcp 2>/dev/null | awk -F: '{print $2}' | head -n 1 || true)"
    if [ -n "$host_port" ]; then
      printf 'http://localhost:%s/\n' "$host_port"
      return
    fi
  fi

  path="$(project_path "$project")"
  file="$(compose_file_for "$path" 2>/dev/null || true)"
  if [ -n "$file" ]; then
    host="$(awk -F'`' '/Host\(`/ {print $2; exit}' "$file" 2>/dev/null || true)"
    if [ -n "$host" ]; then
      printf 'http://%s/\n' "$host"
      return
    fi
  fi

  printf 'http://dev.%s.localhost/\n' "$project"
}

open_url() {
  local url="$1"

  if has_cmd open; then
    open "$url" >/dev/null 2>&1 || true
  elif has_cmd xdg-open; then
    xdg-open "$url" >/dev/null 2>&1 || true
  elif has_cmd powershell.exe; then
    powershell.exe Start-Process "$url" >/dev/null 2>&1 || true
  else
    echo "URL a ouvrir: $url"
  fi
}

start_traefik() {
  if [ ! -d "$TRAEFIK_DIR" ]; then
    echo "Traefik introuvable: $TRAEFIK_DIR"
    return 0
  fi

  echo "Demarrage de Traefik..."
  (
    cd "$TRAEFIK_DIR"
    docker compose up -d
  )
}

compose_up_project() {
  local project="$1"
  local path="$2"
  local code

  set +e
  (
    cd "$path"
    if [ "$(container_status "odoo-$project")" != "absent" ] || [ "$(container_status "postgresql-$project")" != "absent" ]; then
      echo "Conteneurs existants detectes, demarrage sans recreation..."
      docker compose up -d --no-recreate
    else
      docker compose up --pull always -d
    fi
  )
  code=$?
  set -e

  if [ "$code" -ne 0 ] && is_running "odoo-$project"; then
    echo "Docker Compose a retourne une erreur, mais odoo-$project est deja running."
    echo "Le gestionnaire continue avec le conteneur existant."
    return 0
  fi

  return "$code"
}

odoo_server_running() {
  local container="$1"

  docker exec "$container" sh -lc "ps aux | grep -E '[o]doo-bin' >/dev/null 2>&1"
}

wait_odoo_port() {
  local container="$1"
  local max_wait="$2"
  local waited=0

  while [ "$waited" -le "$max_wait" ]; do
    printf '\rAttente serveur Odoo... %ss/%ss ' "$waited" "$max_wait"

    if docker exec "$container" python3 -c 'import socket; s=socket.create_connection(("127.0.0.1", 8069), 2); s.close()' >/dev/null 2>&1; then
      echo ""
      return 0
    fi

    sleep 2
    waited=$((waited + 2))
  done

  echo ""
  return 1
}

start_odoo_server() {
  local project="$1"
  local container="odoo-$project"

  if odoo_server_running "$container"; then
    echo "Serveur Odoo deja demarre dans $container"
  else
    echo "Demarrage du serveur Odoo dans $container..."
    docker exec -e LOG_ATTACHMENTS=False -d "$container" odoo -c /home/odoo/srv/conf/odoo.conf --logfile=/home/odoo/srv/data/odoo.log
  fi

  if ! wait_odoo_port "$container" 90; then
    echo "Odoo ne repond pas sur le port 8069."
    echo "Logs Odoo:"
    docker exec "$container" sh -lc "tail -n 160 /home/odoo/srv/data/odoo.log 2>/dev/null || true"
    return 1
  fi
}

stop_odoo_server() {
  local project="$1"
  local container="odoo-$project"

  if odoo_server_running "$container"; then
    echo "Arret du serveur Odoo dans $container..."
    docker exec "$container" sh -lc "pkill -TERM -f '[o]doo-bin' || pkill -TERM -f '[ /]odoo ' || true"
    sleep 2
  fi
}

install_project_pip_requirements() {
  local project="$1"
  local requirements_file
  local container="odoo-$project"

  requirements_file="$(project_path "$project")/init/requirements_pip.txt"
  [ -f "$requirements_file" ] || return 0
  grep -Eq '^[[:space:]]*[^#[:space:]]' "$requirements_file" || return 0

  echo "Installation des dependances Python du projet..."
  docker exec "$container" /home/_venv/bin/python -m pip install -r /conf/requirements_pip.txt
}

start_project() {
  local project="$1"
  local path
  local compose_file

  require_docker
  path="$(project_path "$project")"
  compose_file="$(compose_file_for "$path" 2>/dev/null || true)"
  [ -n "$compose_file" ] || die "Projet introuvable ou sans fichier compose: $project"
  fix_macos_localtime_mount "$compose_file"

  start_traefik
  echo "Demarrage du projet $project..."
  compose_up_project "$project" "$path"

  wait_for_container "odoo-$project" 60

  if is_running "odoo-$project"; then
    start_odoo_server "$project"
    echo ""
    echo "Projet demarre: $project"
    echo "URL Odoo: $(project_url "$project")"
  else
    echo ""
    echo "Le conteneur odoo-$project n'est pas running."
    docker ps -a --format 'table {{.Names}}\t{{.Status}}' | awk -v name="odoo-$project" 'NR == 1 || $1 == name'
    return 1
  fi
}

stop_project() {
  local project="$1"
  local path
  local compose_file

  require_docker
  path="$(project_path "$project")"
  compose_file="$(compose_file_for "$path" 2>/dev/null || true)"
  [ -n "$compose_file" ] || die "Projet introuvable ou sans fichier compose: $project"

  echo "Arret du projet $project..."
  (
    cd "$path"
    docker compose stop
  )
  echo "Projet arrete: $project"
}

wait_for_container() {
  local container="$1"
  local max_wait="$2"
  local waited=0
  local status

  while [ "$waited" -le "$max_wait" ]; do
    status="$(container_status "$container")"
    printf '\rAttente %s... %ss/%ss (%s) ' "$container" "$waited" "$max_wait" "$status"

    if [ "$status" = "running" ]; then
      echo ""
      return 0
    fi

    if [ "$status" != "absent" ] && [ "$status" != "created" ] && [ "$status" != "restarting" ]; then
      echo ""
      return 1
    fi

    sleep 2
    waited=$((waited + 2))
  done

  echo ""
  return 1
}

print_dashboard() {
  local project odoo_status pg_status url
  local docker_ready=""

  echo ""
  echo "Bases / projets Odoo locaux"
  echo "Workspace: $WORKSPACE"
  echo ""

  if has_cmd "$DOCKER_BIN" && docker info >/dev/null 2>&1; then
    docker_ready="1"
  else
    echo "Note: Docker ne repond pas. Les statuts affiches seront 'docker off'."
    echo ""
  fi

  printf '%-24s %-12s %-12s %s\n' "Projet" "Odoo" "PostgreSQL" "URL"
  printf '%-24s %-12s %-12s %s\n' "------" "----" "----------" "---"

  list_projects | while IFS= read -r project; do
    if [ -n "$docker_ready" ]; then
      odoo_status="$(container_status "odoo-$project")"
      pg_status="$(container_status "postgresql-$project")"
    else
      odoo_status="docker off"
      pg_status="docker off"
    fi
    url="$(project_url "$project")"
    printf '%-24s %-12s %-12s %s\n' "$project" "$odoo_status" "$pg_status" "$url"
  done

  echo ""
}

list_databases() {
  local project="$1"
  local pg_container="postgresql-$project"

  require_docker
  if ! is_running "$pg_container"; then
    echo "PostgreSQL n'est pas demarre pour $project."
    echo "Demarre d'abord le projet."
    return 1
  fi

  echo ""
  echo "Bases PostgreSQL pour $project"
  docker exec "$pg_container" psql -U postgres -Atc \
    "select datname from pg_database where datistemplate = false order by datname;" |
    awk '{ if ($0 == "postgres") print "  - " $0 " (systeme)"; else print "  - " $0 }'
  echo ""
}

run_odoo_module_command() {
  local project="$1"
  local db_name="$2"
  local module_names="$3"
  local module_option="$4"
  local container="odoo-$project"
  local code
  local command_output
  local saved_output
  local stamp
  local safe_project
  local safe_db
  local safe_modules

  start_project "$project"
  install_project_pip_requirements "$project"
  stop_odoo_server "$project"

  echo ""
  echo "Commande Odoo"
  echo "Projet: $project"
  echo "Base: $db_name"
  echo "Module(s): $module_names"
  if [ "$module_option" = "-i" ]; then
    echo "Action: installation"
  else
    echo "Action: mise a jour"
  fi
  echo ""

  mkdir -p "$FAILURE_DIR"
  stamp="$(date '+%Y%m%d_%H%M%S')"
  safe_project="$(safe_name "$project")"
  safe_db="$(safe_name "$db_name")"
  safe_modules="$(compact_name "$module_names")"
  command_output="$(mktemp)"
  saved_output="$FAILURE_DIR/${stamp}_${safe_project}_${safe_db}_${module_option#-}_${safe_modules}.log"
  set +e
  docker exec -e LOG_ATTACHMENTS=False "$container" odoo -c /home/odoo/srv/conf/odoo.conf -d "$db_name" "$module_option" "$module_names" --stop-after-init > "$command_output" 2>&1
  code=$?
  set -e
  if [ "$code" -eq 0 ]; then
    cat "$command_output"
  else
    cp "$command_output" "$saved_output"
    print_failure_context "$command_output"
  fi

  echo ""
  echo "Redemarrage du serveur Odoo..."
  start_odoo_server "$project"

  if [ "$code" -ne 0 ]; then
    echo "La commande Odoo a echoue avec le code $code."
    echo "Sortie complete: $saved_output"
    rm -f "$command_output"
    return "$code"
  fi

  rm -f "$command_output"
  echo "Operation terminee."
  echo "URL Odoo: $(project_url "$project")"
}

run_odoo_uninstall_command() {
  local project="$1"
  local db_name="$2"
  local module_names="$3"
  local container="odoo-$project"
  local code
  local command_output
  local saved_output
  local stamp
  local safe_project
  local safe_db
  local safe_modules

  start_project "$project"
  stop_odoo_server "$project"

  echo ""
  echo "Commande Odoo"
  echo "Projet: $project"
  echo "Base: $db_name"
  echo "Module(s): $module_names"
  echo "Action: desinstallation"
  echo ""

  mkdir -p "$FAILURE_DIR"
  stamp="$(date '+%Y%m%d_%H%M%S')"
  safe_project="$(safe_name "$project")"
  safe_db="$(safe_name "$db_name")"
  safe_modules="$(compact_name "$module_names")"
  command_output="$(mktemp)"
  saved_output="$FAILURE_DIR/${stamp}_${safe_project}_${safe_db}_uninstall_${safe_modules}.log"
  set +e
  docker exec -i -e LOG_ATTACHMENTS=False -e MODULE_NAMES="$module_names" "$container" \
    odoo shell -c /home/odoo/srv/conf/odoo.conf -d "$db_name" --no-http > "$command_output" 2>&1 <<'PY'
import os

module_names = [name.strip() for name in os.environ.get("MODULE_NAMES", "").split(",") if name.strip()]
if not module_names:
    raise SystemExit("Aucun module fourni.")

modules = env["ir.module.module"].search([("name", "in", module_names)])
found = set(modules.mapped("name"))
missing = sorted(set(module_names) - found)
if missing:
    print("Module(s) introuvable(s): " + ", ".join(missing))

installed = modules.filtered(lambda module: module.state == "installed")
skipped = modules - installed
if skipped:
    print("Module(s) ignore(s) car non installes: " + ", ".join(skipped.mapped("name")))

if not installed:
    raise SystemExit("Aucun module installe a desinstaller.")

print("Desinstallation: " + ", ".join(installed.mapped("name")))
installed.button_immediate_uninstall()
env.cr.commit()
print("Desinstallation terminee.")
PY
  code=$?
  set -e
  if [ "$code" -eq 0 ]; then
    cat "$command_output"
  else
    cp "$command_output" "$saved_output"
    print_failure_context "$command_output"
  fi

  echo ""
  echo "Redemarrage du serveur Odoo..."
  start_odoo_server "$project"

  if [ "$code" -ne 0 ]; then
    echo "La desinstallation a echoue avec le code $code."
    echo "Sortie complete: $saved_output"
    rm -f "$command_output"
    return "$code"
  fi

  rm -f "$command_output"
  echo "Operation terminee."
  echo "URL Odoo: $(project_url "$project")"
}

update_all_odoo_modules() {
  local project="$1"
  local db_name="$2"

  require_docker
  if [ -z "$db_name" ] || [ "$db_name" = "postgres" ]; then
    die "Nom de base Odoo invalide: $db_name"
  fi

  echo ""
  echo "Mise a jour de tous les modules Odoo"
  echo "Equivalent: odoo -d $db_name -u all --stop-after-init"
  run_odoo_module_command "$project" "$db_name" "all" "-u"
}

installed_local_modules() {
  local project="$1"
  local db_name="$2"
  local path
  local addons_dir
  local installed_file
  local local_file
  local modules
  local entry
  local module

  path="$(project_path "$project")"
  addons_dir="$path/odoo/addons"
  [ -d "$addons_dir" ] || die "Dossier addons introuvable: $addons_dir"

  installed_file="$(mktemp)"
  local_file="$(mktemp)"

  docker exec "postgresql-$project" psql -U postgres -d "$db_name" -Atc \
    "select name from ir_module_module where state = 'installed' order by name;" > "$installed_file"

  for entry in "$addons_dir"/*; do
    [ -d "$entry" ] || continue
    if [ -f "$entry/__manifest__.py" ] || [ -f "$entry/__openerp__.py" ]; then
      module="$(basename "$entry")"
      printf '%s\n' "$module"
    fi
  done | sort -u > "$local_file"

  modules="$(comm -12 "$installed_file" "$local_file" | paste -sd, -)"
  rm -f "$installed_file" "$local_file"

  printf '%s\n' "$modules"
}

update_local_odoo_modules() {
  local project="$1"
  local db_name="$2"
  local modules

  require_docker
  if [ -z "$db_name" ] || [ "$db_name" = "postgres" ]; then
    die "Nom de base Odoo invalide: $db_name"
  fi

  start_project "$project"
  modules="$(installed_local_modules "$project" "$db_name")"
  if [ -z "$modules" ]; then
    die "Aucun addon projet installe trouve dans $(project_path "$project")/odoo/addons pour la base $db_name."
  fi

  echo ""
  echo "Mise a jour des addons projet installes"
  echo "Equivalent: odoo -d $db_name -u $modules --stop-after-init"
  run_odoo_module_command "$project" "$db_name" "$modules" "-u"
}

update_all_odoo_modules_interactive() {
  local project="$1"
  local db_name

  require_docker
  start_project "$project"
  list_databases "$project"

  echo "Nom de la base Odoo a mettre a jour:"
  read -r db_name
  if [ -z "$db_name" ]; then
    die "Nom de base vide."
  fi

  update_all_odoo_modules "$project" "$db_name"
}

update_local_odoo_modules_interactive() {
  local project="$1"
  local db_name

  require_docker
  start_project "$project"
  list_databases "$project"

  echo "Nom de la base Odoo dont les addons projet installes doivent etre mis a jour:"
  read -r db_name
  if [ -z "$db_name" ]; then
    die "Nom de base vide."
  fi

  update_local_odoo_modules "$project" "$db_name"
}

update_odoo_module() {
  local project="$1"
  local db_name module_names action module_option

  require_docker

  start_project "$project"
  list_databases "$project"

  echo "Nom de la base a mettre a jour:"
  read -r db_name
  if [ -z "$db_name" ]; then
    die "Nom de base vide."
  fi

  echo "Nom du module a mettre a jour (ex: sale) ou plusieurs separes par une virgule (ex: sale,stock):"
  echo "Vous pouvez aussi saisir: all"
  read -r module_names
  if [ -z "$module_names" ]; then
    die "Nom de module vide."
  fi

  echo ""
  echo "Action a executer:"
  echo "1) Mettre a jour un module deja installe"
  echo "2) Installer un nouveau module"
  echo "3) Desinstaller un module installe"
  echo ""
  printf "Votre choix [1]: " >&2
  read -r action
  case "${action:-1}" in
    1)
      module_option="-u"
      ;;
    2)
      module_option="-i"
      ;;
    3)
      run_odoo_uninstall_command "$project" "$db_name" "$module_names"
      return
      ;;
    *)
      die "Choix invalide: $action"
      ;;
  esac

  run_odoo_module_command "$project" "$db_name" "$module_names" "$module_option"
}

open_database_manager() {
  local project="$1"
  local url

  start_project "$project"
  url="$(project_url "$project")web/database/manager"
  echo ""
  echo "Ouverture de la creation/gestion de base Odoo:"
  echo "$url"
  echo "Master password habituel: odoo"
  open_url "$url"
}

snapshot_projects() {
  local output_file="$1"

  list_projects > "$output_file"
}

detect_new_project() {
  local before_file="$1"
  local after_file="$2"

  comm -13 "$before_file" "$after_file" | tail -n 1
}

update_project() {
  local project="$1"
  local path

  require_docker
  path="$(project_path "$project")"
  compose_file_for "$path" >/dev/null 2>&1 || die "Projet introuvable ou sans fichier compose: $project"

  echo ""
  echo "Mise a jour du projet $project"
  (
    cd "$path"

    if [ -d .git ]; then
      echo "Git pull..."
      git pull --ff-only
    else
      echo "Pas de depot Git dans ce projet."
    fi

    echo "Docker pull..."
    docker compose pull

    echo "Redemarrage compose..."
    docker compose up -d
  )

  echo "Mise a jour terminee: $project"
}

update_all_projects() {
  local project

  list_projects | while IFS= read -r project; do
    update_project "$project"
  done
}

show_logs() {
  local project="$1"
  local path service container

  require_docker
  container="odoo-$project"
  path="$(project_path "$project")"
  compose_file_for "$path" >/dev/null 2>&1 || die "Projet introuvable ou sans fichier compose: $project"

  if is_running "$container" && docker exec "$container" test -f /home/odoo/srv/data/odoo.log >/dev/null 2>&1; then
    docker exec "$container" tail -f /home/odoo/srv/data/odoo.log
    return
  fi

  (
    cd "$path"
    service="$(docker compose config --services 2>/dev/null | awk '/odoo/ {print; exit}' || true)"
    if [ -n "$service" ]; then
      docker compose logs -f "$service"
    else
      docker logs -f "$container"
    fi
  )
}

open_odoo_shell() {
  local project="$1"

  require_docker
  is_running "odoo-$project" || start_project "$project"
  docker exec -it "odoo-$project" bash
}

create_project() {
  local before_file after_file new_project

  require_docker

  if [ -d "$VENV_DIR" ]; then
    # shellcheck disable=SC1091
    . "$VENV_DIR/bin/activate"
  elif has_cmd python3.12; then
    python3.12 -m venv "$VENV_DIR"
    # shellcheck disable=SC1091
    . "$VENV_DIR/bin/activate"
  else
    die "python3.12 est introuvable."
  fi

  start_traefik

  mkdir -p "$WORKSPACE"
  cd "$WORKSPACE"

  before_file="$(mktemp)"
  after_file="$(mktemp)"
  snapshot_projects "$before_file"

  echo ""
  echo "Creation d'un nouveau projet Odoo local"
  echo ""
  echo "Important:"
  echo "Quand Brainkeys demande:"
  echo "  Souhaitez-vous executer les conteneurs du projet ?"
  echo "Repondez plutot: Non"
  echo ""
  echo "Le gestionnaire reprendra ensuite la main pour demarrer Odoo,"
  echo "attendre le conteneur, afficher l'URL et ouvrir la creation de base."
  echo ""

  set +e
  _TYPER_STANDARD_TRACEBACK=1 brainkeys riplika
  brainkeys_code=$?
  set -e
  if [ "$brainkeys_code" -ne 0 ]; then
    print_brainkeys_failure_help
    rm -f "$before_file" "$after_file"
    return "$brainkeys_code"
  fi

  snapshot_projects "$after_file"
  new_project="$(detect_new_project "$before_file" "$after_file")"
  rm -f "$before_file" "$after_file"

  if [ -z "$new_project" ]; then
    echo ""
    echo "Aucun nouveau projet detecte automatiquement."
    echo "Utilisez l'option 1 pour voir les projets, puis l'option 2 ou 4 pour le demarrer."
    return 0
  fi

  echo ""
  echo "Nouveau projet detecte: $new_project"
  echo "Demarrage controle du projet et ouverture de la creation de base..."
  open_database_manager "$new_project"
}

pick_project() {
  local choice count project projects_file

  projects_file="$(mktemp)"
  list_projects > "$projects_file"
  count="$(wc -l < "$projects_file" | tr -d ' ')"

  if [ "$count" -eq 0 ]; then
    rm -f "$projects_file"
    die "Aucun projet Odoo local trouve dans $WORKSPACE."
  fi

  echo "" >&2
  echo "Choisir un projet" >&2
  awk '{ printf "  %s) %s\n", NR, $0 }' "$projects_file" >&2
  echo "" >&2
  printf "Numero ou nom du projet: " >&2
  read -r choice

  case "$choice" in
    ''|*[!0-9]*)
      project="$(grep -Fx "$choice" "$projects_file" || true)"
      ;;
    *)
      if [ "$choice" -ge 1 ] && [ "$choice" -le "$count" ]; then
        project="$(sed -n "${choice}p" "$projects_file")"
      else
        project=""
      fi
      ;;
  esac

  rm -f "$projects_file"

  if [ -n "$project" ]; then
    printf '%s\n' "$project"
    return
  fi

  die "Choix invalide: $choice"
}

interactive_menu() {
  local choice project url

  while true; do
    echo ""
    echo "Gestionnaire Odoo local"
    echo "1) Voir toutes les bases / projets"
    echo "2) Demarrer et ouvrir un projet"
    echo "3) Arreter un projet"
    echo "4) Voir les bases PostgreSQL d'un projet"
    echo "5) Creer une base Odoo dans un projet"
    echo "6) Installer / mettre a jour un module Odoo"
    echo "7) Mettre a jour les addons projet installes d'une base"
    echo "8) Mettre a jour tous les modules Odoo d'une base (avance)"
    echo "9) Mettre a jour le code / les images d'un projet"
    echo "10) Mettre a jour le code / les images de tous les projets"
    echo "11) Creer un nouveau projet local (repondre Non au demarrage Brainkeys)"
    echo "12) Voir les logs Odoo"
    echo "13) Ouvrir un shell dans Odoo"
    echo "0) Quitter"
    echo ""
    printf "Votre choix: "
    read -r choice

    case "$choice" in
      1)
        print_dashboard
        ;;
      2)
        project="$(pick_project)"
        start_project "$project"
        url="$(project_url "$project")"
        open_url "$url"
        ;;
      3)
        project="$(pick_project)"
        stop_project "$project"
        ;;
      4)
        project="$(pick_project)"
        list_databases "$project"
        ;;
      5)
        project="$(pick_project)"
        open_database_manager "$project"
        ;;
      6)
        project="$(pick_project)"
        update_odoo_module "$project"
        ;;
      7)
        project="$(pick_project)"
        update_local_odoo_modules_interactive "$project"
        ;;
      8)
        project="$(pick_project)"
        update_all_odoo_modules_interactive "$project"
        ;;
      9)
        project="$(pick_project)"
        update_project "$project"
        ;;
      10)
        update_all_projects
        ;;
      11)
        create_project
        ;;
      12)
        project="$(pick_project)"
        show_logs "$project"
        ;;
      13)
        project="$(pick_project)"
        open_odoo_shell "$project"
        ;;
      0)
        exit 0
        ;;
      *)
        echo "Choix invalide."
        ;;
    esac
  done
}

usage() {
  echo "Usage:"
  echo "  ./odoo_manager.sh"
  echo "  ./odoo_manager.sh --list"
  echo "  ./odoo_manager.sh --start PROJET"
  echo "  ./odoo_manager.sh --stop PROJET"
  echo "  ./odoo_manager.sh --dbs PROJET"
  echo "  ./odoo_manager.sh --create-db PROJET"
  echo "  ./odoo_manager.sh --update-module PROJET BASE MODULE"
  echo "  ./odoo_manager.sh --install-module PROJET BASE MODULE"
  echo "  ./odoo_manager.sh --uninstall-module PROJET BASE MODULE"
  echo "  ./odoo_manager.sh --update-local-modules PROJET BASE"
  echo "  ./odoo_manager.sh --update-all-modules PROJET BASE"
  echo "  ./odoo_manager.sh --update PROJET"
  echo "  ./odoo_manager.sh --update-all"
  echo "  ./odoo_manager.sh --create-project"
  echo "  ./odoo_manager.sh --install-traefik"
  echo "  ./odoo_manager.sh --logs PROJET"
  echo "  ./odoo_manager.sh --shell PROJET"
}

main() {
  case "${1:-}" in
    "")
      interactive_menu
      ;;
    --list)
      print_dashboard
      ;;
    --start)
      [ -n "${2:-}" ] || die "Nom de projet manquant."
      start_project "$2"
      ;;
    --stop)
      [ -n "${2:-}" ] || die "Nom de projet manquant."
      stop_project "$2"
      ;;
    --dbs)
      [ -n "${2:-}" ] || die "Nom de projet manquant."
      list_databases "$2"
      ;;
    --create-db)
      [ -n "${2:-}" ] || die "Nom de projet manquant."
      open_database_manager "$2"
      ;;
    --update-module)
      [ -n "${2:-}" ] || die "Nom de projet manquant."
      [ -n "${3:-}" ] || die "Nom de base manquant."
      [ -n "${4:-}" ] || die "Nom de module manquant."
      require_docker
      run_odoo_module_command "$2" "$3" "$4" "-u"
      ;;
    --install-module)
      [ -n "${2:-}" ] || die "Nom de projet manquant."
      [ -n "${3:-}" ] || die "Nom de base manquant."
      [ -n "${4:-}" ] || die "Nom de module manquant."
      require_docker
      run_odoo_module_command "$2" "$3" "$4" "-i"
      ;;
    --uninstall-module)
      [ -n "${2:-}" ] || die "Nom de projet manquant."
      [ -n "${3:-}" ] || die "Nom de base manquant."
      [ -n "${4:-}" ] || die "Nom de module manquant."
      require_docker
      run_odoo_uninstall_command "$2" "$3" "$4"
      ;;
    --update-all-modules)
      [ -n "${2:-}" ] || die "Nom de projet manquant."
      [ -n "${3:-}" ] || die "Nom de base manquant."
      update_all_odoo_modules "$2" "$3"
      ;;
    --update-local-modules)
      [ -n "${2:-}" ] || die "Nom de projet manquant."
      [ -n "${3:-}" ] || die "Nom de base manquant."
      update_local_odoo_modules "$2" "$3"
      ;;
    --update)
      [ -n "${2:-}" ] || die "Nom de projet manquant."
      update_project "$2"
      ;;
    --update-all)
      update_all_projects
      ;;
    --create-project)
      create_project
      ;;
    --install-traefik)
      install_traefik
      ;;
    --logs)
      [ -n "${2:-}" ] || die "Nom de projet manquant."
      show_logs "$2"
      ;;
    --shell)
      [ -n "${2:-}" ] || die "Nom de projet manquant."
      open_odoo_shell "$2"
      ;;
    --help|-h)
      usage
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
