"""Budget WSL sous Windows : les écrans d'un workspace natif ne doivent pas lancer wsl.exe.

Chaque wsl.exe démarre ou maintient la VM WSL et coûte de 0,3 à plusieurs secondes.
L'overview est rafraîchi toutes les 10 s : une sonde WSL oubliée tient la VM éveillée
en permanence. Windows est simulé ; les commandes sont enregistrées, jamais exécutées.
"""
import os
import platform
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from odoo_manager_core import platform as manager_platform
from odoo_manager_core.project_service import GIT_SSH_COMMAND, ProjectService, merge_wslenv
from odoo_manager_core.system import reset_docker_backend_cache
from odoo_manager_core.config import ManagerSettings
import test_module_layout
from test_module_layout import DummyJob
import odoo_manager_web as web


def executable_name(command):
    # Chemins Windows simulés : « \\ » n'est pas un séparateur pour Path sous macOS et Linux.
    return Path(str(command[0]).replace("\\", "/")).name.casefold() if command else ""


def is_wsl(command):
    return executable_name(command) in {"wsl", "wsl.exe"}


class SimulatedWindows:
    """Windows avec Docker Desktop et Git pour Windows installés et opérationnels."""

    def __init__(self, native_tools=("docker", "git", "ssh-keygen", "winget", "wsl.exe")):
        self.native_tools = set(native_tools)
        self.commands = []

    def which(self, name, *args, **kwargs):
        return rf"C:\Tools\{name}" if name in self.native_tools else None

    def run(self, command, *args, **kwargs):
        command = [str(part) for part in command]
        self.commands.append(command)
        text = kwargs.get("text") or kwargs.get("encoding")
        stdout = ""
        if "info" in command and "{{json .ServerVersion}}" in command:
            stdout = '"29.5.3"\n'
        elif "--version" in command:
            stdout = "git version 2.55.0\n" if any("git" in part for part in command[:4]) else "Docker version 29.5.3\n"
        return subprocess.CompletedProcess(command, 0, stdout if text else stdout.encode(), "" if text else b"")

    def popen(self, command, *args, **kwargs):
        self.commands.append([str(part) for part in command])
        raise OSError("processus long non attendu dans ce scénario")

    def wsl_commands(self):
        return [command for command in self.commands if is_wsl(command)]

    def patches(self):
        return (
            mock.patch.object(platform, "system", return_value="Windows"),
            mock.patch.object(shutil, "which", side_effect=self.which),
            mock.patch.object(subprocess, "run", side_effect=self.run),
            mock.patch.object(subprocess, "Popen", side_effect=self.popen),
        )


def reset_windows_caches(project):
    reset_docker_backend_cache()
    manager_platform.reset_wsl_executable_cache()
    web.WSL_SHELL_AVAILABILITY.clear()
    web.clear_project_module_cache(project)


class WslBudgetTests(unittest.TestCase):
    def setUp(self):
        test_module_layout.ModuleLayoutTests.setUp(self)
        self.addCleanup(test_module_layout.ModuleLayoutTests.tearDown, self)
        for name in ("sale", "stock"):
            module = self.project_root / "odoo" / "addons-store" / name
            module.mkdir(parents=True)
            (module / "__manifest__.py").write_text("{'name': 'Module', 'depends': ['base']}\n", encoding="utf-8")
        direct = self.project_root / "odoo" / "addons" / "custom_direct"
        direct.mkdir()
        (direct / "__manifest__.py").write_text("{'name': 'Direct'}\n", encoding="utf-8")
        previous_settings = web.SETTINGS
        web.SETTINGS = ManagerSettings.from_dict({"workspace": str(self.root)}, str(self.root))
        self.addCleanup(setattr, web, "SETTINGS", previous_settings)
        reset_windows_caches(self.project)
        self.addCleanup(reset_windows_caches, self.project)

    def run_screens(self, windows, strict=True):
        patches = windows.patches()
        for patcher in patches:
            patcher.start()
        try:
            screens = {
                "bootstrap": web.bootstrap_snapshot,
                "overview": web.overview,
                "system/status": web.system_status_snapshot,
                "project-creation-prerequisites": web.project_creation_prerequisites,
                "ssh-keys": web.ssh_public_keys_snapshot,
                "addon-links": lambda: web.addon_links_snapshot(self.project),
                "modules": lambda: web.modules_for(self.project),
                "module-graph": lambda: web.project_module_graph(self.project),
            }
            launched = {}
            for name, screen in screens.items():
                start = len(windows.commands)
                try:
                    screen()
                except RuntimeError:
                    # Scénarios de contrôle : seules les commandes lancées comptent.
                    if strict:
                        raise
                launched[name] = [command for command in windows.commands[start:] if is_wsl(command)]
            return launched
        finally:
            for patcher in reversed(patches):
                patcher.stop()

    def test_native_windows_screens_never_launch_wsl(self):
        windows = SimulatedWindows()
        launched = self.run_screens(windows)
        self.assertEqual({name: [] for name in launched}, launched)
        # Le scénario a bien interrogé Docker et Git pour Windows : le budget n'est pas vide par accident.
        self.assertTrue(any(executable_name(command) == "docker" for command in windows.commands))

    def test_budget_detects_legitimate_wsl_use(self):
        # Contrôle positif : sans Docker ni Git pour Windows, WSL est le recours attendu.
        windows = SimulatedWindows(native_tools=("wsl.exe",))
        launched = self.run_screens(windows, strict=False)
        self.assertTrue(launched["system/status"], "la sonde Docker dans WSL doit rester possible")
        self.assertTrue(launched["project-creation-prerequisites"], "la recherche de Git dans WSL doit rester possible")

    def test_unconverted_wsl_links_still_read_modules_through_wsl(self):
        windows = SimulatedWindows()
        with mock.patch.object(web, "contains_wsl_symlink", return_value=True):
            launched = self.run_screens(windows, strict=False)
        self.assertTrue(launched["modules"])


class WindowsGitEnvironmentTests(unittest.TestCase):
    def test_git_search_in_wsl_is_cached(self):
        manager_platform.reset_wsl_executable_cache()
        self.addCleanup(manager_platform.reset_wsl_executable_cache)
        with mock.patch.object(manager_platform.platform, "system", return_value="Windows"), \
                mock.patch.object(manager_platform, "host_executable_available", return_value=True), \
                mock.patch.object(manager_platform, "_probe_wsl_executable_distribution", return_value="Ubuntu") as probe:
            self.assertEqual("Ubuntu", manager_platform.find_wsl_executable_distribution("git"))
            self.assertEqual("Ubuntu", manager_platform.find_wsl_executable_distribution("git"))
        probe.assert_called_once()

    def test_missing_git_in_wsl_is_checked_again_sooner(self):
        manager_platform.reset_wsl_executable_cache()
        self.addCleanup(manager_platform.reset_wsl_executable_cache)
        clock = [1000.0]
        with mock.patch.object(manager_platform.platform, "system", return_value="Windows"), \
                mock.patch.object(manager_platform, "host_executable_available", return_value=True), \
                mock.patch.object(manager_platform.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(manager_platform, "_probe_wsl_executable_distribution", side_effect=[None, "Ubuntu"]) as probe:
            self.assertIsNone(manager_platform.find_wsl_executable_distribution("git"))
            clock[0] += manager_platform.WSL_EXECUTABLE_MISSING_TTL_SECONDS + 1
            self.assertEqual("Ubuntu", manager_platform.find_wsl_executable_distribution("git"))
        self.assertEqual(2, probe.call_count)

    def test_non_interactive_git_settings_reach_wsl(self):
        service = ProjectService(ManagerSettings(workspace="C:/Odoo"), Path("C:/Odoo"))
        with mock.patch("odoo_manager_core.project_service.platform.system", return_value="Windows"), \
                mock.patch.dict(os.environ, {"WSLENV": "USERPROFILE/p"}, clear=False):
            os.environ.pop("GIT_SSH_COMMAND", None)
            env = service.env()
        self.assertEqual(GIT_SSH_COMMAND, env["GIT_SSH_COMMAND"])
        self.assertEqual("USERPROFILE/p:GIT_TERMINAL_PROMPT:GIT_SSH_COMMAND", env["WSLENV"])

    def test_custom_windows_ssh_command_is_not_forwarded_to_wsl(self):
        service = ProjectService(ManagerSettings(workspace="C:/Odoo"), Path("C:/Odoo"))
        with mock.patch("odoo_manager_core.project_service.platform.system", return_value="Windows"), \
                mock.patch.dict(os.environ, {"GIT_SSH_COMMAND": r"C:\PuTTY\plink.exe -batch", "WSLENV": ""}):
            env = service.env()
        self.assertEqual("GIT_TERMINAL_PROMPT", env["WSLENV"])

    def test_wslenv_merge_keeps_existing_flags_without_duplicates(self):
        self.assertEqual("A/p:GIT_TERMINAL_PROMPT", merge_wslenv("A/p:GIT_TERMINAL_PROMPT/u", ["GIT_TERMINAL_PROMPT"]).replace("/u", ""))
        self.assertEqual("GIT_TERMINAL_PROMPT", merge_wslenv("", ["GIT_TERMINAL_PROMPT"]))


class NativeModuleListingCostTests(unittest.TestCase):
    def setUp(self):
        test_module_layout.ModuleLayoutTests.setUp(self)
        self.addCleanup(test_module_layout.ModuleLayoutTests.tearDown, self)

    def test_wsl_link_is_never_deleted_blindly(self):
        with mock.patch.object(web, "is_wsl_symlink", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "Convertis d'abord les liens"):
                web.delete_module_file_entry(DummyJob(), self.project, "custom_module")

    def test_listing_does_not_resolve_the_common_parent_for_every_module(self):
        storage = self.project_root / "odoo" / "addons-store"
        for index in range(40):
            module = storage / f"module_{index}"
            module.mkdir(parents=True)
            (module / "__manifest__.py").write_text("{}\n", encoding="utf-8")
        with mock.patch.object(web, "safe_resolve", wraps=web.safe_resolve) as resolve:
            modules = web.modules_for(self.project)
        self.assertEqual(40, len(modules))
        # Contexte du projet (3 dossiers) par lecture ; aucun module ordinaire n'ajoute de résolution.
        self.assertLessEqual(resolve.call_count, 6)


if __name__ == "__main__":
    unittest.main()
