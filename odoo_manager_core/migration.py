"""Migration d'un projet depuis un disque Windows vers l'environnement Linux.

Un projet servi depuis `C:\\` traverse le pont 9P de Docker Desktop : Odoo met 48 à
95 s à démarrer, contre 4 à 6 s sur le système de fichiers de la distribution. La
migration copie le projet tel quel, bases et filestore compris, et ne touche jamais
à l'original : il reste sur `C:\\` tant que l'utilisateur ne l'a pas supprimé lui-même.

Le projet doit être arrêté : copier `postgresql_data` pendant que PostgreSQL écrit
donnerait une base incohérente.

`postgresql_data` appartient à l'utilisateur PostgreSQL du conteneur (uid 999, droits
0700) : le backend, qui tourne sous l'utilisateur du poste, ne peut ni y lire le verrou
ni le copier. Dans ce cas, les lectures et la copie passent par `sudo -n`, et `cp -a`
conserve propriétaires et droits, faute de quoi PostgreSQL refuserait de démarrer.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
POSTGRES_DATA_DIRECTORY = "postgresql_data"
POSTMASTER_FILE = "postmaster.pid"
MIGRATION_MARKER = ".odoo_manager_migrated_from"
# Le projet d'origine reste intact : ces dossiers sont recopiés à l'identique.
PROGRESS_EVERY_FILES = 500


def is_project_directory(path):
    path = Path(path)
    try:
        return path.is_dir() and any((path / name).exists() for name in COMPOSE_NAMES)
    except OSError:
        return False


_PRIVILEGE_CACHE = {}


def privileged_prefix(run=subprocess.run, which=shutil.which):
    """`sudo -n` si le backend n'est pas root et peut l'utiliser sans mot de passe, sinon []."""
    if os.name != "posix" or os.geteuid() == 0:
        return []
    if "prefix" not in _PRIVILEGE_CACHE:
        sudo = which("sudo")
        allowed = False
        if sudo:
            try:
                allowed = run([sudo, "-n", "true"], capture_output=True, timeout=10).returncode == 0
            except (OSError, subprocess.SubprocessError):
                allowed = False
        _PRIVILEGE_CACHE["prefix"] = [sudo, "-n"] if allowed else []
    return list(_PRIVILEGE_CACHE["prefix"])


def project_is_stopped(path, prefix=None, run=subprocess.run):
    """Faux tant que PostgreSQL tourne : son fichier de verrou est présent.

    Un verrou illisible sans droits suffisants compte comme « en cours » : mieux vaut
    refuser une migration que copier une base ouverte.
    """
    lock = Path(path) / POSTGRES_DATA_DIRECTORY / POSTMASTER_FILE
    try:
        return not lock.exists()
    except PermissionError:
        if not prefix:
            return False
    script = 'if test -e "$1"; then echo running; else echo stopped; fi'
    try:
        result = run([*prefix, "sh", "-c", script, "lock", str(lock)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "stopped"


def migration_candidates(source_root, destination_root, prefix=None, run=subprocess.run):
    """Projets présents côté Windows et absents de l'environnement Linux."""
    source_root = Path(source_root)
    destination_root = Path(destination_root)
    candidates = []
    try:
        entries = sorted(source_root.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return candidates
    for entry in entries:
        if entry.name.startswith(".") or not is_project_directory(entry):
            continue
        candidates.append({
            "name": entry.name,
            "source": str(entry),
            "already_migrated": (destination_root / entry.name).exists(),
            "stopped": project_is_stopped(entry, prefix, run),
        })
    return candidates


def list_tree(root, prefix, run=subprocess.run):
    """Entrées d'une arborescence lue avec droits : chemin relatif -> (type, taille, cible)."""
    command = [*prefix, "find", str(root), "-mindepth", "1", "-printf", "%P\\t%y\\t%s\\t%l\\n"]
    result = run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
    if result.returncode != 0:
        raise RuntimeError(f"Lecture de {root} impossible : {(result.stderr or '').strip()}")
    entries = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        relative, kind, size, target = parts
        entries[relative] = (kind, int(size) if size.isdigit() else 0, target)
    return entries


def measure_project(path, prefix=None, run=subprocess.run, listing=None):
    """Nombre de fichiers et octets à copier, liens non suivis.

    `listing` réutilise une liste déjà lue avec list_tree : à travers /mnt/c, la lire coûte
    près de deux minutes pour un projet Enterprise.
    """
    if prefix or listing is not None:
        entries = listing if listing is not None else list_tree(path, prefix, run)
        files = [entry for entry in entries.values() if entry[0] != "d"]
        return {"files": len(files), "bytes": sum(size for kind, size, _target in files if kind == "f")}
    files = 0
    total_bytes = 0
    for directory, _subdirectories, names in os.walk(path, followlinks=False):
        for name in names:
            entry = Path(directory) / name
            files += 1
            try:
                if not entry.is_symlink():
                    total_bytes += entry.stat().st_size
            except OSError:
                continue
    return {"files": files, "bytes": total_bytes}


def copy_project_privileged(source, destination, prefix, log=None, progress=None, total_files=0,
                            popen=subprocess.Popen, run=subprocess.run, poll_seconds=5):
    """Copie en flux `tar` sous sudo : propriétaires numériques, droits et liens conservés.

    `cp -a` relit les attributs étendus de chaque fichier à travers /mnt/c : 20,9 s pour
    479 fichiers, contre 3,8 s en flux tar (relevé sous WSL). PostgreSQL n'a besoin que du
    propriétaire (uid 999) et des droits (0700), que tar conserve. La progression est
    estimée en comptant les entrées déjà copiées.
    """
    source = Path(source)
    destination = Path(destination)
    if destination.exists():
        raise ValueError(f"Un projet nommé {destination.name} existe déjà dans l'environnement Linux.")
    log = log or (lambda _message: None)
    # bash pour pipefail : sans lui, un tar de lecture en échec passerait inaperçu.
    script = (
        'set -euo pipefail; mkdir -p "$2"; '
        'tar -C "$1" --numeric-owner -cf - . | tar -C "$2" --numeric-owner -xpf -'
    )
    process = popen([*prefix, "bash", "-c", script, "copy", str(source), str(destination)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    while True:
        try:
            _stdout, stderr = process.communicate(timeout=poll_seconds)
            break
        except subprocess.TimeoutExpired:
            counted = run([*prefix, "sh", "-c", 'find "$1" -mindepth 1 ! -type d | wc -l', "count", str(destination)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            copied = int(counted.stdout.strip() or 0) if counted.returncode == 0 else 0
            log(f"Copie : {copied}/{total_files} entrées" if total_files else f"Copie : {copied} entrées")
            if progress:
                progress(copied, total_files)
    if process.returncode != 0:
        raise RuntimeError(f"La copie a échoué : {(stderr or '').strip()[-400:]}")
    marker = run([*prefix, "sh", "-c", 'printf "%s\\n" "$1" > "$2"', "marker", str(source), str(destination / MIGRATION_MARKER)],
                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if marker.returncode != 0:
        raise RuntimeError("Copie terminée mais marqueur de migration non écrit.")
    log("Copie terminée.")
    return {"files": total_files}


def copy_project(source, destination, log=None, progress=None, total_files=0):
    """Copie le projet, liens d'addons compris, sans suivre les liens.

    Les liens de `odoo/addons` sont relatifs : recopiés tels quels, ils désignent la
    même cible dans la copie. Les suivre dupliquerait chaque module.

    `shutil.copytree(symlinks=True)` ne convient pas : sous Windows, il recrée un lien
    de dossier comme lien de fichier, que ni Windows ni Docker ne peuvent parcourir.
    """
    source = Path(source)
    destination = Path(destination)
    if destination.exists():
        raise ValueError(f"Un projet nommé {destination.name} existe déjà dans l'environnement Linux.")
    log = log or (lambda _message: None)
    copied = 0
    last_report = time.monotonic()

    def count_one():
        nonlocal copied, last_report
        copied += 1
        if copied % PROGRESS_EVERY_FILES == 0 and time.monotonic() - last_report > 1:
            last_report = time.monotonic()
            log(f"Copie : {copied}/{total_files} fichiers" if total_files else f"Copie : {copied} fichiers")
            if progress:
                progress(copied, total_files)

    destination.mkdir(parents=True)
    for directory, subdirectories, names in os.walk(source, followlinks=False):
        current = Path(directory)
        target_directory = destination / current.relative_to(source)
        for name in list(subdirectories):
            link = current / name
            if not link.is_symlink():
                (target_directory / name).mkdir(exist_ok=True)
                continue
            # Lien vers un dossier : le type doit être conservé pour rester parcourable.
            subdirectories.remove(name)
            os.symlink(os.readlink(link), target_directory / name, target_is_directory=True)
            count_one()
        for name in names:
            entry = current / name
            target = target_directory / name
            if entry.is_symlink():
                os.symlink(os.readlink(entry), target)
            else:
                shutil.copy2(entry, target, follow_symlinks=False)
            count_one()
        shutil.copystat(current, target_directory)
    (destination / MIGRATION_MARKER).write_text(str(source) + "\n", encoding="utf-8")
    log(f"Copie terminée : {copied} fichiers.")
    return {"files": copied}


def compare_projects(source, destination, prefix=None, run=subprocess.run, source_listing=None):
    """Contrôle d'après-copie : mêmes fichiers, mêmes cibles de liens."""
    if prefix:
        listing = source_listing if source_listing is not None else list_tree(source, prefix, run)
        source_entries = _listing_entries(listing)
        destination_entries = _listing_entries(list_tree(destination, prefix, run))
    else:
        source_entries = _relative_entries(source)
        destination_entries = _relative_entries(destination)
    destination_entries.pop(MIGRATION_MARKER, None)
    missing = sorted(set(source_entries) - set(destination_entries))
    different_links = sorted(
        name for name, target in source_entries.items()
        if target is not None and destination_entries.get(name) != target
    )
    return {
        "source_files": len(source_entries),
        "copied_files": len(destination_entries),
        "missing": missing[:20],
        "missing_count": len(missing),
        "different_links": different_links[:20],
        "different_links_count": len(different_links),
        "identical": not missing and not different_links,
    }


def _listing_entries(listing):
    """Même forme que _relative_entries, depuis list_tree : fichiers et liens, sans les dossiers."""
    return {
        relative: (target.replace("\\", "/") if kind == "l" else None)
        for relative, (kind, _size, target) in listing.items()
        if kind != "d"
    }


def _relative_entries(root):
    """Chemin relatif -> cible du lien, ou None pour un fichier ordinaire."""
    root = Path(root)
    entries = {}
    for directory, subdirectories, names in os.walk(root, followlinks=False):
        current = Path(directory)
        # Les liens vers un dossier ne sont pas parcourus : leur cible est comparée telle quelle.
        for name in list(subdirectories):
            link = current / name
            if link.is_symlink():
                subdirectories.remove(name)
                entries[str(link.relative_to(root)).replace("\\", "/")] = os.readlink(link).replace("\\", "/")
        for name in names:
            entry = current / name
            key = str(entry.relative_to(root)).replace("\\", "/")
            entries[key] = os.readlink(entry).replace("\\", "/") if entry.is_symlink() else None
    return entries
