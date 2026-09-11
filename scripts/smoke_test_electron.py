#!/usr/bin/env python3
"""Verify a real Electron renderer and its backend using an isolated workspace."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--application", type=Path)
    source.add_argument("--bundle-directory", type=Path)
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()
    application = args.application
    if args.bundle_directory:
        candidates = list(args.bundle_directory.glob("mac*/*.app/Contents/MacOS/SDK Local Manager"))
        candidates += list(args.bundle_directory.glob("linux*/odoo-manager-next"))
        if len(candidates) != 1:
            raise SystemExit(f"Précisez --application : {len(candidates)} applications trouvées.")
        application = candidates[0]
    application = application.resolve()
    if not application.is_file():
        raise SystemExit(f"Application introuvable: {application}")
    with tempfile.TemporaryDirectory(prefix="sdk-electron-smoke-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        report = root / "report.json"
        env = os.environ.copy()
        env.update({"ODOO_WORKSPACE": str(workspace), "ODOO_MANAGER_CONFIG_DIR": str(root / "config"),
                    "ODOO_MANAGER_LOG_DIR": str(root / "logs"), "ODOO_MANAGER_SMOKE_REPORT": str(report)})
        env.pop("ODOO_MANAGER_CONFIG", None)
        env.pop("ELECTRON_RUN_AS_NODE", None)
        try:
            result = subprocess.run([str(application)], env=env, capture_output=True, text=True, timeout=args.timeout)
        except subprocess.TimeoutExpired as error:
            raise SystemExit(f"L’application ne termine pas son smoke test après {args.timeout}s: {error.stderr}") from error
        payload = json.loads(report.read_text()) if report.exists() else {"ok": False, "error": "Rapport absent"}
        if result.returncode or not payload.get("ok"):
            raise SystemExit(json.dumps(payload, indent=2) + "\n" + result.stdout + "\n" + result.stderr)
        port = payload["renderer"]["health"]["port"]
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1):
                raise SystemExit("Le backend reste actif après la fermeture d’Electron.")
        except (OSError, urllib.error.URLError):
            pass
        print(json.dumps({**payload, "backendStopped": True}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
