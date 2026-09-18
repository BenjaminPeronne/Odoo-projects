# Audit de performance — macOS, 17 septembre 2026

## Périmètre et méthode

Mesures sur le poste de développement (macOS 27, Apple Silicon, Docker Desktop 29.8.0), avec
le workspace réel : 17 dossiers, 14 projets Odoo reconnus, 1 projet démarré (DOMEAU),
1 344 modules sur le projet le plus gros (AKAAZ).

Trois bancs distincts, tous en lecture seule — aucune installation, aucune écriture en base :

- démarrage du backend chronométré depuis `Popen` jusqu'à la première réponse de `/api/health`,
  binaire packagé (`/Applications/SDK Local Manager.app`) contre le même code lancé depuis les sources ;
- latence des routes HTTP mesurée sur le backend lancé depuis les sources, port dédié,
  médiane sur 8 à 10 appels, avec et sans abonné `/api/stream` ;
- coût du rendu React mesuré dans le navigateur sur **l'export statique de production**
  (`next build`), pas sur `next dev` — voir la remarque en fin de document.

Les chiffres ci-dessous sont des médianes sur ce poste. Ils dépendent du cache disque et de la
charge de Docker Desktop ; aucun pourcentage de gain global n'est revendiqué.

---

## Résultat principal : 5,2 s perdues à chaque lancement

Le sidecar Python est packagé en `--onefile` sur macOS et Linux
([scripts/build_electron_sidecar.py:51](../scripts/build_electron_sidecar.py#L51)). Windows est
déjà passé en `--onedir`. En `--onefile`, le bootloader PyInstaller **réextrait 17 Mo**
(runtime Python + dylibs) dans `/var/folders/.../T/_MEIxxxxx` **à chaque démarrage**, puis
macOS revalide la signature de chaque dylib fraîchement écrit.

Sept démarrages successifs, même machine, même source, même PyInstaller 6.21 :

| Packaging | Démarrages (ms) | Médiane |
| --- | --- | ---: |
| `--onefile` (livré aujourd'hui) | 5357, 5340, 5252, 5430, 5231, 5333, 5251 | **5 350 ms** |
| `--onedir` | 260, 129, 129, 130, 99, 129, 128 | **129 ms** |
| Sources (`python3`) | — | 162 ms |

Ces 5,2 s sont intégralement sur le chemin critique du premier pixel :
[electron/main.cjs:100](../odoo-manager-next/electron/main.cjs#L100) fait `await backend.start()`
**avant** de créer la `BrowserWindow`. L'utilisateur n'a donc aucune fenêtre pendant 5,3 s,
puis la fenêtre apparaît et charge l'interface.

Effet de bord observé : deux dossiers `_MEI*` de 17 Mo traînaient déjà dans le dossier
temporaire. Un `SIGKILL` du sidecar laisse son extraction derrière lui.

**Correctifs** (deux, indépendants) :

1. Passer macOS et Linux en `--onedir`, comme Windows. Le dossier `Resources/backend/` contient
   alors un exécutable plus un dossier de runtime au lieu d'un seul fichier : `electron-builder.yml`
   et `scripts/build_desktop.py` doivent suivre. Gain mesuré : **−5,2 s**, sur macOS et sur Linux.
2. Créer et afficher la fenêtre avant/pendant le démarrage du backend. L'interface a déjà son
   écran « Chargement du gestionnaire » et sa logique de reprise (`BOOTSTRAP_RETRY_DELAYS_MS`) ;
   elle n'attend que `backend_endpoint`. Cela supprime le temps mort **perçu**, indépendamment du
   packaging, et protège aussi contre un backend lent sur un poste chargé.

Point annexe : `Backend.start()` sonde toutes les 250 ms
([electron/runtime.cjs:104](../odoo-manager-next/electron/runtime.cjs#L104)). Avec un démarrage
ramené à 129 ms, ce pas ajoute jusqu'à 250 ms d'attente inutile. Un pas court au début
(25 ms) puis croissant récupère ce reste.

---

## Backend : les appels `docker` en série

### Coût unitaire mesuré sur ce poste

| Commande | Temps mur | CPU |
| --- | ---: | ---: |
| `docker ps -a --format …` | 24,6 ms | 16,6 ms |
| `docker version --format …` | 19,1 ms | 14,3 ms |
| `docker exec … psql -Atc …` | 50 ms | — |
| `docker inspect -f …` | 115 ms (à froid) | — |
| `docker info` | 957 ms | — |

### `/api/overview` : 93,7 ms, dont 50 ms évitables

Décomposition mesurée de la route, workspace réel, **1 seul projet démarré** :

| Étape | Coût |
| --- | ---: |
| `docker_status()` — `docker version`, recalculé à chaque appel | 21,4 ms |
| `container_statuses()` — un seul `docker ps -a`, déjà groupé | 23,7 ms |
| `list_databases_for()` — un `docker exec psql` **par projet démarré** | 49,9 ms |
| `detected_traefik()` — cache 10 s, 42,9 ms quand il expire | ~0 ms |
| **Total `/api/overview`** | **93,7 ms** |

Deux défauts :

1. La route HTTP appelle `overview()` sans `databases_max_age`
   ([odoo_manager_web.py:5325](../odoo_manager_web.py#L5325)), ce qui **court-circuite complètement**
   le cache de 30 s qu'utilise la boucle SSE. La même fonction coûte 24,6 ms par la boucle SSE et
   93,7 ms par la route. L'interface appelle cette route toutes les 45 s au repos, toutes les 15 s
   pendant une action, et à chaque `job_completed`.
2. Les sondes `psql` sont **séquentielles**, une par projet démarré. C'est le seul poste qui
   grandit avec l'usage : sur ce poste un seul projet tourne, mais la boucle est en O(projets démarrés).

Mesure de la mise en parallèle des sondes `psql` (`ThreadPoolExecutor`, 8 fils) :

| Projets démarrés | Séquentiel | Parallèle | Gain |
| ---: | ---: | ---: | ---: |
| 1 | 68 ms | 56 ms | 17 % |
| 3 | 154 ms | 61 ms | 61 % |
| 5 | 233 ms | 92 ms | 60 % |
| 8 | 374 ms | 154 ms | 59 % |

**Correctifs** : donner un `databases_max_age` court (3–5 s) à la route `/api/overview` plutôt que
`None`, partager la sonde `docker_status` avec la boucle SSE au lieu de la refaire, et paralléliser
les sondes `psql` par projet. Sur 5 projets démarrés, `/api/overview` passerait d'environ
250 ms à environ 70 ms.

### `project_diagnostics` : 1,1 s, 13 `docker exec` en file

[odoo_manager_web.py:2069](../odoo_manager_web.py#L2069) enchaîne **13 `docker exec` strictement
séquentiels** (529 ms cumulées) plus un scan `module_dirs` (219 ms). L'utilisateur attend
1,1 s après avoir cliqué « Diagnostic ». Les requêtes SQL sont indépendantes les unes des
autres : les grouper en une seule session `psql` (plusieurs `-c`) ou les paralléliser ramènerait
ce bouton sous 400 ms.

### Sondes au repos : ~42 processus `docker` par minute

Application ouverte, rien en cours, la boucle
[`event_watch_loop`](../odoo_manager_web.py#L5062) tourne toutes les 2 s :

- `overview()` → 1 `docker ps -a` toutes les 2 s → 30/min
- `docker_status()` → 1 `docker version` toutes les 10 s → 6/min
- `detected_traefik()` → 1 `docker ps` toutes les 10 s → 6/min
- `list_databases_for()` → 1 `docker exec psql` / 30 s / projet démarré

Soit **~42 invocations du CLI docker par minute**, ≈ 0,68 s CPU/min (**1,1 % d'un cœur**) rien
que pour les sondes, côté client seulement — le démon Docker paie sa part en plus. Le processus
Python lui-même ne consomme que 0,4 % d'un cœur : le coût est presque entièrement dans les
`fork/exec` du CLI.

Ce n'est pas un blocage fonctionnel, mais c'est une dépense permanente sur batterie, et elle
se dégrade quand Docker Desktop est chargé (`docker info` est monté à 957 ms pendant les mesures).
Deux pistes, dans l'ordre de sûreté :

- allonger l'intervalle quand la fenêtre n'a pas le focus ou qu'aucune action n'est en cours
  (`EVENT_WATCH_INTERVAL_SECONDS = 2` est un pas de voyant ON/OFF, pertinent pendant une action,
  très cher au repos) ;
- à terme, remplacer `docker ps` en boucle par `docker events`, qui pousse les changements d'état
  sur un seul processus longue durée. C'est un chantier plus lourd, à ne pas engager avant les
  correctifs ci-dessus.

---

## Liste des modules : 152 ms, dont ~36 ms de pathlib pur

`/api/projects/DOMEAU/modules?db=DOMEAU` : **152 ms**, charge utile de **930 KiB** pour
1 344 modules.

Sur AKAAZ (1 335 modules), cache fichiers tiède : `modules_for` = 238 ms à froid, 90 ms avec le
cache de 45 s. Le profil montre que `module_removal_info` représente à lui seul 237 ms des
243 ms profilés — c'est voulu, les autorisations de suppression ne sont jamais mises en cache
(décision de l'audit du 8 septembre, à conserver). Mais l'essentiel de ce coût n'est pas de l'I/O :

| Poste, 1 335 modules | Actuel | Variante en chaînes | Nature |
| --- | ---: | ---: | --- |
| `module_parent_in_layout` | 19,4 ms | **1,0 ms** | CPU pur |
| `path_is_relative_to` (×2/module) | 17,8 ms | **0,3 ms** | CPU pur |
| `path.is_symlink()` | 2,3 ms | — | I/O incompressible |
| `safe_resolve()` sur les liens | 18,2 ms | — | I/O incompressible |
| **`module_removal_info` complet** | **64,6 ms** | **~28 ms** | |

Deux causes, toutes deux sans rapport avec la sécurité de l'opération :

1. [`module_parent_in_layout`](../odoo_manager_web.py#L1765) **reconstruit son dictionnaire de
   3 entrées pour chaque module** : 1 335 constructions de `Path` et 1 335 hachages de `Path`
   par lecture, pour un dictionnaire invariant sur toute la liste. Il appartient au contexte
   `module_layout_context`, calculé une fois.
2. [`path_is_relative_to`](../odoo_manager_web.py#L300) s'appuie sur `Path.relative_to`, qui lève et
   rattrape une `ValueError` et parcourt les parents. Sur des chemins déjà résolus, une
   comparaison de préfixe de chaîne est équivalente et environ 50 fois plus rapide.

**Gain attendu : ~36 ms par lecture de modules**, sans toucher aux contrôles de sécurité ni
introduire le moindre cache. Le même motif est présent dans `module_location_info`, sur le
chemin froid.

En revanche, la taille de la charge utile **n'est pas** un problème : `json.dumps` des 930 KiB
coûte 3,5 ms et `json.loads` 3,5 ms côté navigateur. Retirer `removal_note`, `source_path` et
`link_path` ramènerait le corps à 534 KiB pour un gain négligeable. À ne pas faire.

---

## Frontend : correct en production, fragile par construction

`app/page.tsx` fait **7 654 lignes**, dont un unique composant `Home` à partir de la ligne 1070,
avec **150 `useState`**, 40 `useEffect`, et **zéro `React.memo`**. Chaque changement d'état
ré-exécute tout le corps du composant et tout son JSX.

Mesures sur l'export statique de production, projet DOMEAU, 1 344 modules chargés :

| Interaction | Blocage du fil principal |
| --- | ---: |
| Frappe dans la recherche de modules | 14 – 36 ms |
| Frappe dans la recherche de projets | 7 – 24 ms |
| 30 s au repos, application ouverte | **0 tâche longue** |

Au repos l'application est sobre : 4 requêtes HTTP en 30 s (`/api/jobs` ×3, `/api/system/status` ×1),
aucune tâche longue. La boucle SSE ne publie que sur changement réel et `applyOverview` /
`applyJobs` comparent avant de remplacer l'état, donc aucun rendu inutile. C'est bien fait.

Deux observations, aucune urgente :

- Une frappe dans la recherche de **projets** coûte autant qu'une frappe dans la recherche de
  **modules** : la preuve que le coût est le re-rendu de la page entière, pas la liste affichée
  (le DOM ne fait que 1 700 nœuds, les modules sont paginés par 50). À 20 ms on reste sous le
  budget de 50 ms, mais la marge est faible et elle se dégradera sur un poste plus lent que
  celui-ci — ce sont des Apple Silicon ; sur un portable Windows ou Linux d'entrée de gamme,
  le même rendu sera 3 à 5 fois plus lent.
- **Attention à ne pas mesurer ça sur `next dev`** : la même frappe y coûte 490 – 570 ms
  (React en mode développement, double rendu StrictMode, sources non minifiées). J'ai
  moi-même relevé ce chiffre avant de refaire la mesure sur le build de production. Ce n'est
  pas ce que voient les utilisateurs.

Aucune découpe du composant n'est justifiée par une mesure aujourd'hui. Si elle est engagée un
jour, le point de départ utile est d'extraire la liste de modules et le panneau d'activité en
composants mémoïsés — ce sont les seuls sous-arbres dont le contenu change indépendamment du reste.

---

## Priorités

| # | Correctif | Gain mesuré | Risque | Portée |
| --- | --- | --- | --- | --- |
| 1 | `--onedir` au lieu de `--onefile` (macOS, Linux) | **−5,2 s au lancement** | faible, packaging seul | macOS + Linux |
| 2 | Afficher la fenêtre avant d'attendre le backend | supprime le temps mort perçu | faible | toutes |
| 3 | Cache court + sondes `psql` parallèles sur `/api/overview` | −60 % dès 3 projets démarrés | faible | toutes |
| 4 | `project_diagnostics` : grouper/paralléliser les 13 `docker exec` | 1,1 s → < 400 ms | faible | toutes |
| 5 | pathlib → chaînes dans `module_removal_info` | −36 ms par lecture de modules | faible, pur CPU | toutes |
| 6 | Espacer les sondes quand la fenêtre n'a pas le focus | −1 % d'un cœur au repos | moyen, touche la fraîcheur des voyants | toutes |
| 7 | `docker events` au lieu de `docker ps` en boucle | à mesurer | élevé | toutes |

Les correctifs 1 à 5 sont indépendants les uns des autres et n'ont aucun effet sur le
comportement fonctionnel. Le 1 est de loin le plus rentable : à lui seul il représente plus de
temps gagné que tous les autres réunis.

Tous ces correctifs s'appliquent tels quels à Linux, qui partage le packaging `--onefile` et
l'intégralité du backend Python. Windows bénéficie des points 2 à 7 mais est déjà en `--onedir`.

## Limites

Un seul projet était démarré pendant les mesures : les chiffres en O(projets démarrés) sont
extrapolés depuis des sondes répétées sur le même conteneur, pas observés sur cinq projets réels.
Le temps SQL d'Odoo, le rendu sur un poste non-Apple Silicon et le comportement sous Docker
Desktop chargé n'ont pas été mesurés. Aucune de ces limites n'affecte le résultat n° 1, qui est
un A/B direct sur le même binaire.

---

## Corrections appliquées

Correctifs 1 à 5 appliqués. Les points 6 et 7 (espacer les sondes au repos, `docker events`)
ne le sont pas : ils changent la fraîcheur des voyants et demandent une décision à part.

### 1. Sidecar en `--onedir` sur les trois systèmes

[scripts/build_electron_sidecar.py](../scripts/build_electron_sidecar.py) produit désormais partout
l'exécutable et son dossier `odoo-manager-backend-runtime/` côte à côte, comme Windows le faisait
déjà. Le chemin du sidecar ne change pas pour Electron ; `electron-builder.yml` n'a pas bougé.

Vérifié sur le `.app` complet construit par `scripts/build_desktop.py --bundles app` : sidecar
signé ad hoc avec hardened runtime (`flags=0x10002(adhoc,runtime)`), smoke test Electron packagé
au vert (interface rendue, bootstrap complet, backend arrêté à la fermeture), aucun dossier
`_MEI*` laissé dans le temporaire.

Mesure avec le vrai code de démarrage d'Electron (`Backend.start()`), 12 lancements chacun :

| | Médiane |
| --- | ---: |
| App installée (`--onefile`, sondage d'origine) | 6 299 ms |
| Nouveau `.app` (`--onedir`, nouveau sondage) | **187 ms** |

L'écart avec les 5,3 s du matin tient à la charge du Mac après la mise à jour système ; le
gain est du même ordre dans les deux séries.

Artefact de mesure écarté : sonder `/api/health` avec `http.client` de Python donne par moments
1,1 s au lieu de 0,13 s. C'est une retransmission de SYN côté client (pare-feu applicatif macOS
actif) ; elle disparaît avec un délai de connexion de 0,1 s et n'apparaît pas avec le `fetch`
d'Electron. Le backend lui-même démarre en 110 à 250 ms.

### 2. Fenêtre affichée pendant le démarrage du backend

`Backend` sépare `reserve()` (port et journal) de `start()` (lancement et attente). La fenêtre,
sa politique de sécurité et le pont `backend-endpoint` n'ont besoin que de l'adresse : la fenêtre
est créée tout de suite et le backend démarre en parallèle. L'interface garde son écran de
chargement et ses reprises. Le sondage de santé part à 25 ms et s'allonge jusqu'à 250 ms, à
budget inchangé (30 s) : 259 → 187 ms pour détecter le même backend prêt.

### 3. Overview : sondes `psql` en parallèle

`overview_databases_by_project` lance en parallèle (8 au plus) les sondes que le cache ne sert
pas. Quand le cache de 30 s répond, aucun fil n'est créé.

**Écart avec la recommandation** : pas de cache ajouté sur la route `/api/overview`. Elle sert
aussi les actualisations explicites (bouton « Actualiser », fin d'action) : un cache y aurait
masqué une base créée hors de l'application. La parallélisation seule donne le gain sans ce
risque.

Mesure réelle, 2 projets démarrés (Caritel_v18, DEMO_CPL), ancienne et nouvelle fonction
appelées en alternance : route `/api/overview` 135 → 96 ms, overview du bootstrap 122 → 76 ms.
Le gain croît avec le nombre de projets démarrés (sondes du matin : 233 → 92 ms pour 5). Un test
vérifie que les sondes se chevauchent et que les projets servis par le cache ne sont pas sondés.

### 4. Diagnostic : bases en parallèle, contrôles redondants retirés

- un seul `docker ps` au lieu de deux `docker inspect` ;
- plus de `docker inspect` avant la liste des bases ni avant chaque base : PostgreSQL vient
  d'être vu démarré (`installed_modules` gagne le paramètre `check_container`, comme
  `list_databases_for`) ;
- chaque base est diagnostiquée en parallèle, et le parcours des addons avance pendant la
  lecture des bases.

Pour 3 bases : 14 appels `docker` en série avant, 9 appels après dont 5 en série.

Équivalence vérifiée en comparant l'ancienne et la nouvelle fonction sur 5 scénarios simulés
(3 bases aux problèmes variés, base illisible, base saine, aucune base, PostgreSQL arrêté,
Docker indisponible), puis sur les projets réels : sorties JSON identiques. Un test du dépôt
vérifie le parallélisme et l'ordre des bases dans le rapport.

| Projet réel | Avant | Après |
| --- | ---: | ---: |
| Caritel_v18, 5 bases | 960 ms | 516 ms |
| DEMO_CPL, 1 base | 278 ms | 196 ms |

### 5. Liste des modules : comparaisons en chaînes

`module_layout_context` renvoie un `ModuleLayout` qui précalcule, une fois par liste, la table
des parents résolus et les tests « est dans addons-store / dans un dossier d'import ». Ces tests
comparent des chaînes (`path_scope`) avec la même règle que pathlib.

`path_is_relative_to` n'est **pas** modifiée : elle garde la suppression de modules et le
contrôle du filestore. Le raccourci ne sert qu'à la classification affichée, que la suppression
revalide.

Vérifications :

- sortie de `modules_for` identique avant/après sur les 14 projets du workspace réel
  (19 583 modules), avec et sans cache ;
- test d'équivalence `path_scope` / `path_is_relative_to` (racine, préfixe commun `addons-store` /
  `addons-store-old`, `..`, casse) ; la CI le rejoue sous Windows, où pathlib ignore la casse.

Mesures sur les 14 projets, ordre alterné pour neutraliser le cache disque :

| Lecture | Avant | Après | Gain |
| --- | ---: | ---: | ---: |
| Avec le cache de 45 s (contrôles de suppression refaits) | 986 ms | 412 ms | 58 % |
| Scan complet | 5 256 ms | 3 875 ms | 26 % |

### Reste à faire

- Constaté pendant les corrections, hors périmètre : `modules_for` appelle `installed_modules`
  avec sa garde `docker inspect` à chaque lecture de modules (~25 ms). Un `docker exec` sur un
  conteneur arrêté échoue déjà seul ; la garde pourrait être retirée à cet endroit.
