# Recette de l'environnement WSL — 18 septembre 2026

## Conditions

- Poste : Windows 11 Pro 26200, WSL 2.4.12, Docker Desktop 29.7.2 (toujours installé, Traefik et `SIMPAC_v16` démarrés).
- Code : branche `perf/windows-docker-probes` après fusion de `main` (bouton d'arrêt des activités, backend `onedir`).
- Installateur produit par la CI, installé en mise à niveau de la 0.5.0 de la branche (installation silencieuse en 46 s). Il embarque l'image `sdk-manager-0.5.0.wsl` (546 Mo) et le backend Linux.
- La distribution `SDK-Manager` avait été installée la veille par le bouton « Préparer mon poste ».
- Accords donnés au fil de la recette : installer le build, copier la clé SSH GitLab de Windows dans l'environnement, arrêter l'ancien Traefik de Docker Desktop, migrer `DEMO_CPL03`.

## Résultats

| # | Scénario | Résultat |
|---|---|---|
| 1 | Démarrage de l'application après mise à niveau | ✅ Backend dans la distribution, prêt en 8,1 s (démarrage de la distribution et copie du backend compris) |
| 2 | Remplacement du backend à numéro de version égal | ✅ Empreinte identique à celle de l'installateur ; ancien emplacement supprimé |
| 3 | Chemin des anciens projets transmis au backend | ✅ `ODOO_MANAGER_LEGACY_WORKSPACE=/mnt/c/Users/Aymerick/Odoo-projects` |
| 4 | Temps de réponse de l'interface | ✅ `overview` 12 ms, état système 19 ms, `bootstrap` 21 ms (1 117, 794 et 1 368 ms au début de l'audit) |
| 5 | Docker dans l'environnement | ✅ Moteur 29.8.1 prêt |
| 6 | Traefik, avec Docker Desktop encore actif | ✅ Conflit du port 80 signalé en clair, bouton d'arrêt de l'ancien Traefik proposé |
| 7 | Liste des projets à migrer | ❌ puis corrigé : erreur 500 (voir constat 2) |
| 8 | Prérequis de création | ❌ puis corrigé : Git présent, aucune clé SSH (voir constat 3) |
| 9 | Fermeture de l'application | ✅ Plus aucun backend dans la distribution, port 18765 libéré, aucun orphelin |
| 10 | Liste des projets à migrer, après correction | ✅ HTTP 200 en 272 ms ; seul `DEMO_CPL03` est proposé, les trois autres ayant leur PostgreSQL démarré sous Docker Desktop (verrous vérifiés) |
| 11 | Clé SSH copiée dans l'environnement | ✅ 0600, GitLab répond (`Welcome to GitLab`) |
| 12 | Arrêt de l'ancien Traefik | ✅ Image vérifiée (`traefik:3.6`), conteneur arrêté sans suppression, port 80 libéré |
| 13 | Installation de Traefik dans l'environnement | ✅ 14 s : clone SSH de `docker-local-tools`, image, réseau `traefik-local` |
| 14 | Migration de `DEMO_CPL03` (92 576 fichiers, 2,1 Go) | ✅ Contrôle identique, base en `999`/`0700`, liens relatifs intacts, original conservé. 71 min avec `cp -a` (voir constat 5) |
| 15 | Démarrage de `DEMO_CPL03` | ✅ 18 s jusqu'à la réponse d'Odoo via Traefik ; `http://dev.DEMO_CPL03.localhost/` répond depuis Windows en 0,24 s |
| 16 | Création d'une base (`recette_wsl`, Odoo 19, sans démo) | ✅ **22 s**, contre 90 s le 17/09 sur l'ancien chemin (backend Windows, projet sur `C:\`) |
| 17 | Liste des modules de `DEMO_CPL03` | ✅ 1 447 modules en 314 ms (33 s le 16/09) |

### Build final (commit `23dd64f`), 19 septembre

Installé en 50 s ; backend prêt en 9 s, remplacé à version égale (empreinte identique à l'installateur). Les trois autres projets ont été arrêtés proprement sous Docker Desktop puis migrés un par un avec la copie en flux `tar`.

| # | Scénario | Résultat |
|---|---|---|
| 18 | Verrou PostgreSQL laissé par un arrêt brutal (`SIMPAC_v16`, sortie 255) | ⚠️ Migration refusée à juste titre ; PostgreSQL relancé puis arrêté sous Docker Desktop pour une récupération propre |
| 19 | Migration de `DEMO_CPL` (92 575 fichiers, 2,2 Go) | ✅ 39 min (71 min avec `cp -a` pour `DEMO_CPL03`, de taille équivalente), contrôle identique, base en `999`/`0700` |
| 20 | Migration de `Caritel_v18` (162 471 fichiers, 3,7 Go) | ✅ 67 min, contrôle identique, base en `999`/`0700` |
| 21 | Migration de `SIMPAC_v16` (143 047 fichiers, 5,1 Go) | ✅ 59 min, contrôle identique, base en `999`/`0700` |
| 22 | Démarrage de `SIMPAC_v16` migré | ✅ 74 s, téléchargement de l'image Odoo 16 dans l'environnement compris ; arrêt propre reconnu par PostgreSQL, `/web/login` répond en 0,27 s |

Le flux `tar` est 1,8 fois plus rapide que `cp -a`, et non 5 fois : l'échantillon de mesure sortait du cache de Windows. La durée suit le nombre de fichiers (environ 40 par seconde), pas leur taille : l'ouverture de chaque fichier à travers `/mnt/c` domine.

## Constats et corrections

### Avant la recette, en fusionnant `main`

1. **Backend Linux incomplet.** `main` empaquette désormais le backend en dossier (exécutable et `odoo-manager-backend-runtime/`). La branche ne copiait que l'exécutable dans la distribution : il n'aurait pas démarré. Le dossier entier est copié, puis permuté d'un bloc.
2. **Backend jamais mis à jour à version égale.** La copie n'avait lieu que si le numéro de version changeait : un nouveau build 0.5.0 gardait l'ancien backend. Le backend est désormais comparé par l'empreinte de son exécutable, et vérifié avant chaque démarrage.
3. **Migration jamais proposée.** Rien ne transmettait au backend Linux l'emplacement des anciens projets. Electron le lit dans la configuration Windows et le passe au lancement.
4. **Bascule manuelle.** Après « Préparer mon poste », l'application restait sur le backend Windows jusqu'à un redémarrage manuel ; c'est ce qui s'est produit pendant la première recette. Elle redémarre désormais d'elle-même, sauf si une action est en cours.

### Pendant la recette

1. **Port 80 occupé.** Toutes les distributions WSL partagent le réseau de la VM, où le relais de Docker Desktop tient `127.0.0.1:80` pour son Traefik (`address already in use` au démarrage d'un conteneur publiant le port 80). Tant qu'il tourne, les projets de l'environnement Linux ne sont pas joignables par leur adresse. Le backend lit `/proc/net/tcp`, sans droits, et signale le conflit ; l'interface propose d'arrêter l'ancien Traefik, sans le supprimer, et uniquement si son image est bien Traefik.
2. **Migration en erreur 500.** `postgresql_data` appartient à l'utilisateur PostgreSQL du conteneur (uid 999, droits 0700) ; le backend, qui tourne sous `sdk`, ne pouvait ni lire le verrou ni copier la base. Il passe désormais par `sudo -n`, autorisé pour `sdk` par l'image, et `cp -a` conserve propriétaires et droits. Vérifié en lecture seule sur `DEMO_CPL03` : 92 576 fichiers et liens, dont les 2 050 de la base, cibles de liens relues en format Linux ; verrou correct (`DEMO_CPL03` arrêté, `SIMPAC_v16` en cours).
3. **Liste lue deux fois.** Lister un projet à travers `/mnt/c` prend 112 s ; la même liste sert désormais à la mesure et au contrôle de la copie.
4. **Clé SSH absente.** La clé GitLab reste côté Windows : sans elle, aucun clone. Un bouton « Utiliser ma clé Windows » la copie dans l'environnement, sur demande seulement, en 0600, sans jamais écraser une clé existante.
5. **Migration lente.** `cp -a` relit les attributs étendus de chaque fichier à travers `/mnt/c` : 71 min pour `DEMO_CPL03`. Mesuré sur 479 fichiers : `cp -a` 20,9 s, `cp` sans attributs étendus 7,9 s, flux `tar` 3,8 s. La copie passe désormais par `tar --numeric-owner` sous `bash -o pipefail`, qui conserve propriétaire et droits de la base.
6. **Clé proposée alors qu'elle était déjà copiée.** L'assistant, ouvert avant l'import, affichait encore le bouton ; le second import échouait (« Une clé id_ed25519 existe déjà »), avec l'enveloppe technique d'Electron dans le message. Une clé identique est désormais confirmée, une clé différente n'est jamais remplacée, l'assistant relit son état après chaque tentative, et l'enveloppe est retirée de tous les messages du pont natif.

### Hors code

- **Installateur bloqué par Chrome** (« Téléchargement dangereux bloqué »). L'installateur et le backend ne sont pas signés (constat 9 du 16/09). La signature de code est nécessaire avant de diffuser l'installateur.

## État laissé sur le poste

- Les quatre projets sont dans l'environnement Linux ; `DEMO_CPL03` (base `recette_wsl`) et `SIMPAC_v16` y tournent. Les originaux restent intacts sur `C:\`, à supprimer par l'utilisateur après vérification.
- Sous Docker Desktop, tous les conteneurs des projets et l'ancien Traefik sont arrêtés, sans suppression.

## Non couvert

- Copie parallèle ou exclusion de l'antivirus pour accélérer la migration (non mesurées).
- Poste neuf sans WSL : installation de WSL, redémarrage et reprise.
- Plusieurs projets démarrés simultanément dans l'environnement, et comportement de la VM WSL quand l'application est fermée avec des projets en cours.
