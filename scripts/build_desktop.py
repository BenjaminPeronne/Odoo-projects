#!/usr/bin/env python3
"""Construit le backend Python et les paquets Electron de la plateforme courante."""
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


def run(command, *, cwd=ROOT):
    command = [shutil.which(command[0]) or command[0], *command[1:]]
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def default_bundles():
    return {"Darwin": "app,dmg", "Linux": "deb,appimage", "Windows": "nsis"}.get(platform.system(), "")


def builder_arguments(bundles, system=None):
    system = system or platform.system()
    allowed = {"Darwin": {"app", "dmg", "zip"}, "Linux": {"deb", "appimage"}, "Windows": {"nsis"}}
    targets = bundles.split(",")
    if not targets or any(target not in allowed.get(system, set()) for target in targets):
        raise ValueError(f"Formats non pris en charge sur {system}: {bundles}")
    if targets == ["app"]:
        return ["--dir"]
    targets = ["AppImage" if target == "appimage" else target for target in targets if target != "app"]
    return [{"Darwin": "--mac", "Linux": "--linux", "Windows": "--win"}[system], *targets]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundles", help="app,dmg ou zip sur macOS ; deb,appimage sur Linux ; nsis sur Windows")
    parser.add_argument("--no-clean", action="store_true", help="Conserver le dossier de travail PyInstaller")
    args = parser.parse_args()
    if not shutil.which("npm"):
        raise SystemExit("npm est requis.")
    try:
        targets = builder_arguments(args.bundles or default_bundles())
    except ValueError as error:
        raise SystemExit(str(error)) from error
    sidecar = [sys.executable, str(ROOT / "scripts/build_electron_sidecar.py")]
    if not args.no_clean:
        sidecar.append("--clean")
    run(sidecar)
    run([sys.executable, str(ROOT / "scripts/smoke_test_sidecar.py")])
    run(["npm", "run", "typecheck"], cwd=FRONTEND)
    run(["npm", "run", "test:desktop"], cwd=FRONTEND)
    run(["npm", "run", "build:desktop"], cwd=FRONTEND)
    if platform.system() == "Darwin" and shutil.which("xattr"):
        for directory in ("out", "electron"):
            run(["xattr", "-cr", str(FRONTEND / directory)])
    output = FRONTEND / "release"
    if platform.system() == "Darwin" and not os.environ.get("GITHUB_ACTIONS"):
        # File-provider metadata in Documents can reappear during codesign.
        # Sign outside that tree; only the sealed DMG/ZIP is copied back.
        output = Path(tempfile.mkdtemp(prefix="sdk-electron-package-"))
        targets.append(f"--config.directories.output={output}")
    run(["npm", "run", "desktop:dist", "--", *targets], cwd=FRONTEND)
    if output != FRONTEND / "release":
        (FRONTEND / "release").mkdir(exist_ok=True)
        for extension in ("*.dmg", "*.zip", "*.blockmap"):
            for artifact in output.glob(extension):
                shutil.copy2(artifact, FRONTEND / "release" / artifact.name)
    if os.name == "nt":
        installers = sorted((FRONTEND / "release").glob("*.exe"), key=lambda p: p.stat().st_mtime)
        if not installers:
            raise SystemExit("Installateur NSIS introuvable.")
        run([sys.executable, str(ROOT / "scripts/smoke_test_windows_installer.py"), "--installer", str(installers[-1])])
    print(f"Paquets Electron créés dans: {output}")


if __name__ == "__main__":
    main()
