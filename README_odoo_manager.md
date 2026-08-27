# Gestionnaire Odoo local

Ce script centralise les actions courantes sur les instances Odoo locales du dossier `Odoo-projects`.

## Lancer l'application

Sur macOS, double-cliquez sur :

```text
Odoo Manager.app
```

Le lanceur démarre le backend Python, l'interface React et ouvre :

```text
http://127.0.0.1:3000/
```

Depuis un terminal, la commande équivalente est :

```bash
./odoo_next_gui.sh --background
```

Pour arrêter l'interface React :

```bash
./odoo_next_gui.sh --stop
```

L'ancien lanceur Bootstrap est conserve comme alias de compatibilite :

```bash
./odoo_gui.sh
```

Il lance maintenant l'interface Next active. La vue Bootstrap historique n'est
plus exposee par le backend ; son HTML est archive dans
`archive/bootstrap/odoo_manager_bootstrap_legacy.html`.

La creation d'un nouveau projet se fait directement dans l'application. Le
formulaire permet de choisir la version Odoo et un environnement standard ou
un depot d'addons client GitLab. Aucun terminal et aucun mot de passe GitLab ne
sont demandes. Sous Windows, le premier lancement peut installer Git avec
`winget`, generer une cle SSH Ed25519, copier sa partie publique et ouvrir la
page GitLab. La cle privee ne quitte jamais la machine.

Une fois Docker, Git et la cle SSH disponibles, le bouton `Installer Traefik`
clone ou met a jour `docker-local-tools`, valide le fichier Compose puis demarre
Traefik directement depuis le backend Python. Aucun terminal externe n'est
necessaire en mode natif.

Le bandeau Docker indique si le moteur est arrete ou absent. Sur macOS et
Windows, le bouton `Ouvrir Docker` tente de lancer Docker Desktop. Le bouton
`Parametres` permet de definir :

- le workspace analyse pour lister et creer les projets ;
- le mode natif ou WSL 2 ;
- la commande Docker ;
- le dossier Traefik ;
- la frequence de verification de Docker.

Pour ajouter des modules, selectionnez un projet puis utilisez `Ajouter des
modules locaux` :

- `Copier et lier` copie les modules detectes dans
  `PROJET/odoo/addons-store/`, puis cree un lien symbolique relatif dans
  `PROJET/odoo/addons/`.
- `Importer ZIP` extrait l'archive temporairement, detecte les dossiers
  contenant `__manifest__.py`, copie les modules dans `PROJET/odoo/addons-store/`,
  puis cree les liens relatifs dans `PROJET/odoo/addons/`.

Avant une installation ou une mise a jour de module, le gestionnaire normalise
aussi les anciens liens geres : un lien absolu ou un ancien import est recopie
dans `PROJET/odoo/addons-store/`, puis remplace par un lien relatif depuis
`PROJET/odoo/addons/`.

Pour creer une base, selectionnez un projet puis cliquez sur `Creer base`.
Le gestionnaire demarre le projet si necessaire, appelle Odoo, puis recharge la
liste des bases. Le master password local habituel est `odoo`.

Pour mettre a jour tous les modules d'une base depuis l'interface, selectionnez
le projet et la base Odoo, puis cliquez sur `Mettre a jour tous les modules`.
Cela lance l'equivalent de :

```bash
odoo -d NOM_DE_BASE -u all --stop-after-init
```

Pour supprimer un projet, selectionnez-le puis cliquez sur `Supprimer`.
Le gestionnaire arrete `docker compose down`, puis deplace le dossier dans
`.odoo_manager_deleted/` au lieu de le supprimer definitivement.

## Lancer le menu

Depuis le dossier `Odoo-projects` :

```bash
./odoo_manager.sh
```

Si votre terminal refuse l'execution directe, utilisez :

```bash
sh odoo_manager.sh
```

Le menu permet de :

- voir toutes les bases / projets locaux ;
- demarrer et ouvrir un projet Odoo ;
- arreter les conteneurs Docker Compose d'un projet ;
- lister les bases PostgreSQL d'un projet demarre ;
- ouvrir l'ecran Odoo de creation de base ;
- installer ou mettre a jour un module Odoo sur une base ;
- mettre a jour tous les modules Odoo d'une base ;
- mettre a jour le code / les images d'un projet ;
- mettre a jour le code / les images de tous les projets ;
- creer un nouveau projet via `brainkeys riplika` (CLI historique uniquement) ;
- afficher les logs Odoo ;
- ouvrir un shell dans le conteneur Odoo.

La commande CLI historique `--create-project` utilise encore Brainkeys pour
les parcours Rika non integres. Si Brainkeys demande :

```text
Souhaitez-vous executer les conteneurs du projet ?
```

Repondez `Non`. Le gestionnaire detectera ensuite le nouveau projet, lancera lui-meme `docker compose up`, attendra le conteneur Odoo, affichera l'URL et ouvrira l'ecran de creation de base.

## Commandes directes

```bash
./odoo_manager.sh --list
./odoo_manager.sh --start PROJET
./odoo_manager.sh --stop PROJET
./odoo_manager.sh --dbs PROJET
./odoo_manager.sh --create-db PROJET
./odoo_manager.sh --update-module PROJET BASE MODULE
./odoo_manager.sh --install-module PROJET BASE MODULE
./odoo_manager.sh --update-all-modules PROJET BASE
./odoo_manager.sh --update PROJET
./odoo_manager.sh --update-all
./odoo_manager.sh --create-project
./odoo_manager.sh --logs PROJET
./odoo_manager.sh --shell PROJET
```

## Notes

- Le script detecte les projets qui contiennent un fichier `docker-compose.yml`, `docker-compose.yaml`, `compose.yml` ou `compose.yaml`.
- Si Docker n'est pas lance, `--list` affiche quand meme les projets, avec le statut `docker off`.
- Dans l'interface graphique, la creation de base se fait via le formulaire integre. En terminal, `--create-db` ouvre encore `/web/database/manager`.
- Pour installer un nouveau module, utilisez l'option `5` du menu puis choisissez `Installer`, ou lancez `--install-module`.
- Pour mettre a jour un module deja installe apres modification de code, utilisez l'option `5` puis choisissez `Mettre a jour`, ou lancez `--update-module`.
- Pour mettre a jour tous les modules d'une base, utilisez l'option `6` du menu ou lancez `--update-all-modules PROJET BASE`.
- Le master password documente pour les bases locales est `odoo`.
- Par defaut, le workspace est le dossier ou se trouve le script. Il peut etre surcharge avec `ODOO_WORKSPACE=/chemin/vers/Odoo-projects`.
- Sous Windows 10/11, l'application graphique fonctionne en mode natif avec Docker Desktop. WSL 2 reste disponible pour les anciens parcours shell ou Brainkeys. MS-DOS n'est pas un environnement d'execution compatible avec Docker et Odoo.
