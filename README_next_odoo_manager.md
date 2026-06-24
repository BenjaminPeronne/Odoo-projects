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
python3 -m pip install pyinstaller
python3 scripts/build_desktop.py
```

Le script choisit les paquets natifs suivants :

| Systeme de construction | Paquets |
| --- | --- |
| macOS | `.app` et `.dmg` |
| Linux | `.deb` et `.AppImage` |
| Windows | installateur NSIS `.exe` |

Les sorties sont placees dans
`odoo-manager-next/src-tauri/target/release/bundle/`. Rust, Node.js, npm et les
prerequis Tauri de la plateforme doivent etre installes.

## Compilation multiplateforme

Le workflow `.github/workflows/build-desktop.yml` compile nativement les trois
plateformes. Il peut etre lance manuellement dans GitHub Actions ou par un tag
`app-v*`, par exemple `app-v0.1.0`.

Chaque runner reconstruit le sidecar Python de sa plateforme avant de produire
l'installateur. Cette etape est necessaire : un Mac ne produit pas de maniere
fiable un installateur Windows ou Linux complet.

Les paquets locaux et CI sont non signes par defaut. Une signature ad hoc ne
doit pas etre forcee : elle empeche le sidecar PyInstaller de charger sa
bibliotheque Python sur macOS. Une distribution publique necessitera les
certificats de signature Windows et Apple, la signature coherente du sidecar,
ainsi que la notarisation Apple.
