# Audit des performances Windows et WSL — 17 septembre 2026

## Conditions

- Poste : Windows 11 Pro 26200, 12 cœurs logiques, 16 Go de RAM, mode développeur et chemins longs activés, Defender actif.
- Docker Desktop (moteur 29.7.2) sur WSL 2 2.4.12. Distributions `Ubuntu` (arrêtée) et `docker-desktop`. La VM Docker dispose de 8 Go de RAM, la valeur par défaut, faute de `.wslconfig`.
- Code : `main` (`eed3cfe`), backend lancé depuis les sources avec Python 3.12.10 et une copie de la configuration. L'application installée n'a pas été touchée.
- Workspace réel `C:\Users\Aymerick\Odoo-projects` : `Caritel_v18` (1 416 modules), `DEMO_CPL` et `DEMO_CPL03` (1 447), `SIMPAC_v16` (1 171, conteneurs démarrés). Tous les liens d'addons sont des liens NTFS natifs.
- Les processus enfants du backend sont échantillonnés toutes les 20 ms.

Scripts de mesure : `bench.py` (routes), `idle.py` (charge de fond avec un client SSE), `compare_modules.py` (liste des modules avant/après), `bench_pg.sh` (PostgreSQL en bind mount ou en volume), `bench_code.sh` (code Odoo en bind mount ou en volume), `bench_mix.sh` (compromis Odoo et Enterprise en volume). Ils sont restés hors du dépôt.

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

#### Mesure : code Odoo en bind mount ou en volume Docker

Même copie de la base, PostgreSQL en volume dans les deux cas. Le code de `SIMPAC_v16` (`odoo/odoo` et `odoo/addons-store`, sans `.git`) a été copié dans des volumes par `tar`, et les 1 158 liens de `odoo/addons` ont été recréés avec des cibles identiques. Le code est monté en lecture seule dans les deux variantes. La variante volume passe toujours en premier : le bind mount profite du cache, le gain mesuré est donc un minimum.

| Opération | Bind mount `C:\` | Volume Docker | Gain |
|---|---:|---:|---:|
| `find -L addons` (1 158 manifestes) | 33,2 s | 1,2 s | ×27 |
| Chargement d'Odoo, manche 1 (registre) | 82,5 s (75,3 s) | 6,2 s (2,8 s) | ×13 |
| Chargement d'Odoo, manche 2 | 48,4 s (43,6 s) | 3,7 s (1,5 s) | ×13 |
| Chargement d'Odoo, manche 3 | 48,7 s (44,5 s) | 4,3 s (1,8 s) | ×11 |
| `-u base` (248 modules, 119 000 requêtes) | 440,7 s | 234,5 s | ×1,9 |

Les 248 modules sont chargés sans erreur dans les deux variantes, avec les mêmes chemins d'addons. Sur `-u base` en volume, il ne reste que le travail en base : chargement des données XML et requêtes SQL. Le bind mount de la mesure précédente avait donné 370 s : d'une session à l'autre, `-u base` sur `C:\` varie donc de ±15 %, et le gain réel est compris entre ×1,6 et ×1,9.

Coût de la copie : 7 min 22 s pour `odoo/odoo` (37 669 fichiers, 970 Mo) et 15 min 48 s pour `addons-store` (97 830 fichiers, 1,9 Go). La lecture des fichiers par Windows, analysés par Defender, domine ce temps. Le coût est unique par version d'Odoo si le code est partagé, mais un clone Git fait directement dans le volume éviterait ce passage par Windows.

**Conclusion : sortir le code de `C:\` est le levier principal.** Démarrer Odoo devient 11 à 13 fois plus rapide, et une mise à jour complète deux fois plus rapide. Le stockage de PostgreSQL n'accélère que les opérations de masse sur la base.

#### Mesure : compromis Odoo et Enterprise en volume, addons spécifiques sur `C:\`

Les 1 158 liens de `SIMPAC_v16` se répartissent entre 970 modules standard (Community et Enterprise) et 188 modules spécifiques (OCA, Sudokeys, `simpac`). En fichiers, `addons-store` compte 90 734 fichiers standard pour 7 096 spécifiques, soit 7 %. Trois variantes, avec PostgreSQL en volume et le code en lecture seule :

- **bind** : tout sur `C:\`, comme aujourd'hui ;
- **mixte** : `odoo/odoo`, les deux copies d'Enterprise et Community (`simpac_v16/odoo`) en volumes, montés par-dessus leurs dossiers dans `addons-store`. Les addons spécifiques et les liens de `odoo/addons` restent sur `C:\` ;
- **volume** : tout en volumes, à partir des mêmes volumes standard, d'un volume pour les addons spécifiques et d'un volume pour les liens.

Dans chaque manche, l'ordre est volume, mixte, bind : les variantes les plus lentes profitent du cache. `stat -f` vérifie le type de système de fichiers de chaque montage (ext4 ou 9P), et les 1 158 manifestes sont atteints dans les trois cas.

| Opération | Bind | Mixte | Volume |
|---|---:|---:|---:|
| `find -L addons` (1 158 manifestes) | 31,5 s | 12,2 s | 1,2 s |
| Chargement d'Odoo, manche 1 | 95,3 s | 19,7 s | 6,2 s |
| Chargement d'Odoo, manche 2 | 62,1 s | 19,3 s | 4,2 s |
| Chargement d'Odoo, manche 3 | 63,8 s | 19,4 s | 4,9 s |
| `-u base` | 504,2 s | 273,1 s | 233,9 s |

Lecture :

- **Le compromis apporte l'essentiel du gain sur `-u`** : 273 s contre 504 s en bind, seulement 17 % de plus que tout en volume.
- **Au démarrage, il divise le temps par 3 à 5**, mais reste environ 4 fois plus lent que tout en volume (19,5 s contre 4 à 6 s), avec un résultat très stable d'une manche à l'autre.
- Hypothèse, non mesurée : l'écart vient surtout des liens de `odoo/addons` restés en 9P. Chaque fichier ouvert par Odoo via `addons/<module>/…` passe par une recherche et un `readlink` dans ce dossier 9P avant d'atteindre le volume. Ce dossier ne contient que des liens gérés par le gestionnaire : le placer en volume est faisable sans gêner le développement depuis Windows, les addons spécifiques restant sur `C:\`.
- Les variations du bind mount d'une session à l'autre (370 s, 441 s puis 504 s pour `-u base`) confirment son instabilité.

Coût de la copie des parties standard : 37 669 fichiers d'`odoo/odoo` (6 min 15 s), 38 470 de Community (6 min 19 s) et 26 010 et 26 254 pour les deux Enterprise (3 min 56 s chacune). `SIMPAC_v16` embarque deux copies de Community et d'Enterprise : un volume par version d'Odoo, partagé entre les projets, n'en garderait qu'une.

Pistes, de la moins à la plus invasive :

1. **Volume nommé pour `postgresql_data`** (`postgresql-<projet>-data`) : restaurations et imports 2 à 4 fois plus rapides, sans effet sur le démarrage. Contrepartie : les données ne sont plus visibles dans l'Explorateur. Suppression, sauvegarde et restauration doivent passer par Docker, ce que le gestionnaire fait déjà pour les bases. Il faut aussi migrer les projets existants (dump, puis restauration).
2. **Volume nommé pour `odoo_data`** (filestore). Le diagnostic du filestore lit aujourd'hui ce dossier depuis Windows : il faudrait le faire via `docker exec`.
3. **Code Odoo et Enterprise hors de `C:\`** : démarrage ×11 à ×13 et `-u` ×1,9, mesurés. Deux formes possibles :
   - un workspace dans la distribution WSL (`\\wsl.localhost\Ubuntu\home\…`), déjà pris en charge par le code. Docker lit alors l'ext4 de WSL, comparable à un volume. Contreparties : l'Explorateur et VS Code sans Remote WSL deviennent lents, et le chemin WSL du backend (liste des modules) est à remesurer ;
   - des volumes gérés par le gestionnaire : Odoo et Enterprise par version, clonés directement dans le volume, et les addons du projet dans un volume par projet. C'est une refonte de `project_creator`, de l'import de modules et de la gestion des liens (création et vérification via `docker run`). Le développement d'addons clients depuis Windows devient aussi moins direct : il faut VS Code Dev Containers, ou garder uniquement les addons du projet en bind mount.

   Compromis mesuré : Odoo et Enterprise en volume, addons spécifiques du projet sur `C:\`. Démarrage ×3 à ×5 et `-u` ×1,8, tout en gardant les addons du projet modifiables depuis Windows. Ajouter les liens de `odoo/addons` aux volumes devrait rapprocher le démarrage de celui du tout-volume, à confirmer par une mesure.

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

1. Sortir le code standard de `C:\` : Odoo et Enterprise dans un volume par version, addons du projet sur `C:\` (constat 1, piste 3). Mesurer d'abord la même configuration avec les liens de `odoo/addons` en volume.
2. Client Docker sur named pipe pour `version`, `ps` et `inspect` (constat 3).
3. Limiter le test de l'installateur à la CI (constat 6).
4. `postgresql_data` en volume nommé pour les nouveaux projets, si les restaurations de copies sont fréquentes (constat 1, piste 1).
5. Regrouper les appels du diagnostic (constat 7), précalculer les parents dans `module_layout_context` (constat 4) et nettoyer les dossiers de préparation abandonnés (constat 8).
