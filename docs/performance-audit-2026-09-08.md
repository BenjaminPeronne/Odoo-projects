# Contrôle des performances — 8 septembre 2026

## Périmètre

Lecture du backend et des rafraîchissements React ; profilage local, en lecture seule, de `modules_for('AKAAZ')` sur 1 335 modules. Aucun appel d’installation ni modification de base. Mesures avec `cProfile`, sans paramètre de base : elles excluent SQL, réseau et rendu et ne mesurent pas le temps total ressenti dans l’application installée.

## Résultats

| Lecture | Avant | Après |
| --- | ---: | ---: |
| Première lecture | 1,374 s | 0,775 s |
| Lecture avec cache | 0,640 s | 0,295 s |
| Appels Python, deux lectures | 5 120 612 | 2 784 684 |

Une paire de lectures par version, dans deux processus distincts sur le même projet ; résultats indicatifs, sensibles au cache disque et à la charge du Mac.

## Corrections

- Calcul unique des racines du projet par requête de liste, partagé entre les informations de localisation et de suppression. Auparavant les mêmes chemins étaient résolus pour chaque module.
- Les liens sont toujours contrôlés à chaque lecture et les opérations de suppression refont leur propre contrôle. Aucune mise en cache durable des autorisations de suppression.
- La notification SSE de fin de tâche ne recharge plus directement les modules : la synchronisation existante des tâches terminées s’en charge. Cela retire une demande redondante.
- Le flux SSE ne dépend plus du callback lié au projet et à la base sélectionnés ; changer de sélection ne le reconnecte plus pour cette raison.

## Contrôles et limites

Test de non-régression : après remplacement d’un lien géré par un lien externe, une liste déjà en cache retourne bien `link_only` au lieu de `link_and_storage`. Le contexte de chemins est calculé une seule fois par lecture.

La collecte de l’overview effectue encore une lecture des bases par PostgreSQL démarré. Elle reste séquentielle ; pas de changement sans mesurer la charge des commandes Docker. Les gains sous Windows/WSL, le temps SQL et le rendu de l’application installée restent à mesurer. Aucun gain global en pourcentage n’est revendiqué.
