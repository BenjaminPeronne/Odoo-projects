# Audit des performances Windows et WSL — 17 septembre 2026

## Conditions

- Poste : Windows 11 Pro 26200, 12 cœurs logiques, 16 Go de RAM, mode développeur et chemins longs activés, Defender actif.
- Docker Desktop (moteur 29.7.2) sur WSL 2 2.4.12. Distributions `Ubuntu` (arrêtée) et `docker-desktop`. La VM Docker dispose de 8 Go de RAM, la valeur par défaut, faute de `.wslconfig`.
- Code : `main` (`eed3cfe`), backend lancé depuis les sources avec Python 3.12.10 et une copie de la configuration. L'application installée n'a pas été touchée.
- Workspace réel `C:\Users\Aymerick\Odoo-projects` : `Caritel_v18` (1 416 modules), `DEMO_CPL` et `DEMO_CPL03` (1 447), `SIMPAC_v16` (1 171, conteneurs démarrés). Tous les liens d'addons sont des liens NTFS natifs.
- Les processus enfants du backend sont échantillonnés toutes les 20 ms.

Scripts de mesure : `bench.py` (routes), `idle.py` (charge de fond avec un client SSE), `compare_modules.py` (liste des modules avant/après). Ils sont restés hors du dépôt.

## Synthèse

| # | Gravité | Constat | État |
|---|---|---|---|
| 1 | Élevée | Code Odoo, filestore et PostgreSQL servis par des bind mounts 9P depuis `C:\` : E/S 50 à 250 fois plus lentes | Ouvert : décision d'architecture |
| 2 | Élevée | `docker info` lance les 14 plugins CLI de Docker Desktop à chaque sonde | **Corrigé** |
| 3 | Moyenne | Toutes les sondes Docker passent par la CLI (150 à 560 ms et 2 processus par appel) | Ouvert |
| 4 | Moyenne | Liste des modules : 55 % du temps passé dans `PurePath.relative_to` | **Corrigé** |
| 5 | Moyenne | Scripts `.sh` extraits en CRLF sur un poste Windows (`core.autocrlf=true`) | **Corrigé** |
| 6 | Moyenne | `build_desktop.py` lance le test de l'installateur, qui écrase l'installation du poste | Ouvert (audit du 16/09, n° 8) |
| 7 | Faible | Diagnostic : les mêmes `docker inspect` et `psql` sont répétés | Ouvert |
| — | — | Réveils de WSL par les écrans en lecture seule (audit du 16/09, n° 5 et 7) | Vérifié : 0 `wsl.exe`, Ubuntu reste arrêtée |

## Mesures avant et après correction

| Route | Avant | Après |
|---|---:|---:|
| `bootstrap` | 1 368 ms | 832 ms |
| `overview` (sondage de secours et SSE) | 1 117 à 1 190 ms | 672 à 688 ms |
| `system/status` | 794 ms | 407 ms |
| Diagnostic `Caritel_v18` | 998 ms | 589 ms |
| Diagnostic `SIMPAC_v16` (base active) | 3 172 ms | 2 182 ms |
| Liste des modules en cache, dans le processus (meilleur de 3) | 352 à 558 ms | 208 à 369 ms |
| Processus lancés en 60 s, interface ouverte | 173 | 89 |

La liste des modules est strictement identique avant et après sur les 4 projets (5 481 modules). Les mesures HTTP de la liste en cache varient de ±300 ms d'un passage à l'autre : la réponse pèse environ 1 Mo.

## Constats

### 1. Bind mounts 9P : la vraie lenteur « WSL »

Chaque projet monte depuis `C:\` `./odoo/odoo`, `./odoo/addons`, `./odoo/addons-store`, `./odoo_data` (filestore) et `./postgresql_data`. Dans la VM Docker Desktop, ces dossiers sont des montages `9p` (`aname=drvfs`, `msize=65536`) : chaque `stat`, `open` ou `readdir` traverse le pont Windows↔VM.

Dans `odoo-SIMPAC_v16` en fonctionnement :

| Opération | Durée |
|---|---:|
| `find` des `__manifest__.py` dans `odoo` et `addons-store` (2 132) | 87,1 s |
| `find -L addons` à travers les liens (1 158) | 37,9 s |
| `find` des `.py` d'Odoo Community (5 752) | 19,5 s |
| Lecture de 800 fichiers `.py` | 10,2 s |

Comparaison contrôlée dans un même conteneur, sur 3 000 petits fichiers :

| Opération | Bind mount `C:\` (9P) | Volume Docker (ext4 de la VM) | Rapport |
|---|---:|---:|---:|
| Création | 11 282 ms | 232 ms | ×49 |
| `find` | 314 ms | 4 ms | ×78 |
| Lecture | 8 678 ms | 34 ms | ×255 |

C'est la cause principale des démarrages d'Odoo en plusieurs minutes, des `-u` lents et du « Bad Gateway » prolongé mentionné au dépannage. Le backend du gestionnaire n'y est pour rien : ce sont Odoo et PostgreSQL qui lisent à travers 9P.

Point de fiabilité : `dd oflag=dsync` écrit 35,7 Mo/s dans `postgresql_data` contre 0,86 Mo/s dans le conteneur. Le montage 9P ne garantit donc probablement pas les `fsync` de PostgreSQL. Une coupure brutale (arrêt de Docker Desktop, mise en veille forcée) peut corrompre une base locale.

Pistes, de la moins à la plus invasive :

1. **Volume nommé pour `postgresql_data`** (`postgresql-<projet>-data`). C'est le gain le plus simple et la correction du risque `fsync`. Contrepartie : les données ne sont plus visibles dans l'Explorateur. Suppression, sauvegarde et restauration doivent passer par Docker, ce que le gestionnaire fait déjà pour les bases.
2. **Volume nommé pour `odoo_data`** (filestore). Le diagnostic du filestore lit aujourd'hui ce dossier depuis Windows : il faudrait le faire via `docker exec`.
3. **Workspace dans la distribution WSL** (`\\wsl.localhost\Ubuntu\home\…`), déjà pris en charge par le code. Docker lit alors de l'ext4 natif ; c'est Windows (Explorateur, VS Code sans Remote WSL) qui devient lent. Le coût de la liste des modules se déplace vers WSL : le chemin WSL du backend est à remesurer.
4. **Code Odoo et Enterprise dans un volume partagé par version** : une seule copie par version, montée en lecture seule par tous les projets. Gain maximal, mais c'est une refonte de `project_creator` et de la gestion des liens.

À mesurer avant de choisir : le temps de démarrage d'Odoo et d'un `-u base` sur `SIMPAC_v16`, avec bind mounts puis avec volumes (copie du projet, pas le projet réel).

### 2. `docker info` lance les 14 plugins CLI — corrigé

`docker_status` exécutait `docker info --format {{json .ServerVersion}}`. Même avec `--format`, la CLI énumère ses plugins pour la section Client : Docker Desktop en installe 14 (`docker-compose`, `docker-buildx`, `docker-scout`, `docker-ai`…), chacun lancé comme processus. Médiane de 560 ms par appel, contre 199 ms pour `docker version --format {{json .Server.Version}}`.

`docker_status` est appelé par `bootstrap`, `overview`, `system/status` et le diagnostic, et toutes les 10 s par la boucle SSE : 84 processus par minute rien que pour ces plugins.

Correction : `docker version`, qui interroge aussi le moteur (code 1 et message sur stderr s'il est injoignable). La logique d'état est inchangée.

### 3. Toutes les sondes Docker passent par la CLI

Même sans plugins, chaque appel lance `docker.exe` et `conhost.exe` : 150 ms pour `docker ps` ou `docker inspect`, 280 ms pour `docker exec psql`. Dès qu'un client SSE est connecté, `event_watch_loop` lance `docker ps` toutes les 2 s. Resteront 89 processus par minute tant que l'application est ouverte, chacun analysé par Defender.

L'API du moteur répond sur `\\.\pipe\docker_engine` : 3 ms pour `/_ping`, 12 ms pour `/version`, 11 ms pour `/containers/json?all=1`. Un petit client HTTP sur le named pipe (stdlib : `open(pipe, "r+b")`) couvrirait `version`, `ps` et `inspect`, avec repli sur la CLI si le pipe est absent ou si un `DOCKER_HOST` ou un contexte différent est configuré. Gain attendu : `overview` sous 100 ms et plus aucun processus au repos. `docker exec` (psql) reste sur la CLI.

Alternative moins ambitieuse : ralentir `docker ps` quand la fenêtre est masquée, ou utiliser `docker events` en flux continu.

### 4. Liste des modules : `PurePath.relative_to` — corrigé

Profil d'une liste en cache de `Caritel_v18` (1,67 s sous cProfile) : `module_removal_info` → `path_is_relative_to` → `PurePath.relative_to` représente 0,91 s. Pour 4 248 appels, pathlib reconstruit en Python chaque parent du chemin (89 000 objets `Path`).

Correction : comparaison lexicale des chaînes normalisées (`ntpath`/`posixpath.normcase`), même sémantique que `relative_to`, insensible à la casse sous Windows et sans faux positif sur un préfixe (`addons-store-evil`). Test d'équivalence avec pathlib sur 13 cas Windows, UNC et POSIX.

Coûts restants par module, nécessaires à la sécurité des suppressions : `safe_resolve` de chaque lien (0,36 s) et `module_parent_in_layout`, qui reconstruit trois `Path` par module (0,25 s ; ceux-ci pourraient être calculés une seule fois dans `module_layout_context`).

### 5. Scripts `.sh` en CRLF — corrigé

Git pour Windows est installé avec `core.autocrlf=true` au niveau système. `git ls-files --eol` : `i/lf w/crlf` pour tous les `.sh`. `odoo_next_gui.sh` échoue sous `sh`, et `odoo_manager.sh` est embarqué tel quel dans le sidecar (constat 10 du 16/09).

Correction : `.gitattributes` avec `*.sh text eol=lf`, ajouté à la liste blanche de `.gitignore`.

### 6. Le build local écrase l'installation du poste

Toujours présent : `scripts/build_desktop.py` lance `smoke_test_windows_installer.py` dès que `os.name == "nt"`. Sur ce poste, où l'application est installée, un build local la fermerait de force et remplacerait son entrée de désinstallation (détails dans l'audit du 16/09, n° 8). À ne pas lancer tant que le test n'est pas limité à `GITHUB_ACTIONS` ou isolé (`appId`, port et `userData` distincts).

### 7. Diagnostic : appels répétés

Le diagnostic de `SIMPAC_v16` lance 8 `docker.exe` : `docker info`, puis `inspect postgresql-SIMPAC_v16` trois fois, `inspect odoo-…` une fois, et trois `docker exec psql`. Un seul `docker inspect odoo-X postgresql-X` et le regroupement des requêtes SQL dans un seul `psql` économiseraient environ 1 s.

## Configuration du poste

Faite :

- Node.js 22.23.2 et npm 10.9.8 (`winget OpenJS.NodeJS.22`).
- Python 3.12.10 (`winget Python.Python.3.12`), version de la CI. Environnement `.venv/` à la racine (ignoré par Git) avec PyInstaller et psutil.
- `npm ci` dans `odoo-manager-next`.
- Vérifications : 370 tests Python (11 ignorés), `tsc --noEmit`, 13 tests Electron et 6 tests `lib`.

Recommandé, à faire par l'utilisateur :

- **Exclusions Defender** (PowerShell administrateur) pour le workspace et les dépôts : chaque lecture de fichier par Odoo à travers 9P, et chaque `docker.exe` lancé, est analysée en temps réel. Réservé à un poste dont le contenu est de confiance.
- **`%UserProfile%\.wslconfig`** : Docker Desktop occupe par défaut la moitié de la RAM (8 Go), ce qui suffit pour un ou deux projets. Avec plusieurs projets démarrés, `memory=10GB` et `[experimental] autoMemoryReclaim=gradual` limitent le gonflement de `vmmem`.
- Identité Git (`user.name`, `user.email`) : non configurée sur ce poste.

## Ordre de correction proposé

1. Mesurer puis passer `postgresql_data` en volume nommé pour les nouveaux projets, avec une migration proposée pour les projets existants (constat 1, piste 1).
2. Client Docker sur named pipe pour `version`, `ps` et `inspect` (constat 3).
3. Limiter le test de l'installateur à la CI (constat 6).
4. Regrouper les appels du diagnostic (constat 7) et précalculer les parents dans `module_layout_context` (constat 4).
5. Évaluer un workspace dans WSL ou un code Odoo partagé en volume (constat 1, pistes 3 et 4).
