# Signature des versions Odoo Manager

Une licence logicielle (`MIT`, propriétaire, etc.) définit les droits
d'utilisation du code. Elle ne supprime pas les alertes de macOS ou Windows.
Pour cela, les installateurs doivent être signés avec une identité vérifiée.

Le workflow `.github/workflows/build-desktop.yml` prend en charge :

- macOS : signature Developer ID, hardened runtime, notarisation et contrôle du
  ticket attaché au DMG ;
- Windows : import d'un certificat Authenticode PFX, signature Tauri et
  vérification de l'installateur NSIS ;
- Linux : les paquets restent non signés pour le moment.

Les certificats et mots de passe ne doivent jamais être ajoutés au dépôt.

## État du dépôt GitHub

Contrôler les paramètres sans afficher les valeurs secrètes :

```sh
gh auth status
gh repo view --json nameWithOwner,isPrivate,viewerPermission
gh secret list
gh variable list
```

## macOS

### Prérequis

1. Adhérer à l'Apple Developer Program.
2. Créer un certificat `Developer ID Application` depuis le compte Apple.
3. Installer le certificat avec sa clé privée dans le trousseau macOS.
4. Exporter l'identité en fichier `.p12` protégé par un mot de passe.
5. Créer un mot de passe spécifique à l'application pour l'identifiant Apple.

Encoder le certificat sans retour à la ligne :

```sh
openssl base64 -A -in /chemin/DeveloperIDApplication.p12 -out /tmp/apple-certificate.txt
```

Ajouter les secrets au dépôt. `gh secret set` demande la valeur de manière
interactive lorsqu'aucune valeur n'est passée sur la ligne de commande :

```sh
gh secret set APPLE_CERTIFICATE < /tmp/apple-certificate.txt
gh secret set APPLE_CERTIFICATE_PASSWORD
gh secret set APPLE_ID
gh secret set APPLE_PASSWORD
gh secret set APPLE_TEAM_ID
```

Supprimer ensuite les copies temporaires du certificat encodé. Conserver le
`.p12` original dans un coffre-fort sécurisé, jamais dans Git.

## Windows

### Option A : certificat Authenticode PFX

Acheter un certificat de signature de code auprès d'une autorité reconnue et
obtenir un fichier `.pfx` exportable avec sa clé privée. Certains certificats
récents utilisent uniquement un jeton matériel ou un service cloud ; dans ce
cas, utiliser la procédure du fournisseur ou l'option Azure ci-dessous.

Encoder le PFX :

```sh
openssl base64 -A -in /chemin/odoo-manager-code-signing.pfx -out /tmp/windows-certificate.txt
gh secret set WINDOWS_CERTIFICATE < /tmp/windows-certificate.txt
gh secret set WINDOWS_CERTIFICATE_PASSWORD
```

Le serveur d'horodatage DigiCert est utilisé par défaut. Pour utiliser celui du
fournisseur du certificat :

```sh
gh variable set WINDOWS_TIMESTAMP_URL --body "https://url-du-fournisseur"
```

### Option B : Azure Artifact Signing

Azure Artifact Signing évite de déposer un PFX exportable dans GitHub. Cette
option nécessite un compte Azure Artifact Signing, un profil de certificat et
une configuration OIDC ou d'identifiants Azure. Le workflow actuel utilise le
mode PFX ; son passage à Azure doit être fait avec les informations du compte,
du profil et de la région Azure retenus.

## Rendre la signature obligatoire

Tant que les certificats ne sont pas configurés, le workflow produit encore des
builds de test non signés et inscrit un avertissement dans le résumé GitHub
Actions. Après avoir ajouté les secrets Apple et Windows, activer le garde-fou :

```sh
gh variable set RELEASE_SIGNING_REQUIRED --body true
```

À partir de ce moment, un secret absent ou incomplet fait échouer le build au
lieu de publier silencieusement un installateur non signé.

Vérifier la configuration :

```sh
gh secret list
gh variable list
```

## Compiler

Committer et pousser la configuration avant de déclencher le build :

```sh
git add -A
git commit -m "Add release code signing pipeline"
git push origin main
sh scripts/build_all_platforms.sh
```

Le script crée automatiquement le prochain tag de build pour la version
applicative courante.
