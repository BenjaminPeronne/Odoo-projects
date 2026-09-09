# Interface Next.js du gestionnaire Odoo

Cette interface est l'UI active du gestionnaire Odoo local.

- Application de bureau : installer le DMG puis ouvrir `SDK Local Manager.app`
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

Cette commande est le lanceur de previsualisation du design. Elle demarre
l'API Python sur `127.0.0.1:18765`, lance Next.js, attend que les deux services
soient disponibles puis ouvre automatiquement `http://127.0.0.1:3000/` dans
le navigateur. `Ctrl+C` arrete les processus demarres par cette commande.

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
ODOO_MANAGER_API=http://127.0.0.1:18765 npm run dev -- --hostname 127.0.0.1 --port 3000
```

Le port local de l'API peut etre modifie dans `Parametres`. L'application de
bureau verifie sa disponibilite au demarrage et choisit automatiquement un
port libre si le port prefere est deja utilise, notamment par Docker.

Ports utilises ou contactes par le gestionnaire :

- `18765` par defaut : API locale du gestionnaire, liee uniquement a `127.0.0.1` ;
- `80` et `443` : acces aux projets via Traefik ;
- `8069` : port interne des conteneurs Odoo ;
- `5432` : port interne des conteneurs PostgreSQL ;
- `10022` : connexion SSH sortante vers GitLab Sudokeys ;
- `3000` : interface Next.js en mode developpement uniquement.

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
- vue `A propos` avec la version de l'application et les informations du createur ;
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

Le formulaire propose aussi **Copie depuis RIKA**. Il demande le nom de
l'instance et les identifiants Sudokeys, génère et télécharge la copie, contrôle
le ZIP puis détecte automatiquement la version Odoo avant de préparer le modèle
Docker correspondant. Les identifiants restent limités au job de création et ne
sont pas enregistrés dans les paramètres du gestionnaire.

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

Quand la neutralisation est demandee, le gestionnaire effectue ensuite une
seconde passe avec le moteur de neutralisation des modules installes. Il arrete
brievement le serveur Odoo et restaure avec les workers cron coupes pour eviter
la fenetre d'execution qui existe sur certaines revisions Odoo, puis
controle que la base est marquee comme neutralisee, que tous les crons metier
sont inactifs et qu'aucun serveur de messagerie entrant ou sortant exploitable
ne reste actif. Cette passe est aussi disponible sur une base existante avec le
bouton `Neutraliser et contrôler`. Odoo 16 a 19 utilisent le moteur natif ; Odoo
15 applique un repli limite aux crons et aux serveurs de messagerie.

Une base deja marquee comme neutralisee est automatiquement neutralisee de
nouveau, avant le redemarrage du serveur, apres chaque installation ou mise a
jour de module. Cela evite qu'un addon nouvellement charge reactive un cron ou
une integration externe.

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

Les artefacts GitHub Actions contiennent uniquement les installateurs `.dmg`,
`.deb`, `.AppImage` et `.exe`, conservés pendant 7 jours. Télécharge les paquets
à archiver avant leur expiration. Les applications décompressées et les dossiers
intermédiaires ne sont pas déposés. Cette règle s'applique aux prochains dépôts ;
elle ne libère pas le stockage occupé par les anciens artefacts.

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
git commit -m "Release SDK Local Manager 0.1.2"
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

## Identité Sudokeys Glow

Le produit se nomme **SDK Local Manager**. L’interface utilise la charte Glow : noir, orange `#F4791F`, surfaces chaudes et halos discrets. Le thème sombre est proposé par défaut ; les préférences existantes sont conservées. Manrope et JetBrains Mono sont embarquées dans `odoo-manager-next/public/fonts`, avec leurs licences OFL, pour fonctionner hors ligne.

Le logo final fourni est conservé dans `app/icon.png` et décliné par Tauri pour les icônes natives. Les identifiants techniques, clés de préférences et noms du sidecar restent stables pour préserver la compatibilité. Les nouveaux installateurs porteront le nom SDK Local Manager lors de leur prochaine compilation.


### Addons depuis un dépôt HTTPS

Dans **Modules → Dépôt HTTPS · Ajout / MAJ**, renseigner l’URL Git HTTPS et une branche ou un tag compatible avec la version Odoo.

- **Ajouter** : noms techniques séparés par des virgules, ou champ vide pour tous les modules. Un doublon bloque toute l’opération avant modification.
- **Mettre à jour le code existant** : noms obligatoires ; remplace uniquement les copies gérées dans `odoo/addons-store` avec leur lien relatif dans `odoo/addons`. Les autres dépôts et dossiers sont protégés.
- Le dépôt est récupéré dans un dossier temporaire (délai maximal : cinq minutes). Les liens symboliques et noms de modules ambigus sont refusés. Les dépôts privés utilisent les accès Git HTTPS déjà configurés, sans jeton dans l’URL ni demande interactive.
- Les versions remplacées sont conservées dans `.odoo_manager_backups/modules/<projet>`. Si la copie échoue, les changements de cet import sont annulés. Une autre action sur le même projet bloque le lancement de l’import.
- Cette opération prépare le **code uniquement** : lancer ensuite l’installation ou la mise à jour des modules dans la base depuis l’interface. La source externe n’est jamais modifiée. Pour actualiser à nouveau, réutiliser l’URL, la branche et les noms souhaités.
