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

L'interface Bootstrap reste disponible comme solution de secours :

```bash
./odoo_gui.sh
```

L'interface Bootstrap permet de voir les projets et bases, demarrer un projet,
ouvrir Odoo, creer une base depuis un formulaire integre, creer un nouveau
projet, supprimer un projet de maniere recuperable, lier un dossier de modules
local au projet, importer un ZIP d'addons, installer ou mettre a jour un module,
mettre a jour tous les modules d'une base, lancer les mises a jour de code /
images, consulter les logs et configurer le dossier de projets.

La creation d'un nouveau projet ouvre le terminal local de la machine puis
lance `brainkeys riplika`. Ce choix conserve l'interaction officielle de
Brainkeys sans reproduire son terminal dans l'interface. Quand la creation est
terminee, revenez dans le gestionnaire et cliquez sur `Actualiser`.

Le bandeau Docker indique si le moteur est arrete ou absent. Sur macOS et
Windows, le bouton `Ouvrir Docker` tente de lancer Docker Desktop. Le bouton
`Parametres` permet de definir :

- le workspace analyse pour lister et creer les projets ;
- le mode natif ou WSL 2 ;
- les commandes Docker et Brainkeys ;
- le dossier Traefik et le terminal ;
- la frequence de verification de Docker.

Pour ajouter des modules, selectionnez un projet puis utilisez `Ajouter des
modules locaux` :

- `Lier` relie un dossier local contenant un ou plusieurs modules Odoo.
- `Importer ZIP` extrait une archive ZIP dans `.odoo_manager_imports/PROJET/`,
  detecte les dossiers contenant `__manifest__.py`, puis les relie au projet.

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

## Application macOS

L'application locale temporaire est disponible dans le dossier :

```text
Odoo Manager.app
```

Double-cliquez dessus pour démarrer l'interface React et ouvrir automatiquement
la page dans le navigateur. L'application utilise `odoo_next_gui.sh`, donc
gardez-la dans `Odoo-projects` jusqu'au remplacement par le paquet Tauri.

Cette application historique reste fonctionnelle pendant la migration vers
Tauri. Elle ne constitue plus la cible de distribution multiplateforme.

Pour regenerer l'application apres modification du lanceur :

```bash
./build_odoo_gui_app.sh
```

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
- lister les bases PostgreSQL d'un projet demarre ;
- ouvrir l'ecran Odoo de creation de base ;
- installer ou mettre a jour un module Odoo sur une base ;
- mettre a jour tous les modules Odoo d'une base ;
- mettre a jour le code / les images d'un projet ;
- mettre a jour le code / les images de tous les projets ;
- creer un nouveau projet via `brainkeys riplika` ;
- afficher les logs Odoo ;
- ouvrir un shell dans le conteneur Odoo.

Lors de la creation d'un nouveau projet, si Brainkeys demande :

```text
Souhaitez-vous executer les conteneurs du projet ?
```

Repondez `Non`. Le gestionnaire detectera ensuite le nouveau projet, lancera lui-meme `docker compose up`, attendra le conteneur Odoo, affichera l'URL et ouvrira l'ecran de creation de base.

## Commandes directes

```bash
./odoo_manager.sh --list
./odoo_manager.sh --start PROJET
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
- Sous Windows, le mode supporte est Windows 10/11 avec WSL 2 et Docker Desktop. MS-DOS n'est pas un environnement d'execution compatible avec Docker et Odoo.
