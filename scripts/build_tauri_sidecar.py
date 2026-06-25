#!/usr/bin/env python3
import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TAURI_ROOT = ROOT / "odoo-manager-next" / "src-tauri"
BINARIES = TAURI_ROOT / "binaries"


def target_triple():
    rustc = shutil.which("rustc")
    if rustc:
        result = subprocess.run(
            [rustc, "--print", "host-tuple"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    system = platform.system()
    machine = platform.machine().lower()
    mapping = {
        ("Darwin", "arm64"): "aarch64-apple-darwin",
        ("Darwin", "aarch64"): "aarch64-apple-darwin",
        ("Darwin", "x86_64"): "x86_64-apple-darwin",
        ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
        ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
        ("Windows", "amd64"): "x86_64-pc-windows-msvc",
        ("Windows", "x86_64"): "x86_64-pc-windows-msvc",
        ("Windows", "arm64"): "aarch64-pc-windows-msvc",
    }
    try:
        return mapping[(system, machine)]
    except KeyError as exc:
        raise RuntimeError(f"Plateforme non prise en charge: {system} {machine}") from exc


def main():
    parser = argparse.ArgumentParser(description="Construit le sidecar Python pour Tauri.")
    parser.add_argument("--clean", action="store_true", help="Supprime les sorties PyInstaller avant construction.")
    args = parser.parse_args()

    build_root = ROOT / ".tauri-sidecar-build"
    os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(build_root / "pyinstaller-config"))

    try:
        import PyInstaller.__main__
    except ImportError as exc:
        raise SystemExit(
            "PyInstaller est requis. Lance plutot: "
            "sh scripts/build_local_desktop.sh"
        ) from exc

    if args.clean and build_root.exists():
        shutil.rmtree(build_root)
    build_root.mkdir(parents=True, exist_ok=True)
    BINARIES.mkdir(parents=True, exist_ok=True)

    extension = ".exe" if os.name == "nt" else ""
    base_name = f"odoo-manager-backend-{target_triple()}"
    output = BINARIES / f"{base_name}{extension}"
    data_separator = os.pathsep
    pyinstaller_args = [
        str(ROOT / "odoo_manager_web.py"),
        "--onefile",
        "--noconfirm",
        "--clean",
        "--name",
        base_name,
        "--distpath",
        str(BINARIES),
        "--workpath",
        str(build_root / "work"),
        "--specpath",
        str(build_root),
        "--add-data",
        f"{ROOT / 'odoo_manager.sh'}{data_separator}.",
        "--add-data",
        f"{ROOT / 'odoo_manager_core'}{data_separator}odoo_manager_core",
    ]
    if os.name == "nt":
        pyinstaller_args.append("--noconsole")

    PyInstaller.__main__.run(pyinstaller_args)
    print(f"Sidecar créé: {output}")


if __name__ == "__main__":
    main()
