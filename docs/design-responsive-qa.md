# Contrôle design et responsive — 7 septembre 2026

Contrôle de l’interface locale avec un projet déjà démarré (DOMEAU), sans lancement d’installation, de mise à jour, de création ou de restauration.

## Défaut reproduit

L’ouverture de « Toutes les origines » faisait passer la largeur de `main` de 1105 à 1090 px pour un viewport de 1120 px. Le document réservait déjà 15 px avec `scrollbar-gutter: stable`. Radix injectait ensuite une marge de 15 px sur le body, écrasant la règle de même spécificité de l’application.

La règle `html body[data-scroll-locked]` prend désormais priorité. Elle est limitée aux moteurs prenant en charge `scrollbar-gutter`, pour conserver la compensation de Radix dans les autres moteurs.

## Corrections responsive

- Les boutons peuvent afficher les libellés longs sur plusieurs lignes, avec une hauteur automatique et une hauteur minimale conservée.
- Leur dimensionnement inclut les espacements internes ; les marges négatives des boutons discrets sont neutralisées. Cela corrige notamment le bouton « Gestionnaire de bases Odoo ».
- Les onglets conservent leurs libellés sur mobile, avec une police compacte et sans les icônes décoratives sous 640 px.
- La hauteur maximale des fenêtres tient compte des marges haute et basse de Radix et de la hauteur dynamique du viewport.
- Le filtre Origine dispose de 210 px dans la grille de bureau pour afficher son libellé complet.

## Contrôles effectués dans le navigateur intégré

| Contrôle | Résultat |
| --- | --- |
| Bases, Modules, Logs, Actions à 360, 768, 1024 et 1440 px | Aucun débordement horizontal non contenu détecté dans les 16 combinaisons |
| État, Origine et Base Odoo : ouverture puis Échap à 360, 768 et 1440 px | Largeur de page identique avant, pendant et après ouverture : 345, 753 et 1425 px respectivement |
| Menu « Autres actions » d’un module à 1120 px | Largeur maintenue à 1105 px |
| Paramètres, À propos, Nouveau projet à 360 et 1440 px | Aucun décalage de la page ni débordement horizontal détecté |
| Liste Version Odoo dans Nouveau projet à 360 px | Verrouillage imbriqué : largeur et position de la fenêtre inchangées |
| Restauration ZIP à 360 px | Aucun débordement horizontal du formulaire |
| Nouveau projet à 360 × 780 px | Fenêtre de 313 px de large, bord inférieur à environ 733 px : marge basse conservée |

Les listes longues, textes tronqués volontairement et zones de logs conservent leur défilement propre. Les écrans ont été inspectés visuellement sur mobile et bureau. La validation porte sur le navigateur intégré ; les WebViews des installateurs macOS, Windows et Linux n’ont pas été exécutées pour ce contrôle.

Pour reproduire : lancer `./odoo_next_gui.sh --background`, choisir un projet déjà démarré, puis comparer la largeur et le bord droit de `main` à l’ouverture et à la fermeture des listes et fenêtres. Ne déclencher aucune opération métier pendant ce contrôle.

## Complément du 8 septembre 2026 — actions du projet, application macOS

- Grille stable de trois colonnes à partir de 640 px, largeur de 480 px sur grand écran ; « Ouvrir Odoo » occupe la seconde ligne sous 640 px.
- Textes et icônes blancs sur les boutons pleins ; orange `#c2540d` (contraste blanc calculé : 4,60:1), rouge conservé pour les actions destructives.
- Relief et effets de survol communs. La variante orange utilise une classe explicite ; les boutons Démarrer/Arrêter ont des clés distinctes pour renouveler leur rendu lors du changement d’état.
- Contrôle visuel dans `/Applications/SDK Local Manager.app` : AKAAZ arrêté → DOMEAU allumé → AKAAZ arrêté. Largeurs et alignements stables, texte blanc et couleurs orange/rouge confirmés. Aucune action de démarrage ou d’arrêt des projets n’a été déclenchée.
- Disposition également contrôlée dans une demi-fenêtre native. Le format inférieur à 640 px n’a pas été validé visuellement lors de ce complément.
- Build Next.js/TypeScript, compilation Tauri et vérification de signature locale réussis. Application installée ; version d’origine conservée dans `dist/backup-design-20260907-231914/SDK Local Manager.app`.
