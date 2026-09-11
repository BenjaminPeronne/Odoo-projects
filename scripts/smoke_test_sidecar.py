#!/usr/bin/env python3
"""Start the packaged backend and verify that its HTTP API is reachable."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from build_electron_sidecar import BINARIES


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def sidecar_path() -> Path:
    extension = ".exe" if os.name == "nt" else ""
    return BINARIES / f"odoo-manager-backend{extension}"


def request(url: str, *, method: str = "GET", timeout: float = 2.0) -> bytes:
    return urllib.request.urlopen(  # noqa: S310 - loopback smoke test only
        urllib.request.Request(url, method=method),
        timeout=timeout,
    ).read()


def tail(path: Path, limit: int = 12_000) -> str:
    if not path.exists():
        return "Journal backend absent."
    content = path.read_text(encoding="utf-8", errors="replace")
    return content[-limit:]


def main() -> None:
    parser = argparse.ArgumentParser(description="Teste le sidecar Odoo Manager construit.")
    parser.add_argument("--binary", type=Path, default=sidecar_path())
    parser.add_argument("--timeout", type=float, default=25.0)
    args = parser.parse_args()

    binary = args.binary.resolve()
    if not binary.is_file():
        raise SystemExit(f"Sidecar introuvable: {binary}")

    with tempfile.TemporaryDirectory(prefix="odoo-manager-smoke-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        log_dir = root / "logs"
        config_dir = root / "config"
        workspace.mkdir()
        port = available_port()
        env = os.environ.copy()
        env.update(
            {
                "ODOO_GUI_HOST": "127.0.0.1",
                "ODOO_GUI_PORT": str(port),
                "ODOO_WORKSPACE": str(workspace),
                "ODOO_MANAGER_CONFIG_DIR": str(config_dir),
                "ODOO_MANAGER_LOG_DIR": str(log_dir),
            }
        )
        process = subprocess.Popen(
            [str(binary)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        health_url = f"http://127.0.0.1:{port}/api/health"
        deadline = time.monotonic() + args.timeout
        failure = "Le sidecar n'a pas répondu avant le délai imparti."
        try:
            while time.monotonic() < deadline:
                exit_code = process.poll()
                if exit_code is not None:
                    failure = f"Le sidecar s'est arrêté prématurément (code {exit_code})."
                    break
                try:
                    payload = json.loads(request(health_url))
                    if payload.get("ok") is True:
                        print(f"Sidecar opérationnel: {health_url}")
                        return
                    failure = f"Réponse de santé invalide: {payload!r}"
                except (OSError, urllib.error.URLError, json.JSONDecodeError):
                    time.sleep(0.2)
        finally:
            try:
                request(f"http://127.0.0.1:{port}/api/system/shutdown", method="POST")
            except (OSError, urllib.error.URLError):
                pass
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()

        diagnostics = "\n".join(
            part
            for part in (
                failure,
                "--- stdout ---\n" + (stdout or "(vide)"),
                "--- stderr ---\n" + (stderr or "(vide)"),
                "--- backend.log ---\n" + tail(log_dir / "backend.log"),
            )
            if part
        )
        raise SystemExit(diagnostics)


if __name__ == "__main__":
    main()
