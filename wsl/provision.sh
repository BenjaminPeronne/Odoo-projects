#!/bin/sh
# Prépare la distribution SDK-Manager, ou la met à niveau après une mise à jour de
# l'application. Exécuté comme root par le gestionnaire (wsl -u root), à chaque
# démarrage quand la version installée diffère de celle de l'application.
#
# Rejouable sans risque : chaque étape vérifie l'état avant d'agir et ne touche
# jamais aux projets ni aux bases.
set -eu

SDK_USER=${SDK_USER:-sdk}
SDK_HOME=$(getent passwd "$SDK_USER" | cut -d: -f6)
SDK_HOME=${SDK_HOME:-/home/$SDK_USER}
WORKSPACE=${SDK_WORKSPACE:-$SDK_HOME/Odoo-projects}
RELEASE_FILE=/etc/sdk-manager-release
VERSION=${SDK_MANAGER_VERSION:-}

log() { printf '%s\n' "$*"; }

log "Provisionnement de SDK-Manager (version demandée: ${VERSION:-inconnue})."

if [ ! -d "$SDK_HOME" ]; then
    log "Utilisateur $SDK_USER sans dossier personnel : environnement inattendu." >&2
    exit 1
fi

install -d -o "$SDK_USER" -g "$SDK_USER" -m 0755 "$WORKSPACE"
install -d -o "$SDK_USER" -g "$SDK_USER" -m 0700 "$SDK_HOME/.ssh"
install -d -m 0755 /opt/sdk-manager

# Docker doit être démarré et le rester : les projets en dépendent.
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    systemctl enable docker >/dev/null 2>&1 || true
    systemctl start docker >/dev/null 2>&1 || log "Docker n'a pas démarré : voir journalctl -u docker." >&2
else
    log "systemd absent : vérifier [boot] systemd=true dans /etc/wsl.conf." >&2
fi

# L'utilisateur doit pouvoir parler au démon sans sudo.
if ! id -nG "$SDK_USER" | tr ' ' '\n' | grep -qx docker; then
    usermod -aG docker "$SDK_USER"
    log "Utilisateur $SDK_USER ajouté au groupe docker."
fi

if [ -n "$VERSION" ]; then
    printf '%s\n' "$VERSION" > "$RELEASE_FILE"
fi

log "Provisionnement terminé."
