#!/usr/bin/env python3
"""Install the Windows NSIS package and verify the packaged desktop backend."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def request(url: str, *, method: str = "GET", timeout: float = 2.0) -> bytes:
    return urllib.request.urlopen(  # noqa: S310 - loopback smoke test only
        urllib.request.Request(url, method=method),
        timeout=timeout,
    ).read()


def backend_log() -> Path:
    local_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local_data / "com.sudokeys.odoo-manager" / "logs" / "backend.log"


def log_tail(path: Path, limit: int = 16_000) -> str:
    if not path.is_file():
        return f"Journal absent: {path}"
    return path.read_text(encoding="utf-8", errors="replace")[-limit:]


def stop_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    taskkill = shutil.which("taskkill")
    if taskkill:
        subprocess.run(
            [taskkill, "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        process.kill()


def wait_for_health(process: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    failure = "L'application installée n'a pas exposé son API locale."
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            failure = f"L'application installée s'est arrêtée (code {exit_code})."
            break
        try:
            payload = json.loads(request("http://127.0.0.1:8765/api/health"))
            if payload.get("ok") is True:
                return
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.25)
    raise RuntimeError(failure)


def shutdown_application(process: subprocess.Popen[bytes]) -> None:
    try:
        request("http://127.0.0.1:8765/api/system/shutdown", method="POST")
    except (OSError, urllib.error.URLError):
        pass
    stop_process_tree(process)


def main() -> None:
    if os.name != "nt":
        raise SystemExit("Ce test doit être exécuté sur Windows.")

    parser = argparse.ArgumentParser(description="Teste l'installateur NSIS Odoo Manager.")
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args()
    installer = args.installer.resolve()
    if not installer.is_file():
        raise SystemExit(f"Installateur introuvable: {installer}")

    with tempfile.TemporaryDirectory(prefix="odoo-manager-installed-") as temporary:
        root = Path(temporary)
        install_dir = root / "app"
        workspace = root / "workspace"
        config_dir = root / "config"
        workspace.mkdir()
        subprocess.run([str(installer), "/S", f"/D={install_dir}"], check=True, timeout=90)

        application = next(
            (
                candidate
                for candidate in install_dir.rglob("*.exe")
                if candidate.name.lower() in {"odoo manager.exe", "odoo-manager.exe"}
            ),
            None,
        )
        runtime = application.parent / "odoo-manager-backend-runtime" if application else None
        if application is None or runtime is None or not runtime.is_dir():
            contents = "\n".join(str(path.relative_to(install_dir)) for path in install_dir.rglob("*"))
            raise SystemExit(f"Installation Windows incomplète.\n{contents}")

        env = os.environ.copy()
        env.update(
            {
                "ODOO_WORKSPACE": str(workspace),
                "ODOO_MANAGER_CONFIG_DIR": str(config_dir),
            }
        )
        process = subprocess.Popen([str(application)], env=env)
        try:
            wait_for_health(process, args.timeout)
            print("Première installation Windows opérationnelle.")

            # Exercise the exact user workflow: update while the previous app
            # and its backend still own files in the installation directory.
            subprocess.run(
                [str(installer), "/S", f"/D={install_dir}"],
                check=True,
                timeout=90,
            )
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(
                    "La mise à niveau n'a pas arrêté l'ancienne application."
                ) from error
        except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise SystemExit(
                f"Échec du test de mise à niveau Windows: {error}\n"
                f"--- backend.log ---\n{log_tail(backend_log())}"
            ) from error
        finally:
            stop_process_tree(process)

        process = subprocess.Popen([str(application)], env=env)
        try:
            wait_for_health(process, args.timeout)
            print("Mise à niveau Windows opérationnelle: http://127.0.0.1:8765/api/health")
        except RuntimeError as error:
            raise SystemExit(f"{error}\n--- backend.log ---\n{log_tail(backend_log())}") from error
        finally:
            shutdown_application(process)


if __name__ == "__main__":
    main()
