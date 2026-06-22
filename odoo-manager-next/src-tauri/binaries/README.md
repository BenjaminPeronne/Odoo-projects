# Sidecar backend

Ce dossier reçoit le backend Python produit par `scripts/build_tauri_sidecar.py`.

Tauri exige un fichier suffixé par le triple de compilation, par exemple :

- `odoo-manager-backend-aarch64-apple-darwin`
- `odoo-manager-backend-x86_64-unknown-linux-gnu`
- `odoo-manager-backend-x86_64-pc-windows-msvc.exe`

Le binaire doit être construit sur chaque système cible. Il n’est pas versionné.
