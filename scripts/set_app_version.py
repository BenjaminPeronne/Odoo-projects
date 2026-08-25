#!/usr/bin/env python3
"""Synchronise la version publique d'Odoo Manager dans tous ses manifestes."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def write_json_version(path: Path, version: str, *, package_lock: bool = False) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = version
    if package_lock:
        data.setdefault("packages", {}).setdefault("", {})["version"] = version
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def replace_once(path: Path, pattern: str, replacement: str) -> None:
    content = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, content, count=1, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"Version introuvable ou ambiguë dans {path}")
    path.write_text(updated, encoding="utf-8")


def set_app_version(root: Path, version: str) -> None:
    if not SEMVER_RE.fullmatch(version):
        raise ValueError("La version doit respecter le format MAJEUR.MINEUR.CORRECTIF, par exemple 0.1.2")

    frontend = root / "odoo-manager-next"
    tauri = frontend / "src-tauri"
    write_json_version(frontend / "package.json", version)
    write_json_version(frontend / "package-lock.json", version, package_lock=True)
    write_json_version(tauri / "tauri.conf.json", version)
    replace_once(
        tauri / "Cargo.toml",
        r'(^\[package\]\n(?:.*\n)*?version = ")[^"]+("$)',
        rf"\g<1>{version}\g<2>",
    )
    replace_once(
        tauri / "Cargo.lock",
        r'(^\[\[package\]\]\nname = "odoo-manager"\nversion = ")[^"]+("$)',
        rf"\g<1>{version}\g<2>",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="Nouvelle version, par exemple 0.1.2")
    args = parser.parse_args()
    set_app_version(ROOT, args.version)
    print(f"Version Odoo Manager synchronisée: {args.version}")
    print("Committe ces manifestes avant de lancer scripts/build_all_platforms.sh.")


if __name__ == "__main__":
    main()
