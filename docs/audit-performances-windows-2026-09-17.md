# Audit des performances Windows et WSL — 17 septembre 2026

## Conditions

- Poste : Windows 11 Pro 26200, 12 cœurs logiques, 16 Go de RAM, mode développeur et chemins longs activés, Defender actif.
- Docker Desktop (moteur 29.7.2) sur WSL 2 2.4.12. Distributions `Ubuntu` (arrêtée) et `docker-desktop`. La VM Docker dispose de 8 Go de RAM, la valeur par défaut, faute de `.wslconfig`.
- Code : `main` (`eed3cfe`), backend lancé depuis les sources avec Python 3.12.10 et une copie de la configuration. L'application installée n'a pas été touchée.
- Workspace réel `C:\Users\Aymerick\Odoo-projects` : `Caritel_v18` (1 416 modules), `DEMO_CPL` et `DEMO_CPL03` (1 447), `SIMPAC_v16` (1 171, conteneurs démarrés). Tous les liens d'addons sont des liens NTFS natifs.
- Les processus enfants du backend sont échantillonnés toutes les 20 ms.

Scripts de mesure : `bench.py` (routes), `idle.py` (charge de fond avec un client SSE), `compare_modules.py` (liste des modules avant/après), `bench_pg.sh` (PostgreSQL en bind mount ou en volume). Ils sont restés hors du dépôt.

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
| 8 | Faible | 6,8 Go de dossiers de création interrompue jamais nettoyés dans `.odoo_manager_staging` | Ouvert |
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

Fiabilité : `pg_test_fsync` montre qu'en 9P `open_datasync` n'est pas synchronisé (211 µs par écriture, contre 3,9 ms sur un volume). `fdatasync`, la méthode par défaut de PostgreSQL sous Linux, a en revanche la même latence dans les deux cas (3,4 et 3,8 ms). Les validations de PostgreSQL semblent donc bien écrites sur disque. Le `dd oflag=dsync` rapide observé plus haut s'explique par ce `O_DSYNC` ignoré, non par un `fsync` ignoré.

#### Mesure : PostgreSQL en bind mount ou en volume Docker

Copie de `SIMPAC_v16/test` (1,1 Go, 248 modules Odoo 16) obtenue par `pg_dump`, puis restaurée dans deux instances `postgres:14` jetables : l'une avec son dossier de données sur `C:\` (9P), l'autre dans un volume nommé. Le code Odoo reste en bind mount, identique pour les deux. Les passages Odoo alternent les variantes pour répartir l'effet du cache de fichiers.

| Opération | Bind mount `C:\` | Volume Docker | Gain |
|---|---:|---:|---:|
| `initdb` et démarrage | 20,4 s | 6,9 s | ×3,0 |
| Restauration de la base (`pg_restore -j 4`) | 295,9 s | 153,7 s | ×1,9 |
| `ANALYZE` de la base | 60,7 s | 15,9 s | ×3,8 |
| Insertion en masse (`pgbench -i`, 2 M lignes) | 62,5 s | 6,6 s | ×9,5 |
| `pgbench` lecture seule, 8 clients | 9 633 tps | 26 245 tps | ×2,7 |
| `pgbench` TPC-B (petites écritures validées), 8 clients | 686 tps | 638 tps | ≈ |
| Chargement d'Odoo, passage 1 (bind, puis volume) | 102,7 s | 62,8 s | biaisé par le cache |
| Chargement d'Odoo, passage 2 (bind, puis volume) | 52,9 s | 49,4 s | ×1,07 |
| `-u base` (volume d'abord, puis bind) | 370,1 s | 392,3 s | ≈ |

Lecture :

- **Le volume accélère fortement les opérations de masse sur la base** : restauration d'une copie RIKA ou d'une sauvegarde, `ANALYZE`, imports, lectures lourdes.
- **Il ne change presque rien au démarrage d'Odoo ni à `-u`.** Les petites validations sont limitées par `fdatasync`, dont le coût est le même, et le temps d'Odoo se passe à lire le code en 9P. Le passage 1 (102,7 s puis 62,8 s) et le passage 2 (52,9 s) montrent que le cache du code pèse plus que le stockage de la base. Sur `-u base`, 300 s sur 365 servent à charger les 248 modules (119 299 requêtes).

Pistes, de la moins à la plus invasive :

1. **Volume nommé pour `postgresql_data`** (`postgresql-<projet>-data`) : restaurations et imports 2 à 4 fois plus rapides, sans effet sur le démarrage. Contrepartie : les données ne sont plus visibles dans l'Explorateur. Suppression, sauvegarde et restauration doivent passer par Docker, ce que le gestionnaire fait déjà pour les bases. Il faut aussi migrer les projets existants (dump, puis restauration).
2. **Volume nommé pour `odoo_data`** (filestore). Le diagnostic du filestore lit aujourd'hui ce dossier depuis Windows : il faudrait le faire via `docker exec`.
3. **Code Odoo et Enterprise hors de `C:\`** : c'est le seul levier sur le démarrage et `-u`. Deux formes possibles :
   - un workspace dans la distribution WSL (`\\wsl.localhost\Ubuntu\home\…`), déjà pris en charge par le code. Windows (Explorateur, VS Code sans Remote WSL) devient alors lent, et le chemin WSL du backend est à remesurer ;
   - un volume par version d'Odoo, monté en lecture seule par tous les projets. C'est une refonte de `project_creator` et de la gestion des liens.

   À mesurer en priorité : chargement d'Odoo et `-u base` avec le code copié dans un volume.

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

### 8. Dossiers de préparation abandonnés

`Odoo-projects/.odoo_manager_staging` contient 8 dossiers (`DEMO_CPL-…`, du 8 au 11 septembre), soit 6,8 Go. La création prépare le projet dans ce dossier puis le déplace, mais une création interrompue (fermeture, échec du clone) n'est jamais nettoyée, ni au démarrage ni par l'interface. Proposer leur suppression au démarrage, pour les dossiers sans action en cours, libérerait l'espace sans risque. Rien n'a été supprimé pendant cet audit.

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

1. Mesurer le chargement d'Odoo et `-u base` avec le code dans un volume ou dans WSL (constat 1, piste 3). C'est le seul levier mesuré sur les temps d'attente quotidiens.
2. Client Docker sur named pipe pour `version`, `ps` et `inspect` (constat 3).
3. Limiter le test de l'installateur à la CI (constat 6).
4. `postgresql_data` en volume nommé pour les nouveaux projets, si les restaurations de copies sont fréquentes (constat 1, piste 1).
5. Regrouper les appels du diagnostic (constat 7), précalculer les parents dans `module_layout_context` (constat 4) et nettoyer les dossiers de préparation abandonnés (constat 8).
