# Image de la distribution WSL « SDK-Manager », exportée en fichier .wsl.
#
# Elle n'est jamais exécutée comme conteneur : `docker export` en produit l'archive
# racine, que Windows importe avec `wsl --install --from-file`. Tout ce dont le
# gestionnaire a besoin y est figé et testé en CI : systemd, Docker Engine, Git et SSH.
FROM debian:12-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG SDK_MANAGER_VERSION=0.0.0

# systemd démarre les services de la distribution (Docker) ; iptables est requis par
# Docker sur Debian 12, en mode nft.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        dbus \
        git \
        gnupg \
        iproute2 \
        iptables \
        openssh-client \
        procps \
        sudo \
        systemd \
        systemd-sysv \
        tzdata \
        unzip \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb

# Utilisateur du poste : uid 1000, membre du groupe docker, sudo sans mot de passe
# pour le dépannage. Le gestionnaire, lui, utilise `wsl -u root` quand il a besoin de droits.
RUN useradd --create-home --shell /bin/bash --uid 1000 sdk \
    && usermod -aG docker sdk \
    && echo "sdk ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/sdk \
    && chmod 0440 /etc/sudoers.d/sdk \
    && mkdir -p /home/sdk/Odoo-projects /home/sdk/.ssh /opt/sdk-manager \
    && chown -R sdk:sdk /home/sdk \
    && chmod 700 /home/sdk/.ssh

COPY wsl.conf /etc/wsl.conf
COPY daemon.json /etc/docker/daemon.json
COPY provision.sh /opt/sdk-manager/provision.sh
# Clés d'hôte vérifiées, si le dépôt en fournit : sinon le premier clone les accepte
# une fois (StrictHostKeyChecking=accept-new), comme le fait déjà le gestionnaire.
COPY known_hosts /etc/ssh/ssh_known_hosts

RUN chmod 0755 /opt/sdk-manager/provision.sh \
    && systemctl enable docker \
    && printf '%s\n' "$SDK_MANAGER_VERSION" > /etc/sdk-manager-release

CMD ["/bin/bash"]
