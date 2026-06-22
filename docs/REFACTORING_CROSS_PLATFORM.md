# Refonte multiplateforme du gestionnaire Odoo

## Architecture retenue

1. `odoo_manager_core/` porte la configuration et les adaptations système.
2. `odoo_manager_web.py` reste temporairement l'API locale et le sidecar Python.
3. `odoo-manager-next/` est l'interface principale React.
4. L'interface Bootstrap embarquée reste disponible sur le port `8765` pendant la migration.
5. `odoo-manager-next/src-tauri/` prépare l'application de bureau Tauri 2.
6. `scripts/build_desktop.py` fournit une commande de construction native commune.

Les interfaces ne doivent contenir aucune commande Docker ou Brainkeys. Elles
appellent uniquement l'API locale.

## Compatibilité visée

| Système | Mode d'exécution | Docker | Création Brainkeys |
| --- | --- | --- | --- |
| macOS | natif | Docker Desktop | Terminal ou iTerm |
| Linux | natif | Docker Engine/Desktop | terminal graphique détecté |
| Windows 10/11 | WSL 2 | Docker Desktop avec intégration WSL | Windows Terminal ou CMD vers WSL |

MS-DOS n'est pas une cible technique : Odoo, Docker, WSL et Tauri nécessitent
un système moderne. La cible Windows est Windows 10/11.

## Configuration

Le fichier est stocké dans le dossier applicatif du système :

- macOS : `~/Library/Application Support/Odoo Manager/config.json`
- Linux : `$XDG_CONFIG_HOME/odoo-manager/config.json`
- Windows : `%APPDATA%/Odoo Manager/config.json`

Variables de surcharge utiles :

- `ODOO_MANAGER_CONFIG`
- `ODOO_MANAGER_CONFIG_DIR`
- `ODOO_WORKSPACE`
- `ODOO_MANAGER_SCRIPT`

## Nettoyage réalisé

- suppression de l'assistant Brainkeys intégré, de sa modale et de son pseudo-terminal ;
- centralisation du statut et du démarrage Docker ;
- centralisation des emplacements de configuration par système ;
- ajout d'un lanceur de terminal macOS/Linux/Windows-WSL ;
- conservation d'une seule API pour React et Bootstrap.

## Éléments conservés temporairement

- `odoo_manager.sh` : encore requis par plusieurs actions métier ;
- `odoo_manager_web.py` : API et interface Bootstrap de secours ;
- `odoo_gui.sh` et `odoo_next_gui.sh` : lanceurs de développement et de secours.

L'ancien `Odoo Manager.app`, qui ouvrait simplement l'interface React dans le
navigateur, et son script `build_odoo_gui_app.sh` ont ete retires apres
validation du paquet Tauri autonome.

## Distribution native

La construction se lance avec `python3 scripts/build_desktop.py`. Le script
produit `.app`/`.dmg` sur macOS, `.deb`/`.AppImage` sur Linux et un installateur
NSIS `.exe` sur Windows.

Le workflow `.github/workflows/build-desktop.yml` execute ces constructions sur
des runners natifs macOS, Ubuntu et Windows. Les binaires de bureau ne doivent
pas etre reutilises entre plateformes : le sidecar Python est reconstruit pour
le triple Rust de chaque runner.

L'icone source est `odoo-manager-next/assets/app-icon.png`; ses variantes
plateformes sont versionnees dans `odoo-manager-next/src-tauri/icons/`.

## Prochaine extraction

Les appels restants à `odoo_manager.sh` doivent être déplacés par domaine dans
le backend Python : cycle Docker Compose, commandes Odoo, logs, modules et mise
à jour des dépôts. Une fois cette parité couverte par des tests, le script shell
pourra devenir un simple client CLI de l'API au lieu de porter la logique métier.
