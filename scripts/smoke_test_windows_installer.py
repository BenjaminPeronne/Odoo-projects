#!/usr/bin/env python3
"""Install the Windows NSIS package and verify the packaged desktop backend."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


TAURI_WINDOWS_ORIGIN = "http://tauri.localhost"
PACKAGED_PROCESS_NAMES = (
    "Odoo Manager.exe",
    "odoo-manager.exe",
    "odoo-manager-backend.exe",
)


def request(
    url: str,
    *,
    method: str = "GET",
    timeout: float = 2.0,
    origin: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    headers = {"Origin": origin} if origin else {}
    if method == "OPTIONS":
        headers.update(
            {
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "content-type",
            }
        )
    with urllib.request.urlopen(  # noqa: S310 - loopback smoke test only
        urllib.request.Request(url, method=method, headers=headers),
        timeout=timeout,
    ) as response:
        response_headers = {key.lower(): value for key, value in response.headers.items()}
        return response.read(), response_headers


def backend_log() -> Path:
    local_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local_data / "com.sudokeys.odoo-manager" / "logs" / "backend.log"


def log_tail(path: Path, limit: int = 16_000) -> str:
    if not path.is_file():
        return f"Journal absent: {path}"
    return path.read_text(encoding="utf-8", errors="replace")[-limit:]


def stop_process_tree(process: subprocess.Popen[bytes]) -> None:
    taskkill = shutil.which("taskkill")
    if taskkill and process.poll() is None:
        subprocess.run(
            [taskkill, "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    elif process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def stop_packaged_processes() -> None:
    taskkill = shutil.which("taskkill")
    if not taskkill:
        return
    for image_name in PACKAGED_PROCESS_NAMES:
        subprocess.run(
            [taskkill, "/F", "/T", "/IM", image_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def wait_for_backend_shutdown(timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            request("http://127.0.0.1:8765/api/health", timeout=0.5)
        except (OSError, urllib.error.URLError):
            return
        time.sleep(0.2)
    raise RuntimeError("Le backend Windows reste actif après la fermeture de l'application.")


def remove_tree_with_retry(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while path.exists():
        try:
            shutil.rmtree(path)
            return
        except OSError as error:
            last_error = error
            if time.monotonic() >= deadline:
                break
            stop_packaged_processes()
            time.sleep(0.5)
    if path.exists():
        raise RuntimeError(
            f"Le dossier d'installation temporaire reste verrouillé: {path} ({last_error})"
        ) from last_error


@contextmanager
def temporary_install_directory():
    root = Path(tempfile.mkdtemp(prefix="odoo-manager-installed-"))
    try:
        yield root
    finally:
        remove_tree_with_retry(root)


def wait_for_health(process: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    failure = "L'application installée n'a pas exposé son API locale."
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            failure = f"L'application installée s'est arrêtée (code {exit_code})."
            break
        try:
            body, _headers = request("http://127.0.0.1:8765/api/health")
            payload = json.loads(body)
            if payload.get("ok") is True:
                _preflight_body, preflight_headers = request(
                    "http://127.0.0.1:8765/api/bootstrap",
                    method="OPTIONS",
                    origin=TAURI_WINDOWS_ORIGIN,
                )
                bootstrap_body, bootstrap_headers = request(
                    "http://127.0.0.1:8765/api/bootstrap",
                    timeout=12.0,
                    origin=TAURI_WINDOWS_ORIGIN,
                )
                bootstrap = json.loads(bootstrap_body)
                cors_origin = bootstrap_headers.get("access-control-allow-origin", "")
                preflight_origin = preflight_headers.get("access-control-allow-origin", "")
                allowed_headers = preflight_headers.get("access-control-allow-headers", "").lower()
                if (
                    cors_origin != TAURI_WINDOWS_ORIGIN
                    or preflight_origin != TAURI_WINDOWS_ORIGIN
                    or "content-type" not in allowed_headers
                ):
                    failure = (
                        "L'API ne permet pas les appels de la WebView Windows "
                        "(prévalidation CORS incomplète)."
                    )
                    break
                if {"overview", "system_status", "settings", "jobs"} <= bootstrap.keys():
                    return
                failure = f"Réponse bootstrap incomplète: {sorted(bootstrap.keys())}"
                break
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.25)
    raise RuntimeError(failure)


def shutdown_application(process: subprocess.Popen[bytes]) -> None:
    try:
        request("http://127.0.0.1:8765/api/system/shutdown", method="POST")
    except (OSError, urllib.error.URLError):
        pass
    stop_process_tree(process)
    stop_packaged_processes()
    wait_for_backend_shutdown()


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

    with temporary_install_directory() as root:
        install_dir = root / "app"
        # Reproduce a real first launch: the configured workspace may not exist yet.
        workspace = root / "workspace"
        config_dir = root / "config"
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
