"""Liens d'addons sous Windows : liens hérités de WSL et liens natifs.

Sans le mode développeur, Windows refuse les liens symboliques natifs et les
anciennes versions créaient alors les liens de odoo/addons avec `ln -s` dans WSL.
Docker les suit, mais Windows ne peut pas les ouvrir (WinError 1920) : chaque
lecture de module devait repasser par WSL, soit 30 à 70 s pour un projet
Enterprise. Un lien natif relatif est lu par Windows et par Docker Desktop.
"""

import json
import os
import time
import uuid
from pathlib import Path

from .platform import platform_id


IO_REPARSE_TAG_SYMLINK = 0xA000000C
IO_REPARSE_TAG_LX_SYMLINK = 0xA000001D
FSCTL_GET_REPARSE_POINT = 0x000900A8
MIGRATION_JOURNAL_NAME = ".odoo_manager_link_migration.json"

# Seul le succès est mémorisé : le mode développeur peut être activé à tout moment.
NATIVE_SYMLINK_SUPPORT = {}
NATIVE_SYMLINK_SUPPORT_TTL_SECONDS = 300


def reparse_tag(path):
    try:
        return getattr(os.lstat(path), "st_reparse_tag", 0)
    except OSError:
        return 0


def is_wsl_symlink(path):
    return platform_id() == "windows" and reparse_tag(path) == IO_REPARSE_TAG_LX_SYMLINK


def iter_wsl_symlinks(directory):
    if platform_id() != "windows":
        return
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    tag = getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0)
                except OSError:
                    continue
                if tag == IO_REPARSE_TAG_LX_SYMLINK:
                    yield Path(entry.path)
    except OSError:
        return


def wsl_symlinks(directory):
    return sorted(iter_wsl_symlinks(directory), key=lambda path: path.name.lower())


def contains_wsl_symlink(directory):
    return next(iter_wsl_symlinks(directory), None) is not None


def parse_lx_symlink_reparse_data(raw):
    """Cible Linux contenue dans un REPARSE_DATA_BUFFER de lien WSL."""
    if len(raw) < 12:
        raise ValueError("Données de lien WSL tronquées.")
    tag = int.from_bytes(raw[0:4], "little")
    if tag != IO_REPARSE_TAG_LX_SYMLINK:
        raise ValueError(f"Point d'analyse inattendu : {tag:#x}.")
    length = int.from_bytes(raw[4:6], "little")
    data = raw[8:8 + length]
    if length < 4 or len(data) != length:
        raise ValueError("Données de lien WSL tronquées.")
    version = int.from_bytes(data[0:4], "little")
    if version != 2:
        raise ValueError(f"Version de lien WSL non prise en charge : {version}.")
    return data[4:].decode("utf-8")


def read_wsl_symlink(path):
    """Lit la cible d'un lien WSL sans lancer WSL."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    share_all = 0x1 | 0x2 | 0x4
    open_existing = 3
    open_reparse_point = 0x00200000
    backup_semantics = 0x02000000
    handle = kernel32.CreateFileW(
        str(path), 0, share_all, None, open_existing, open_reparse_point | backup_semantics, None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_string_buffer(16 * 1024)
        returned = wintypes.DWORD()
        if not kernel32.DeviceIoControl(
            handle, FSCTL_GET_REPARSE_POINT, None, 0, buffer, len(buffer), ctypes.byref(returned), None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return parse_lx_symlink_reparse_data(buffer.raw[:returned.value])
    finally:
        kernel32.CloseHandle(handle)


def native_link_value(linux_target):
    """Valeur d'un lien Windows équivalent, ou None si la cible n'est pas relative."""
    target = str(linux_target or "")
    if not target or target.startswith("/") or any(character in target for character in "\\:\0"):
        return None
    parts = [part for part in target.split("/") if part not in {"", "."}]
    if not parts:
        return None
    return os.path.join(*parts)


def native_symlinks_supported(directory):
    """Vrai si Windows autorise un lien symbolique natif dans ce dossier."""
    if platform_id() != "windows":
        return True
    directory = Path(directory)
    key = os.path.normcase(os.path.abspath(directory))
    now = time.monotonic()
    succeeded_at = NATIVE_SYMLINK_SUPPORT.get(key)
    if succeeded_at is not None and now - succeeded_at < NATIVE_SYMLINK_SUPPORT_TTL_SECONDS:
        return True
    if not directory.is_dir():
        return False
    probe = directory / f".odoo_manager_symlink_probe_{uuid.uuid4().hex}"
    try:
        os.symlink(".", probe, target_is_directory=True)
    except OSError:
        NATIVE_SYMLINK_SUPPORT.pop(key, None)
        return False
    remove_link_entry(probe)
    NATIVE_SYMLINK_SUPPORT[key] = now
    return True


def read_migration_journal(directory):
    try:
        payload = json.loads((Path(directory) / MIGRATION_JOURNAL_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    links = payload.get("links") if isinstance(payload, dict) else None
    if not isinstance(links, dict):
        return {}
    return {
        name: value
        for name, value in links.items()
        if isinstance(name, str) and isinstance(value, str) and name == Path(name).name
    }


def write_migration_journal(directory, links):
    journal = Path(directory) / MIGRATION_JOURNAL_NAME
    temporary = journal.with_name(journal.name + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "links": links}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, journal)


def remove_link_entry(path):
    """Supprime l'entrée du lien lui-même, jamais sa cible.

    Un lien WSL créé vers un dossier existant porte l'attribut répertoire : Windows
    refuse alors DeleteFile (WinError 5) et exige RemoveDirectory, qui retire le
    point d'analyse sans parcourir la cible.
    """
    try:
        os.unlink(path)
    except (PermissionError, IsADirectoryError):
        os.rmdir(path)


def is_native_link_to(path, value):
    if reparse_tag(path) != IO_REPARSE_TAG_SYMLINK:
        return False
    try:
        return os.path.normcase(os.readlink(path)) == os.path.normcase(value)
    except OSError:
        return False


def convert_wsl_symlinks(directory, log=None):
    """Remplace les liens WSL d'un dossier par des liens Windows relatifs identiques.

    Les cibles sont notées dans un journal avant toute modification : une
    conversion interrompue (fermeture, panne) reprend là où elle s'est arrêtée.
    """
    directory = Path(directory)
    log = log or (lambda _message: None)
    links = read_migration_journal(directory)
    skipped = []
    for path in wsl_symlinks(directory):
        try:
            linux_target = read_wsl_symlink(path)
        except (OSError, ValueError) as exc:
            skipped.append((path.name, f"lien illisible : {exc}"))
            continue
        value = native_link_value(linux_target)
        if value is None:
            skipped.append((path.name, f"cible non relative : {linux_target}"))
            continue
        links[path.name] = value
    if not links:
        return {"converted": 0, "skipped": skipped, "failures": []}

    write_migration_journal(directory, links)
    converted = 0
    failures = []
    total = len(links)
    for index, (name, value) in enumerate(sorted(links.items(), key=lambda item: item[0].lower()), start=1):
        path = directory / name
        if is_native_link_to(path, value):
            converted += 1
        elif os.path.lexists(path) and reparse_tag(path) != IO_REPARSE_TAG_LX_SYMLINK:
            failures.append((name, "entrée modifiée depuis le début de la conversion"))
        else:
            try:
                if os.path.lexists(path):
                    remove_link_entry(path)
                os.symlink(value, path, target_is_directory=True)
                converted += 1
            except OSError as exc:
                failures.append((name, str(exc)))
        if index % 200 == 0 or index == total:
            log(f"Conversion des liens : {index}/{total}")
    if not failures:
        (directory / MIGRATION_JOURNAL_NAME).unlink(missing_ok=True)
    return {"converted": converted, "skipped": skipped, "failures": failures}
