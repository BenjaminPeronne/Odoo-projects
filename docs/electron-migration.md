# Migration Electron — SDK Local Manager 0.2.0

## Périmètre

Electron 44.3.0, version stable vérifiée sur https://releases.electronjs.org/ et
avec `npm view electron version`. Le verrouillage npm fixe cette version.
Electron-builder 26.15.3 construit les installateurs.

L’interface Next.js/Radix existante est exportée sans refonte : les pages Bases,
Modules, Logs, Actions et les assistants conservent leurs composants, styles,
logos et polices. La maquette de design n’est pas intégrée.

Les projets Odoo, les bases, les dépôts et les réglages du backend ne sont pas
migrés ni déplacés. Le fichier de configuration reste celui d’Odoo Manager :

- macOS : `~/Library/Application Support/Odoo Manager/config.json` ;
- Windows : `%APPDATA%/Odoo Manager/config.json` ;
- Linux : `$XDG_CONFIG_HOME/odoo-manager/config.json` ou `~/.config/odoo-manager/config.json` ;
- les variables `ODOO_MANAGER_CONFIG` et `ODOO_MANAGER_CONFIG_DIR` restent prioritaires.

Les préférences du moteur WebView, telles que le thème conservé dans son
localStorage, ne sont pas importées automatiquement depuis Tauri. La préférence
de thème pourra être resélectionnée. Les réglages métier stockés dans le JSON
sont conservés.

## Architecture

- `electron/main.cjs` : fenêtre, cycle de vie, protocole local, permissions et IPC.
- `electron/preload.cjs` : huit capacités natives explicites, sans accès brut à IPC.
- `electron/runtime.cjs` : configuration, choix du port, backend et règles d’URL/CSP.
- `lib/desktop.ts` : contrat TypeScript entre frontend et preload.
- `scripts/build_electron_sidecar.py` : backend Python embarqué, onefile sur macOS/Linux,
  onedir avec runtime adjacent sur Windows.
- `electron-builder.yml` : installateurs et ressources hors ASAR pour le backend.

Le rendu utilise `app://sdk`, `contextIsolation`, le sandbox Chromium et
`nodeIntegration: false`. La CSP autorise les empreintes exactes des scripts de
bootstrap Next, et seulement le port choisi pour les connexions au backend.
Les liens HTTP(S) sont ouverts dans le navigateur système. Les schémas de fichiers
et commandes ne sont pas autorisés. Le presse-papiers en écriture est permis pour
l’interface locale, les autres permissions Web sont refusées ; les notifications
utilisent l’API native et restent soumises aux réglages du système.

Le backend est lancé sur loopback. Le port configuré est utilisé s’il est libre,
sinon un port éphémère est choisi. Une identité d’instance permet de vérifier le
backend avant d’envoyer la demande d’arrêt. La fermeture de l’application attend
l’arrêt du backend et prévoit un arrêt forcé de son propre processus en dernier recours.
Les commandes backend déjà existantes conservent leur comportement de fermeture.

## Construction et tests

```sh
cd odoo-manager-next
npm ci
npm run test:desktop
npm run build:desktop
cd ..
python3 -m unittest discover -s tests -q
sh scripts/build_local_desktop.sh
python3 scripts/smoke_test_electron.py --bundle-directory odoo-manager-next/release
```

Ne pas lancer `typecheck` simultanément avec `build:desktop` : Next régénère les
types `.next/types` utilisés par TypeScript. Le script de construction les exécute
séquentiellement.

Le smoke test Electron utilise une configuration et un workspace temporaires.
Il vérifie le moteur chargé, le preload, l’absence de Node dans la page, le rendu
React, la santé et le bootstrap API depuis Chromium, puis la disparition du backend
après fermeture. Il ne lance aucune opération sur une base Odoo réelle.

Le workflow GitHub compile les trois systèmes et conserve les installateurs un
jour. Il ne se lance que manuellement ou par tag `app-v*`. Aucun run GitHub n’est
nécessaire pour valider localement macOS. Les tests natifs Windows et Linux doivent
être exécutés sur les runners correspondants avant d’affirmer leur compatibilité.

## Distribution et retour arrière

Le paquet macOS local utilise une signature ad hoc sans service d’horodatage distant, avec les entitlements standard
Electron-builder. La notarisation Apple publique nécessite une identité de
publication dédiée. Le runtime téléchargé est nettoyé de ses métadonnées Finder
avant signature. Sur macOS local, la signature est effectuée dans un dossier
temporaire hors Documents pour empêcher la réintroduction de métadonnées par le
fournisseur de fichiers. Les DMG/ZIP finalisés sont recopiés dans `release/` ;
le chemin du bundle `.app` signé est affiché à la fin du build.

Pas de service de mise à jour automatique ajouté : la demande concerne le passage
à la dernière version stable d’Electron. Les prochaines versions se distribuent
par `.dmg`, `.exe`, `.deb` ou `.AppImage`.

Conserver l’ancienne application avant remplacement. Le retour à cette copie
réutilise le même fichier de configuration ; il ne nécessite aucune restauration
de base. Les sources Tauri sont conservées comme historique mais ne sont plus
compilées, et les dépendances Tauri sont retirées du frontend.

## Validation locale

Validé le 10 septembre 2026 :

- 241 tests Python passent, dont l’origine CORS Electron et les chemins du paquet Windows.
- 5 tests Node passent : chemins de configuration, collision de port, URL externes,
  confinement des ressources et CSP.
- TypeScript et export Next de production passent.
- `npm audit` : aucune vulnérabilité après mise à jour compatible de Browserslist
  et baseline-browser-mapping.
- Bundle macOS arm64 et DMG 0.2.0 construits ; signature ad hoc vérifiée par
  `codesign --verify --deep --strict`.
- Smoke test du paquet signé : Electron 44.3.0, version 0.2.0, preload disponible,
  Node absent du rendu, React affiché, health/bootstrap accessibles depuis Chromium,
  backend arrêté à la fermeture.
- Version installée dans `/Applications/SDK Local Manager.app` ; empreinte du
  fichier de configuration identique avant et après installation.
- Sauvegarde Tauri : `dist/backup-before-electron-20260910/SDK Local Manager Tauri.zip`.

Contrôle sur le workspace réel en attente de l’autorisation macOS Documents :
le journal TCC indique une ancienne exigence de signature non correspondante puis
`AUTHREQ_PROMPTING` pour `kTCCServiceSystemPolicyDocumentsFolder`. Le backend reste
bloqué dans `os.scandir` tant que la demande n’est pas acceptée. Ce comportement
n’a pas été constaté dans le workspace temporaire du smoke test, hors Documents.
Le contrôle automatisé de la fenêtre système UserNotificationCenter est interdit
par l’outil ; l’utilisateur doit accepter cette demande lui-même. Aucun réglage
TCC n’a été modifié ou contourné. Le contrôle visuel final des projets réels reste
à effectuer après cette autorisation.

Windows/Linux : workflow adapté, builds natifs non exécutés pendant cette session.
Aucun tag, push, run GitHub ni publication externe effectué.
