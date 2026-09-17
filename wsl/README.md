# Distribution WSL « SDK-Manager »

Environnement Linux que l'application installe elle-même sous Windows. Les projets Odoo, Docker et le backend y tournent sur un système de fichiers Linux : Odoo démarre en 4 à 6 s au lieu de 48 à 95 s, et `-u base` prend 234 s au lieu de 504 s ([mesures](../docs/audit-performances-windows-2026-09-17.md)).

L'utilisateur n'ouvre jamais de terminal : l'application importe l'image, la démarre et la maintient à jour.

## Contenu

| Fichier | Rôle |
|---|---|
| `Dockerfile` | Racine du système : Debian 12, systemd, Docker Engine et Compose, Git, OpenSSH, utilisateur `sdk` (uid 1000) |
| `wsl.conf` | `systemd=true`, utilisateur par défaut, `appendWindowsPath=false`, disques Windows montés sous `/mnt` |
| `daemon.json` | Configuration de Docker, avec rotation des journaux de conteneurs |
| `provision.sh` | Préparation et mises à niveau, rejouable, exécuté comme `root` quand la version change |
| `known_hosts` | Clés d'hôte SSH vérifiées, vide par défaut (voir plus bas) |

L'image ne contient pas le backend : l'application y copie son propre exécutable Linux dans `/opt/sdk-manager/` à chaque mise à jour.

## Construire l'image

```bash
python3 scripts/build_wsl_image.py
```

Le script produit `dist/wsl/sdk-manager-<version>.wsl` et son empreinte `.sha256`. Il utilise `docker build` puis `docker export` : l'image n'est jamais exécutée comme conteneur, seule sa racine sert. La CI (`wsl-image`) la reconstruit à chaque version et vérifie son contenu.

Installation manuelle, pour une mise au point :

```bash
wsl --install --from-file dist/wsl/sdk-manager-0.5.0.wsl --name SDK-Manager --location "%LOCALAPPDATA%\SDK Local Manager\wsl" --no-launch
```

Suppression complète, projets compris :

```bash
wsl --unregister SDK-Manager
```

## Clés d'hôte GitLab

`known_hosts` est vide tant que l'empreinte de `gitlab.sudokeys.com:10022` n'a pas été vérifiée hors bande. Le premier clone accepte alors la clé présentée et la mémorise, comme aujourd'hui sous Windows et macOS.

Pour la figer : relever l'empreinte depuis GitLab ou une machine déjà appairée, la comparer, l'ajouter au fichier, puis reconstruire l'image. Une attaque de l'homme du milieu au premier clone devient impossible.

## Mises à jour

L'application ne réimporte jamais la distribution : les projets y vivent. Quand sa version diffère de `/etc/sdk-manager-release`, elle exécute `provision.sh`, qui vérifie l'état avant d'agir et ne touche ni aux projets ni aux bases.
