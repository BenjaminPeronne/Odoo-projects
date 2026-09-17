# Recette Windows — 17 septembre 2026

## Conditions

- Poste : Windows 11 Pro 26200, mode développeur activé, Docker Desktop (moteur 29.5.3), WSL 2 (Ubuntu), Git pour Windows 2.55.
- Code testé : `main` (`c430e5a`) avec les corrections WSL non commitées du 17 septembre, plus le correctif trouvé pendant cette recette.
- Méthode : backend lancé depuis les sources sur le port 18799, interface Next.js pilotée dans un navigateur. Workspace réel `C:\Users\benja\Odoo-projects` avec deux projets :
  - `Caritel` (Odoo 18, 1 416 liens WSL hérités de la 0.3.0) ;
  - `DEMO_01` (Odoo 19, 1 449 modules, liens natifs).
- Les processus enfants du backend sont échantillonnés toutes les 100 à 150 ms pour compter les lancements de `wsl.exe`.
- Avant la conversion, les 1 416 cibles de liens de Caritel ont été sauvegardées hors du dépôt.

## Résultats

| # | Scénario | Attendu | Résultat |
|---|---|---|---|
| 1 | Interface ouverte 60 s | aucun `wsl.exe` | ✅ 0 `wsl.exe`, 25 `docker.exe` |
| 2a | Conversion des liens de Caritel (bouton du bandeau) | 1 416 liens convertis | ❌ **WinError 5** sur les 1 416 liens, aucun dégât. Voir « Anomalie » |
| 2b | Reprise de la conversion après correctif (bouton « Reprendre la conversion ») | reprise depuis le journal, bandeau retiré | ✅ 1 416/1 416 convertis, journal supprimé, bandeau retiré |
| 2c | Contrôle après conversion | liens natifs, cibles identiques, lisibles | ✅ 1 416 liens NTFS, 0 lien WSL, cibles identiques à la sauvegarde (1 416/1 416), manifestes lisibles par Windows et dans Docker (1 416/1 416) |
| 3 | Liste des modules de Caritel | sans WSL, rapide | ✅ 2,5 s puis 0,95 s (avant : 31,8 s), 0 `wsl.exe`, 1 417 modules |
| 4 | Création de la base `demo_test` sur `DEMO_01` (formulaire) | plus de `getaddrinfo failed` | ✅ Odoo HTTP 303, « Base créée » en 90 s |
| 5 | Restauration d'une sauvegarde ZIP de `demo_test` sous le nom `demo_restore` | restauration, puis neutralisation contrôlée | ✅ en 72 s : base marquée neutralisée, 0 cron métier, 0 serveur de messagerie actif, fichier temporaire supprimé |
| 6 | Suppression de `demo_restore` (menu de la base) | base et filestore supprimés | ✅ Odoo HTTP 303 en 3 s, filestore absent |
| 7 | Onglet Modules de `DEMO_01` sur `demo_test` | 1 449 modules, états corrects | ✅ environ 2,0 s, états de base compris (avant : 33 à 34 s) |
| 8 | Assistant de nouveau projet (prérequis Git et SSH) | Git Windows, clé détectée, sans WSL | ✅ Git 2.55 Windows, `id_ed25519.pub`, workspace prêt, 0 `wsl.exe` sur 45 s (Modules compris) |
| 9 | Accès GitLab Sudokeys par l'environnement Git de l'application | SSH non interactif fonctionnel | ✅ `ls-remote` du modèle Docker : 7 branches en 2,5 s |

Scénario 5 : la sauvegarde a été produite par `pg_dump` et le filestore au format Odoo (`dump.sql`, `filestore/`, `manifest.json`), puis envoyée à la route de restauration de l'application. Le sélecteur de fichier de l'interface n'est pas automatisable.

La base `demo_test` est conservée sur `DEMO_01`. `demo_restore` a été supprimée à la fin.

## Anomalie trouvée et corrigée

**Conversion refusée par Windows (WinError 5).** Les liens WSL créés vers un dossier existant portent l'attribut répertoire (`FILE_ATTRIBUTE_DIRECTORY`). Windows refuse alors leur suppression comme fichier (`DeleteFile`) et exige `RemoveDirectory`. Celui-ci ne retire que le point d'analyse : la cible n'est pas parcourue.

- **Impact :** aucune perte. La conversion notait les cibles avant de modifier quoi que ce soit, et l'échec survenait avant toute suppression. Les 1 416 liens sont restés intacts et le bandeau a proposé la reprise.
- **Pourquoi les tests ne l'avaient pas vu :** ils utilisaient des liens WSL créés vers une cible absente, sans cet attribut.
- **Correctif :** `remove_link_entry` tente `unlink`, puis `rmdir` en cas de refus. Il a d'abord été validé sur un seul lien réel (`account` : cible intacte, 16 fichiers).
- **Non-régression :** `test_links_with_the_directory_attribute_are_converted`. Ce test échoue avec l'ancien comportement.
- **Attention :** la version installée `app-v0.5.0-build9` contient encore ce défaut. La conversion y échoue sans dégât. Il faut publier un nouveau build avant de proposer la conversion à d'autres postes.

## Observations hors périmètre

- **Requêtes de modules en double :** à l'ouverture de l'onglet Modules, l'interface envoie deux fois la même requête en parallèle, et une fois pour une base qui venait d'être supprimée. Sans impact fonctionnel.
- **VM WSL maintenue éveillée par la version installée :** l'app installée (build9) sonde encore Docker dans WSL, d'où Ubuntu toujours démarré pendant la recette. Le prochain build corrige ce point.

## Tests automatisés

369 tests Python réussis (11 ignorés), vérification TypeScript et 13 tests Electron réussis.
