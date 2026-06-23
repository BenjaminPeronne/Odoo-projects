#!/usr/bin/env python3
"""Construit Odoo Manager pour la plateforme sur laquelle le script s'exécute."""

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "odoo-manager-next"


def require(command: str) -> None:
    if not shutil.which(command):
        raise SystemExit(f"Commande requise introuvable: {command}")


def default_bundles() -> str:
    return {
        "Darwin": "app,dmg",
        "Linux": "deb,appimage",
        "Windows": "nsis",
    }.get(platform.system(), "")


def clean_macos_attributes() -> None:
    if platform.system() != "Darwin" or not shutil.which("xattr"):
        return
    for path in (
        FRONTEND / "assets",
        FRONTEND / "src-tauri" / "icons",
        FRONTEND / "src-tauri" / "binaries",
        FRONTEND / "src-tauri" / "target" / "release" / "bundle",
    ):
        if path.exists():
            subprocess.run(["xattr", "-cr", str(path)], check=False)


def run(command: list[str], *, cwd: Path = ROOT, env=None) -> None:
    resolved = command.copy()
    executable = shutil.which(resolved[0])
    if executable:
        resolved[0] = executable
    print("+", " ".join(command), flush=True)
    subprocess.run(resolved, cwd=cwd, env=env, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Construit le sidecar et l'installateur natifs d'Odoo Manager."
    )
    parser.add_argument(
        "--bundles",
        help="Formats Tauri, par exemple app,dmg, deb,appimage ou nsis.",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Conserve les sorties PyInstaller précédentes.",
    )
    args = parser.parse_args()

    bundles = args.bundles or default_bundles()
    if not bundles:
        raise SystemExit(f"Plateforme non prise en charge: {platform.system()}")

    require("npm")
    require("cargo")

    sidecar_command = [sys.executable, str(ROOT / "scripts" / "build_tauri_sidecar.py")]
    if not args.no_clean:
        sidecar_command.append("--clean")
    run(sidecar_command)

    clean_macos_attributes()
    env = os.environ.copy()
    env["CI"] = "true"

    run(
        ["npm", "run", "tauri", "build", "--", "--bundles", bundles],
        cwd=FRONTEND,
        env=env,
    )
    print(f"Paquets créés dans: {FRONTEND / 'src-tauri' / 'target' / 'release' / 'bundle'}")


if __name__ == "__main__":
    main()
