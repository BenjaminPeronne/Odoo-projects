#!/usr/bin/env python3
"""Construit Odoo Manager pour la plateforme sur laquelle le script s'exécute."""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
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


def local_macos_target_dir() -> Path | None:
    if platform.system() != "Darwin" or os.environ.get("GITHUB_ACTIONS"):
        return None
    if os.environ.get("CARGO_TARGET_DIR"):
        return Path(os.environ["CARGO_TARGET_DIR"])
    return Path(tempfile.gettempdir()) / "odoo-manager-tauri-target"


def clean_macos_attributes(*extra_paths: Path) -> None:
    if platform.system() != "Darwin" or not shutil.which("xattr"):
        return
    for path in (
        FRONTEND / "out",
        FRONTEND / "assets",
        FRONTEND / "public",
        FRONTEND / "src-tauri",
        FRONTEND / "src-tauri" / "icons",
        FRONTEND / "src-tauri" / "binaries",
        FRONTEND / "src-tauri" / "target" / "release" / "bundle",
        *extra_paths,
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

    env = os.environ.copy()
    env["CI"] = "true"

    cargo_target_dir = local_macos_target_dir()
    if cargo_target_dir:
        cargo_target_dir.mkdir(parents=True, exist_ok=True)
        env["CARGO_TARGET_DIR"] = str(cargo_target_dir)

    clean_macos_attributes(*(path for path in (cargo_target_dir,) if path))

    run(
        ["npm", "run", "tauri", "build", "--", "--bundles", bundles],
        cwd=FRONTEND,
        env=env,
    )
    bundle_dir = (
        (cargo_target_dir / "release" / "bundle")
        if cargo_target_dir
        else (FRONTEND / "src-tauri" / "target" / "release" / "bundle")
    )
    print(f"Paquets créés dans: {bundle_dir}")


if __name__ == "__main__":
    main()
