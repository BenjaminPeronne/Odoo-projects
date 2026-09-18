#!/usr/bin/env python3
import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESKTOP_ROOT = ROOT / "odoo-manager-next" / "electron"
BINARIES = DESKTOP_ROOT / "binaries"
# Dossier du runtime Python, à côté de l'exécutable. En --onefile, le bootloader réextrait
# l'archive (17 Mo) dans un dossier temporaire à chaque lancement : 5,3 s sur macOS contre
# 0,13 s en --onedir, sur le chemin critique de la première fenêtre.
RUNTIME_NAME = "odoo-manager-backend-runtime"



def main():
    parser = argparse.ArgumentParser(description="Construit le sidecar Python pour Electron.")
    parser.add_argument("--clean", action="store_true", help="Supprime les sorties PyInstaller avant construction.")
    args = parser.parse_args()

    build_root = ROOT / ".electron-sidecar-build"
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

    is_windows = os.name == "nt"
    extension = ".exe" if is_windows else ""
    base_name = "odoo-manager-backend"
    output = BINARIES / f"{base_name}{extension}"
    runtime = BINARIES / RUNTIME_NAME
    if output.exists():
        output.unlink()
    if runtime.exists():
        shutil.rmtree(runtime)
    data_separator = os.pathsep
    pyinstaller_args = [
        str(ROOT / "odoo_manager_web.py"),
        "--onedir",
        "--contents-directory",
        RUNTIME_NAME,
        "--noconfirm",
        "--clean",
        "--name",
        base_name,
        "--distpath",
        # Hors de binaries/ : sans extension .exe, le dossier onedir porterait le nom de
        # l'exécutable final et l'écraserait au moment de le remonter.
        str(build_root / "dist"),
        "--workpath",
        str(build_root / "work"),
        "--specpath",
        str(build_root),
        "--add-data",
        f"{ROOT / 'odoo_manager.sh'}{data_separator}.",
    ]
    if is_windows:
        pyinstaller_args.append("--noconsole")

    PyInstaller.__main__.run(pyinstaller_args)
    # L'exécutable et son dossier de runtime sont remontés côte à côte dans binaries/, qui devient
    # Resources/backend/ : le chemin du sidecar reste le même pour Electron sur les trois systèmes.
    onedir_output = build_root / "dist" / base_name
    built_executable = onedir_output / f"{base_name}{extension}"
    built_runtime = onedir_output / RUNTIME_NAME
    if not built_executable.is_file() or not built_runtime.is_dir():
        raise SystemExit(f"Sortie PyInstaller incomplète: {onedir_output}")
    shutil.copy2(built_executable, output)
    shutil.copytree(built_runtime, runtime, symlinks=True)
    shutil.rmtree(onedir_output)
    print(f"Sidecar créé: {output} (runtime: {runtime})")


if __name__ == "__main__":
    main()
