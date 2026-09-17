# Audit de la version Windows — 16 septembre 2026

## Périmètre

SDK Local Manager 0.5.0, commit `3a7aa22`. Lecture du code Electron (`main`, `runtime`, `preload`, `credentials`), de `odoo_manager_core` (plateforme, système, configuration, services projet et création), des parties Windows/WSL de `odoo_manager_web.py`, des scripts de build et de l'installateur NSIS.

Contrôles dynamiques sur Windows 11 Pro 26200 : Docker Desktop 4.79 (moteur 29.5.3), WSL 2 avec Ubuntu, mode développeur activé, installateur CI `app-v0.5.0-build8` (non signé, SHA-256 `4E7B186E…4B510740`) installé en mise à niveau de la 0.3.0. Projets réels : `Caritel`, créé par la 0.3.0, avec 1 416 addons liés, et `DEMO_01`, créé par la 0.5.0 pendant la recette, avec 787 addons liés.

Aucune base ni aucun conteneur existant n'a été modifié. Les mesures sont indicatives : elles dépendent du cache disque et de la charge de la machine.

## Synthèse

| # | Gravité | Constat |
|---|---|---|
| 1 | Élevée | Anciens projets : liens d'addons créés par WSL, illisibles par Windows |
| 2 | Élevée | Liste des modules : scan via WSL de 32 à 73 s, contre 0,2 s en natif |
| 3 | Élevée | Cache WSL : mode de suppression obsolète après changement d'un lien |
| 4 | Élevée | La CI Windows n'exécute jamais les chemins WSL |
| 5 | Moyenne | Sondage Docker qui réveille WSL toutes les 10 s environ |
| 6 | Moyenne | Création de projet via Git dans WSL sans options SSH non interactives |
| 7 | Moyenne | Sonde WSL relancée à chaque construction de commande Git |
| 8 | Moyenne | Le test de l'installateur casse l'installation d'un poste de développement |
| 9 | Moyenne | Installateur et backend non signés |
| 10 | Faible | `odoo_manager.sh` embarqué en CRLF ; code mort, mais requis au démarrage |
| 11 | À vérifier | Backend orphelin si Electron est tué brutalement |
| 12 | À vérifier | `Docker Desktop.exe` relancé alors que Docker tournait |
| 13 | Faible | Paramètres enregistrables pendant une action en cours |
| 14 | Bloquante | Création, suppression et restauration de base impossibles : `getaddrinfo failed` (trouvé en recette) |

## Constats

### 1. Anciens projets : liens d'addons créés par WSL, illisibles par Windows

`Caritel/odoo/addons` contient 1 416 entrées, qui sont toutes des liens WSL (point d'analyse `0xA000001D`, `IO_REPARSE_TAG_LX_SYMLINK`). Windows ne peut pas les ouvrir : `Le système ne peut pas accéder au fichier`. Ils sont aussi inaccessibles pour l'Explorateur, VS Code, Git pour Windows et le backend natif. Odoo fonctionne dans Docker, mais le gestionnaire dépend alors entièrement de WSL pour lire les modules.

Ils viennent de `ProjectCreator.link_modules` : `link_modules_batch_via_wsl` est essayé en premier dès que `wsl.exe` existe, et `link_module_via_wsl` sert de repli quand le lien natif échoue (WinError 1314 sans mode développeur).

Contre-exemple : `DEMO_01`, créé par la 0.5.0 avec le mode développeur actif, n'a que des liens NTFS (787 liens, `0xA000000C`, aucun lien WSL). Un lien NTFS relatif est correctement vu dans un conteneur Docker Desktop : `mod_a -> ../store/mod_a`, et le manifeste est lisible. Les liens natifs fonctionnent donc de bout en bout.

### 2. Liste des modules : scan via WSL de 32 à 73 s, contre 0,2 s en natif

`module_dirs` et `project_module_graph` passent par `wsl_module_dirs` dès que `wsl_shell_available()` répond, même quand le workspace est sur `C:\` et que les liens sont natifs.

| Mesure (app installée) | Durée |
|---|---:|
| `GET /api/projects/Caritel/modules`, premier appel | 32,3 s |
| même appel, cache chaud | 0,3 s |
| `GET /api/projects/DEMO_01/modules` après expiration du cache (×2) | 33,3 s / 34,1 s |
| Script de scan actuel rejoué dans WSL (1 `readlink` par lien) | 73,1 s |
| `find` seul dans WSL, sans `readlink` ni test de manifeste | 2,9 s |
| Parcours natif Python de `odoo/addons` et `addons-store/odoo_entreprise` | 0,2 s |

Le cache `MODULE_CACHE` ne dure que 45 s. Revenir sur l'onglet Modules après une minute relance donc le scan complet. `clear_project_module_cache` le vide aussi après les actions. L'interface accorde 90 s aux lectures de modules (`SLOW_READ_TIMEOUT_MS`), et le pire cas mesuré s'en approche. Deux lectures simultanées lancent deux scans, car rien ne mutualise un scan en cours.

Le coût vient des appels de fichiers à travers le pont Windows↔WSL (9P) : un `readlink -f` lancé comme processus pour chaque lien, et un test `[ -f manifest ]` pour chaque dossier. Une variante `find -L` est encore plus lente (141 s) ; optimiser le script WSL ne suffit donc pas.

### 3. Cache WSL : mode de suppression obsolète après changement d'un lien

`module_removal_info` et `module_location_info` lisent d'abord `WSL_MODULE_METADATA`, rempli par le dernier scan WSL. Quand `modules_for` réutilise la liste en cache, ces métadonnées ne sont pas recalculées. Après remplacement d'un lien géré par un lien externe, la liste annonce encore `link_and_storage` au lieu de `link_only`.

L'audit de performance du 8 septembre annonçait pourtant : « Les liens sont toujours contrôlés à chaque lecture ». Sous Windows avec WSL, cette garantie ne tient pas. Le test `test_cached_listing_rechecks_link_safety_without_rebuilding_layout_per_module` échoue sur ce poste.

À vérifier avant correction : l'opération de suppression refait-elle son propre contrôle sans passer par `WSL_MODULE_METADATA` ? Sinon, une suppression pourrait retirer la copie de `addons-store` alors que le lien pointe ailleurs.

### 4. La CI Windows n'exécute jamais les chemins WSL

Les runners `windows-latest` n'ont pas de distribution WSL : le code prend toujours la branche native. Sur un vrai poste avec Ubuntu, 342 tests donnent 8 échecs (4 tests distincts, dont un hérité par trois classes). Ces 4 tests ne sont pas isolés des vrais appels `wsl.exe` :

- `test_standard_project_is_created_atomically_with_relative_enterprise_links` et `test_gitlab_addons_are_cloned_to_store_and_linked` : les liens sont créés par WSL, donc `is_symlink()` renvoie `False` ;
- `test_cached_listing_rechecks_link_safety_without_rebuilding_layout_per_module` : voir le constat 3 ;
- `test_socle_job_logs_dependencies_and_rejects_missing_ones` : `project_module_graph` scanne le disque réel via WSL et ignore `module_dirs` simulé ; le test prend 5,6 s seul.

Sans le mode développeur, 82 tests échouent (WinError 1314 puis 1920). Rien ne signale ce prérequis aux développeurs.

### 5. Sondage Docker qui réveille WSL toutes les 10 s environ

`system.docker_status` interroge sous Windows tous les backends Docker : il lance `docker info` en natif et `wsl.exe --exec docker --version` dans la distribution par défaut, même quand Docker natif répond. `/api/overview` l'appelle, et l'interface rafraîchit l'overview toutes les 10 s (1,2 s pendant une action).

Observé : Ubuntu passe de `Stopped` à `Running` au lancement de l'app, alors que Docker Desktop n'utilise pas cette distribution. Sur 60 s d'utilisation, le backend a lancé 32 processus : 28 `docker.exe` et 3 `wsl.exe`. La VM WSL n'est jamais mise en veille, ce qui consomme mémoire (`vmmem`) et batterie. `/api/system/status` met 3,1 s au premier appel.

### 6. Création de projet via Git dans WSL sans options SSH non interactives

`ProjectService.env()` définit `GIT_SSH_COMMAND` (`BatchMode=yes`, `StrictHostKeyChecking=accept-new`) et `GIT_TERMINAL_PROMPT=0`. Or `wsl.exe` ne transmet pas les variables d'environnement Windows (aucun `WSLENV` dans le code). `ProjectCreator.git()` ne passe pas `-c core.sshCommand=…`, contrairement à l'import de dépôt (`REPOSITORY_GIT_OPTIONS`).

Conséquence probable quand Git n'existe que dans WSL : au premier clone vers `gitlab.sudokeys.com:10022`, la clé d'hôte est inconnue et ssh ne peut pas poser la question sans terminal (`Host key verification failed`). À confirmer par un essai sur une distribution sans `known_hosts`.

### 7. Sonde WSL relancée à chaque construction de commande Git

`ProjectService.git()` et `preferred_git_runtime()` appellent `find_wsl_executable_distribution("git")` à chaque fois. Cela lance un `wsl.exe sh -lc` (délai de 6 s), puis `wsl --list` et une sonde par distribution en cas d'échec, même quand Git pour Windows est présent et sera utilisé. Aucun cache. Le délai de 6 s est aussi serré pour un démarrage à froid de la VM WSL.

### 8. Le test de l'installateur casse l'installation d'un poste de développement

`scripts/build_desktop.py` lance toujours `smoke_test_windows_installer.py` sous Windows. Ce script :

- force l'arrêt de `SDK Local Manager.exe` et `odoo-manager-backend.exe` (`taskkill /F /IM`), y compris l'app ouverte par l'utilisateur ;
- installe le même `appId` dans un dossier temporaire, ce qui remplace l'entrée de désinstallation et les raccourcis de l'installation réelle, puis supprime ce dossier ;
- utilise le port fixe 18765 et le dossier de données Electron réel (`%APPDATA%\SDK Local Manager`), car `userData` n'est isolé que pour le smoke test Electron.

Sans risque sur un runner CI jetable, mais dangereux sur un poste de développement.

### 9. Installateur et backend non signés

`SDK-Local-Manager-0.5.0-win-x64.exe` : `NotSigned`. Un téléchargement par navigateur déclenche SmartScreen (« Windows a protégé votre ordinateur »). Un exécutable PyInstaller non signé est aussi une source fréquente de faux positifs de Defender ou des antivirus d'entreprise.

### 10. `odoo_manager.sh` embarqué en CRLF ; code mort, mais requis au démarrage

Le script installé (`resources/backend/odoo-manager-backend-runtime/odoo_manager.sh`) a 1 310 fins de ligne CRLF sur 1 310. `actions/checkout` sur Windows applique `core.autocrlf=true`, et le dépôt n'a pas de `.gitattributes`. `sh` échouerait à la première ligne.

Le défaut est latent : `manager_job` et `manager_command` ne sont plus appelés, et leur branche WSL est inatteignable puisque `execution_mode` est forcé à `native` sous Windows. `main()` refuse pourtant de démarrer si le script est absent, et le sidecar l'embarque encore.

### 11. À vérifier : backend orphelin si Electron est tué brutalement

Rien ne relie la durée de vie du backend à celle d'Electron : ni surveillance du PID parent, ni Job Object. `taskkill` n'est utilisé que dans `Backend.terminate()`, lors d'une fermeture normale. Si Electron plante ou est terminé depuis le Gestionnaire des tâches, le backend continue de tourner. L'app suivante choisit alors un autre port, ce cas est géré, mais les fichiers restent verrouillés. Non testé dynamiquement pour ne pas interrompre la recette. Il faut vérifier le comportement de la mise à niveau NSIS dans ce cas.

### 12. À vérifier : `Docker Desktop.exe` relancé alors que Docker tournait

Pendant l'échantillonnage, le backend a lancé `Docker Desktop.exe` (22:21:07) alors que les conteneurs `DEMO_01` et Traefik tournaient. Cause à identifier : action de l'utilisateur, ou démarrage automatique après un état Docker transitoirement indisponible.

### 13. Paramètres enregistrables pendant une action en cours

`errors.jsonl` montre trois `POST /api/settings` refusés (HTTP 400) en deux secondes pendant « Démarrer Caritel ». Le backend protège correctement, mais l'interface laisse cliquer sans indiquer pourquoi c'est bloqué.

### 14. Création, suppression et restauration de base impossibles

Trouvé en recette : « Créer base demo_test » sur `DEMO_01` échoue avec `urlopen error [Errno 11001] getaddrinfo failed`. `post_form_no_redirect` (création et suppression) et `post_odoo_database_restore` résolvaient le nom `dev.<projet>.localhost` fourni par la règle Traefik. Or le résolveur Windows ne résout que `localhost`, pas ses sous-domaines. Les navigateurs Chrome et Edge, macOS et Linux les résolvent : le défaut n'existait que sous Windows.

Le démarrage du projet fonctionnait, car sa sonde se connecte déjà à `127.0.0.1` avec l'en-tête `Host`. Traefik route correctement cette requête (HTTP 303 vérifié).

## Points vérifiés sans anomalie

- Le backend est déclaré `longPathAware`, et `LongPathsEnabled=1`.
- Démarrage de l'app installée : API prête et identité d'instance vérifiée en 1,5 s.
- Serveur API multithread (`ThreadingHTTPServer`, 64 connexions en file) : un scan long ne bloque pas les autres routes.
- Electron : `contextIsolation`, `sandbox`, CSP à empreintes, IPC filtré par origine et par cadre, `externalUrl` limité à HTTP(S), identifiant d'app (AUMID) cohérent avec l'installateur pour les notifications.
- Mise à niveau NSIS silencieuse de la 0.3.0 vers la 0.5.0 en 13,6 s. Configuration, projets, raccourcis du menu Démarrer et du Bureau conservés.
- Traefik publié sur `127.0.0.1:80` par l'override généré.
- Sortie de `wsl.exe --list` en UTF-16 correctement décodée.

## Ordre de correction proposé

1. **Liens et scan natifs (constats 1, 2, 3).** Sous Windows, créer des liens NTFS relatifs quand le privilège existe, et n'utiliser les liens WSL que pour un workspace `\\wsl.localhost\…`. Détecter le mode développeur dans l'assistant de premier lancement. Lister les modules en natif, et ne passer par WSL que pour un workspace WSL ou un projet contenant encore des liens WSL. Proposer une conversion des liens WSL existants (Caritel) en liens NTFS. Ne jamais lire `removal_mode` depuis un cache.
2. **Sondages (constats 5, 7).** Ne sonder Docker dans WSL que si Docker natif est absent ou en échec, et mettre en cache l'environnement Git choisi, comme pour Docker.
3. **SSH via WSL (constat 6).** Passer `core.sshCommand` en argument `-c` dans `ProjectCreator.git()`, comme `REPOSITORY_GIT_OPTIONS`.
4. **Fiabilité CI et tests (constats 4, 8, 10).** Ajouter un `.gitattributes` (`*.sh text eol=lf`). Isoler les 4 tests des vrais appels `wsl.exe`. Limiter le test de l'installateur à la CI, ou lui donner un `appId` et un port distincts. Retirer `manager_job`, `manager_command` et la dépendance de démarrage à `odoo_manager.sh` si le script n'a plus d'usage.
5. **Distribution (constat 9).** Signer l'installateur et le backend avec un certificat de signature de code, injecté par les secrets GitHub Actions.

## Limites

Non couvert dans cette passe : restauration d'une sauvegarde volumineuse, neutralisation, import ZIP, création de projet RIKA, workspace placé dans `\\wsl.localhost\…`, poste sans Git pour Windows, poste sans mode développeur avec la 0.5.0. Les performances Odoo/PostgreSQL sur un bind mount NTFS n'ont pas été mesurées.
