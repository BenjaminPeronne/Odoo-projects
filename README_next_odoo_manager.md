# Interface Next.js du gestionnaire Odoo

Cette interface est l'UI active du gestionnaire Odoo local.

- Application de bureau : installer le DMG puis ouvrir `Odoo Manager.app`
- Interface Next.js en developpement : `./odoo_next_gui.sh`, puis http://127.0.0.1:3000/

La nouvelle interface consomme l'API Python existante servie par `odoo_manager_web.py`.
Le backend Python reste le sidecar API local. La vue Bootstrap historique est
archivee dans `archive/bootstrap/odoo_manager_bootstrap_legacy.html` et n'est
plus exposee comme interface de secours.

L'interface utilise la configuration persistante du backend. Le dossier de
projets peut etre change depuis `Parametres` sans deplacer le gestionnaire.

## Commandes

```sh
./odoo_next_gui.sh
```

Pour lancer en arriere-plan via tmux si disponible :

```sh
./odoo_next_gui.sh --background
```

Pour arreter la nouvelle interface :

```sh
./odoo_next_gui.sh --stop
```

Pour lancer Next directement :

```sh
cd odoo-manager-next
ODOO_MANAGER_API=http://127.0.0.1:8765 npm run dev -- --hostname 127.0.0.1 --port 3000
```

## Etat actuel

Premiere tranche disponible :

- sidebar projets avec recherche et statuts ;
- onglets Bases, Modules, Logs, Actions ;
- recherche/filtres modules ;
- selection multiple de modules ;
- import ZIP avec copie dans `PROJET/odoo/odoo/addons/` et lien relatif dans `PROJET/odoo/addons/` ;
- creation de base ;
- historique des jobs ;
- actions principales projet ;
- notification Docker et tentative de demarrage de Docker Desktop ;
- parametres workspace, Docker, Brainkeys, Traefik, terminal et WSL 2.

## Application de bureau Tauri

Le dossier `odoo-manager-next/src-tauri` contient le socle Tauri 2. Le frontend
Next peut etre exporte statiquement avec :

```sh
cd odoo-manager-next
npm run build:desktop
```

L'icone source se trouve dans `odoo-manager-next/assets/app-icon.png`. Les
formats `.icns`, `.ico` et PNG utilises par les installateurs sont generes dans
`odoo-manager-next/src-tauri/icons/`.

Pour construire l'application sur le systeme courant :

```sh
sh scripts/build_local_desktop.sh
```

Sur macOS, pour generer uniquement le bundle `.app` local :

```sh
sh scripts/build_local_desktop.sh --bundles app
```

Le script choisit les paquets natifs suivants :

| Systeme de construction | Paquets |
| --- | --- |
| macOS | `.app` et `.dmg` |
| Linux | `.deb` et `.AppImage` |
| Windows | installateur NSIS `.exe` |

Les sorties sont placees dans
`odoo-manager-next/src-tauri/target/release/bundle/`. Sur un build macOS local,
le script peut utiliser le dossier temporaire systeme et affiche alors le chemin
exact en fin de commande. Rust, Node.js, npm et les prerequis Tauri de la
plateforme doivent etre installes.

## Compilation multiplateforme

Le workflow `.github/workflows/build-desktop.yml` compile nativement les trois
plateformes. Le runner macOS est volontairement épinglé sur `macos-15` pour
eviter les migrations automatiques de `macos-latest`. Il peut etre lance
manuellement dans GitHub Actions ou par un tag `app-v*`, par exemple
`app-v0.1.1`.

Un script lance toute la procedure depuis le poste local :

```sh
sh scripts/build_all_platforms.sh
```

Il verifie le backend, les tests et le build Next.js, cree un tag `app-v*`, le
pousse sur GitHub et laisse GitHub Actions compiler macOS, Linux et Windows. Si
GitHub CLI est installe et authentifie (`gh auth login`), le script attend la
fin du workflow puis telecharge les artefacts dans `dist/all-platforms/<tag>/`.

Exemples utiles :

```sh
sh scripts/build_all_platforms.sh --tag app-v0.1.1
sh scripts/build_all_platforms.sh --local
sh scripts/build_all_platforms.sh --no-wait --no-download
```

Pour une version stable, utilise un tag sans suffixe, par exemple
`app-v0.1.1`. Les suffixes comme `app-v0.1.1-build2` restent possibles pour des
builds intermediaires, mais ils ne doivent pas etre utilises comme version de
diffusion stable.

Chaque runner reconstruit le sidecar Python de sa plateforme avant de produire
l'installateur. Cette etape est necessaire : un Mac ne produit pas de maniere
fiable un installateur Windows ou Linux complet.

Les paquets macOS privés sont signés ad hoc afin que le bundle `.app` soit
coherent localement, mais ils ne sont pas notarizes par Apple. Apres un
telechargement depuis GitHub ou un navigateur, macOS peut encore afficher que
l'application est endommagee ou bloquee par securite. Pour un build prive,
glisse l'app dans Applications puis lance :

```sh
sh scripts/macos_allow_private_build.sh
```

Ce script retire la quarantaine macOS et verifie la signature locale du bundle.

Une distribution publique sans alerte macOS necessitera un certificat Apple
Developer ID, la signature Developer ID du sidecar et de l'app, puis la
notarisation Apple du DMG. Les certificats ne doivent pas etre commités dans le
depot ; ils devront etre injectes via les secrets GitHub Actions.
