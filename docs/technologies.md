# Technologies utilisées

Cette page décrit la stack **active** de SDK Local Manager 0.2.0. Les versions
JavaScript ci-dessous viennent de `odoo-manager-next/package.json` et les versions
de runtime correspondent à celles utilisées par le workflow de compilation.

## Application

| Technologie | Version | Utilité dans le projet |
| --- | --- | --- |
| Electron | 44.3.0 | Fournit l'application de bureau, la fenêtre native, les notifications, les boîtes de dialogue et le lancement du backend local. |
| Chromium | Embarqué avec Electron | Exécute et affiche l'interface web dans l'application de bureau. |
| Next.js | 16.3.4 | Structure l'interface, gère le build web et produit l'export statique embarqué dans Electron. |
| React / React DOM | 19.2.8 | Construit les écrans interactifs : projets, bases, modules, actions, logs et paramètres. |
| TypeScript | ^5.7.2 | Type le frontend et le contrat entre React et le preload Electron. |
| Python | 3.12 en CI | Porte l'API locale et toute la logique métier : projets Odoo, Docker, bases, modules, sauvegardes et tâches asynchrones. |
| Bibliothèque standard HTTP Python | Incluse avec Python | Sert l'API locale sur `127.0.0.1` sans framework web tiers. |

## Interface et design

| Technologie | Version | Utilité dans le projet |
| --- | --- | --- |
| Tailwind CSS | ^3.4.17 | Fournit les classes utilitaires et les règles de mise en page responsive. |
| Radix UI Themes | ^3.3.0 | Apporte les primitives d'interface accessibles utilisées par les composants. |
| Lucide React | ^0.468.0 | Fournit les icônes de l'interface. |
| next-themes | ^0.4.6 | Gère les thèmes clair et sombre. |
| clsx | ^2.1.1 | Compose les classes CSS conditionnelles. |
| tailwind-merge | ^2.6.0 | Fusionne les classes Tailwind sans conflits. |
| PostCSS | ^8.4.49 | Transforme la feuille de style pendant le build. |
| Autoprefixer | ^10.4.20 | Ajoute les préfixes CSS nécessaires à la compatibilité navigateur. |

## Desktop, packaging et sécurité

| Technologie | Version | Utilité dans le projet |
| --- | --- | --- |
| electron-builder | 26.15.3 | Produit les paquets macOS (`.dmg`), Windows (NSIS `.exe`) et Linux (`.deb`, `.AppImage`). |
| PyInstaller | Installé lors du build | Transforme le backend Python en exécutable natif embarqué avec l'application. |
| Preload Electron + IPC | API interne restreinte | Expose au frontend uniquement les fonctions natives autorisées, avec isolation du contexte et sans Node.js dans le rendu. |
| ASAR | Fourni par Electron | Regroupe les ressources JavaScript de l'application ; le backend natif reste une ressource externe. |

## Environnement Odoo et outils système

| Technologie | Utilité dans le projet |
| --- | --- |
| Docker Desktop / Docker Engine | Exécute les environnements locaux Odoo et PostgreSQL. |
| Docker Compose | Démarre, arrête et inspecte les services propres à chaque projet Odoo. |
| Odoo 15 à 19 | Versions de projets actuellement prises en charge par le gestionnaire. |
| PostgreSQL | Stocke les bases des instances Odoo ; le gestionnaire pilote les opérations via les conteneurs. |
| Traefik | Route les URL locales des projets Odoo et permet leur ouverture sans exposer directement leurs ports internes. |
| Git | Clone et met à jour les modèles Docker, Odoo Community, Enterprise et les dépôts d'addons. |
| SSH / GitLab | Authentifie l'accès aux dépôts privés Sudokeys sans conserver de mot de passe GitLab. |
| WSL 2 | Exécute sous Windows les opérations Linux nécessaires aux projets et à certains liens d'addons. |
| Windows Package Manager (`winget`) | Permet l'installation guidée de Git au premier lancement sous Windows. |

## Qualité et livraison

| Technologie | Version | Utilité dans le projet |
| --- | --- | --- |
| Node.js | 22 en CI | Exécute Next.js, TypeScript, les tests Electron et les outils de packaging. |
| npm | Fourni avec Node.js | Installe de façon reproductible les dépendances verrouillées par `package-lock.json`. |
| `unittest` Python | Inclus avec Python | Teste le backend et les comportements multiplateformes sans dépendance de test externe. |
| Node Test Runner | Inclus avec Node.js | Teste le runtime Electron et ses règles de sécurité. |
| GitHub Actions | Actions `checkout@v5`, `setup-node@v5`, `setup-python@v6`, `upload-artifact@v6` | Construit et contrôle nativement les installateurs macOS, Linux et Windows. |
| Tests de fumée Electron | Scripts internes | Vérifient le vrai paquet : démarrage du backend, rendu React, API, isolation de Node.js et arrêt propre. |

## Technologie historique

Tauri, Rust et WebKitGTK ne font plus partie du build actif. Le dossier
`odoo-manager-next/src-tauri/` est conservé uniquement comme référence de la
précédente architecture.
