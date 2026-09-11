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
WINDOWS_RUNTIME_NAME = "odoo-manager-backend-runtime"



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
    windows_runtime = BINARIES / WINDOWS_RUNTIME_NAME
    if output.exists():
        output.unlink()
    if windows_runtime.exists():
        shutil.rmtree(windows_runtime)
    data_separator = os.pathsep
    pyinstaller_args = [
        str(ROOT / "odoo_manager_web.py"),
        "--onedir" if is_windows else "--onefile",
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
    ]
    if is_windows:
        pyinstaller_args.extend(["--noconsole", "--contents-directory", WINDOWS_RUNTIME_NAME])

    PyInstaller.__main__.run(pyinstaller_args)
    if is_windows:
        onedir_output = BINARIES / base_name
        built_executable = onedir_output / f"{base_name}.exe"
        built_runtime = onedir_output / WINDOWS_RUNTIME_NAME
        if not built_executable.is_file() or not built_runtime.is_dir():
            raise SystemExit(f"Sortie PyInstaller Windows incomplète: {onedir_output}")
        shutil.copy2(built_executable, output)
        shutil.copytree(built_runtime, windows_runtime)
        shutil.rmtree(onedir_output)
    print(f"Sidecar créé: {output}")


if __name__ == "__main__":
    main()
