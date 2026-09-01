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

- premier lancement guide avec verification du workspace, de Docker, de Git, de la cle SSH publique et de Traefik ;
- installation silencieuse de Git avec `winget` sous Windows, generation Ed25519 et copie de la cle publique vers GitLab ;
- installation native de Traefik par clone Git atomique puis demarrage Docker Compose, sans terminal externe ;
- creation native d'un projet Odoo standard ou d'un socle standard complete par un depot d'addons GitLab ;
- sidebar projets avec recherche et statuts ;
- onglets Bases, Modules, Logs, Actions ;
- recherche/filtres modules ;
- selection multiple de modules ;
- import ZIP avec copie dans `PROJET/odoo/addons-store/` et lien relatif dans `PROJET/odoo/addons/` ;
- mise a jour sans filestore complet et annulation locale securisee des operations de modules dont le code est absent ;
- creation de base vide ou restauration directe d'une sauvegarde ZIP Odoo ;
- historique des jobs ;
- actions principales projet ;
- notification Docker et tentative de demarrage de Docker Desktop ;
- parametres workspace, Docker et Traefik ; la coordination Windows/WSL 2 est automatique.

### Creation d'un projet

Le bouton `Nouveau projet` ouvre un formulaire integre. Le backend Python
recupere le modele Docker, Odoo Community et Odoo Enterprise depuis GitLab,
configure le projet puis cree les liens relatifs des modules dans
`PROJET/odoo/addons/`. Un depot d'addons client peut etre ajoute pendant la
creation ; il est conserve dans `PROJET/odoo/addons-store/`.

La creation utilise Git en arguments structures, sans terminal interactif et
sans demander les identifiants GitLab. Sous Windows, l'assistant peut installer
Git avec Windows Package Manager, generer une cle Ed25519 locale, afficher
uniquement sa partie publique et ouvrir la page des cles SSH GitLab. L'utilisateur
doit ensuite enregistrer cette cle publique dans GitLab. Le projet est prepare dans
un dossier temporaire du workspace puis deplace a son emplacement final en une
operation, afin qu'un clone interrompu ne laisse pas de projet partiel.

Le parcours `brainkeys riplika` reste disponible uniquement dans le client CLI
historique pour les environnements Rika non couverts par l'interface graphique.

### Restaurer une sauvegarde Odoo

Dans l'onglet `Bases`, le bouton `Restaurer une sauvegarde ZIP` remplace le
passage manuel par `/web/database/selector`. Le gestionnaire demande le ZIP et
le nom de la nouvelle base, utilise `odoo` comme master password par defaut,
demarre le projet puis transmet la sauvegarde au controleur officiel Odoo
`/web/database/restore`.

Le televersement est ecrit progressivement sur disque et transmis a Odoo en
flux afin de ne pas charger une sauvegarde volumineuse en memoire. La base est
declaree comme une copie et la neutralisation est activee par defaut pour les
tests locaux. Le fichier temporaire est supprime a la fin du job, y compris en
cas d'erreur.

### Modules absents sur une copie locale

Avant une mise a jour complete, le gestionnaire detecte tous les modules en
etat `to install`, `to upgrade` ou `to remove`, que leur code soit disponible
ou absent. Il est possible de selectionner les modules inutiles pour la recette
locale et d'annuler uniquement leur operation en attente. Cette action ne
desinstalle pas le module et ne supprime aucune donnee. Les exclusions peuvent
etre reactivees depuis la meme fenetre.

Le backend refuse l'exception lorsqu'un module actif dont le code est present
depend du module selectionne. Les exceptions acceptees sont conservees dans la
configuration locale du gestionnaire et apparaissent ensuite comme des
avertissements dans le diagnostic. Tant qu'une exception locale existe, la MAJ
complete utilise une liste explicite des modules installes dont le code est
disponible au lieu de `-u all`, afin de ne pas remettre les modules absents en
etat `to upgrade`.

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

Il verifie le backend, les tests et le build Next.js, calcule automatiquement
le prochain numero `app-v<version>-buildN`, pousse ce tag sur GitHub et laisse
GitHub Actions compiler macOS, Linux et Windows. Par exemple, apres
`app-v0.1.1-build19`, la commande suivante produit `app-v0.1.1-build20`. Si
GitHub CLI est installe et authentifie (`gh auth login`), le script attend la
fin du workflow puis telecharge les artefacts dans `dist/all-platforms/<tag>/`.

Exemples utiles :

```sh
sh scripts/build_all_platforms.sh
sh scripts/build_all_platforms.sh --tag app-v0.1.2
sh scripts/build_all_platforms.sh --local
sh scripts/build_all_platforms.sh --no-wait --no-download
```

`--tag` reste disponible pour publier explicitement une nouvelle version
fonctionnelle. Le numero automatique distingue les compilations successives
sans modifier la version de l'application dans `package.json`, `Cargo.toml` et
`tauri.conf.json`.

Pour publier une vraie evolution fonctionnelle, synchronise d'abord sa version :

```sh
python3 scripts/set_app_version.py 0.1.2
git add -A
git commit -m "Release Odoo Manager 0.1.2"
git push origin main
sh scripts/build_all_platforms.sh
```

La derniere commande repart automatiquement sur `app-v0.1.2-build1`.

Chaque runner reconstruit le sidecar Python de sa plateforme puis le demarre sur
un port local temporaire et controle `/api/health` avant de produire
l'installateur. Sur Windows, le pipeline installe ensuite silencieusement le
paquet NSIS, lance l'application installee et controle une seconde fois cette
API. Un backend Windows qui quitte au demarrage fait donc echouer le build au
lieu de produire un installateur inutilisable. Le runtime Windows est livre en
repertoire pour eviter toute extraction d'executable Python dans `%TEMP%`.
Le test relance aussi l'installateur pendant que l'application est ouverte afin
de verifier que la mise a niveau ferme l'ancien backend avant de remplacer les
fichiers verrouilles par Windows.
Cette etape est necessaire : un Mac ne produit pas de maniere fiable un
installateur Windows ou Linux complet.

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
