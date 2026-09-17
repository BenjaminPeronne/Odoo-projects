#!/usr/bin/env python3
"""Construit l'image de la distribution WSL « SDK-Manager » (fichier .wsl).

`docker build` prépare la racine du système, `docker export` en produit l'archive tar
que Windows importe avec `wsl --install --from-file`. L'empreinte SHA-256 accompagne
l'archive : l'application refuse d'importer une image qui ne correspond pas.
"""
import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WSL_DIRECTORY = ROOT / "wsl"
DEFAULT_OUTPUT_DIRECTORY = ROOT / "dist" / "wsl"
IMAGE_REPOSITORY = "sdk-manager-rootfs"
REQUIRED_FILES = ("Dockerfile", "wsl.conf", "daemon.json", "provision.sh", "known_hosts")


def application_version(frontend=None):
    manifest = (frontend or ROOT / "odoo-manager-next") / "package.json"
    return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])


def image_tag(version):
    return f"{IMAGE_REPOSITORY}:{version}"


def image_file_name(version):
    return f"sdk-manager-{version}.wsl"


def checksum_file_name(version):
    return image_file_name(version) + ".sha256"


def sha256_of(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def missing_sources(directory=None):
    directory = Path(directory or WSL_DIRECTORY)
    return [name for name in REQUIRED_FILES if not (directory / name).is_file()]


def run(command):
    print("+", " ".join(str(part) for part in command), flush=True)
    subprocess.run([str(part) for part in command], check=True)


def export_container_filesystem(tag, destination):
    """Archive la racine construite. Le conteneur ne sert qu'à l'export."""
    container = f"sdk-manager-export-{uuid.uuid4().hex[:8]}"
    run(["docker", "create", "--name", container, tag])
    try:
        with open(destination, "wb") as archive:
            print(f"+ docker export {container} > {destination}", flush=True)
            subprocess.run(["docker", "export", container], stdout=archive, check=True)
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False, stdout=subprocess.DEVNULL)


def build(version, output_directory=None, keep_image=False):
    absent = missing_sources()
    if absent:
        raise SystemExit("Fichiers manquants dans wsl/: " + ", ".join(absent))
    if not shutil.which("docker"):
        raise SystemExit("Docker est requis pour construire l'image.")

    output_directory = Path(output_directory or DEFAULT_OUTPUT_DIRECTORY)
    output_directory.mkdir(parents=True, exist_ok=True)
    tag = image_tag(version)
    # L'image doit être amd64 même construite depuis un Mac ARM : WSL 2 n'exécute que x86-64.
    run([
        "docker", "build",
        "--platform", "linux/amd64",
        "--build-arg", f"SDK_MANAGER_VERSION={version}",
        "-t", tag,
        str(WSL_DIRECTORY),
    ])
    archive = output_directory / image_file_name(version)
    export_container_filesystem(tag, archive)
    digest = sha256_of(archive)
    (output_directory / checksum_file_name(version)).write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8"
    )
    if not keep_image:
        subprocess.run(["docker", "image", "rm", tag], check=False, stdout=subprocess.DEVNULL)
    size_mb = archive.stat().st_size / (1024 * 1024)
    print(f"Image WSL: {archive} ({size_mb:.0f} Mo)")
    print(f"SHA-256: {digest}")
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="Version de l'application (par défaut celle de package.json)")
    parser.add_argument("--output", help="Dossier de sortie (par défaut dist/wsl)")
    parser.add_argument("--keep-image", action="store_true", help="Conserver l'image Docker intermédiaire")
    arguments = parser.parse_args()
    if platform.system() == "Windows" and not shutil.which("docker"):
        raise SystemExit("Docker Desktop doit être démarré.")
    build(arguments.version or application_version(), arguments.output, arguments.keep_image)
    return 0


if __name__ == "__main__":
    sys.exit(main())
