"""Arrêt des actions en cours : points d'arrêt, sous-processus, étapes protégées et retour arrière.

Une action (job) s'exécute dans son propre thread, auquel est lié un `JobControl`. Le code métier
n'a pas besoin de connaître l'action qui l'appelle : les fonctions de ce module agissent sur le
contrôle du thread courant et ne font rien hors d'une action (tests, appels directs).

Arrêter une action :
- termine ses sous-processus locaux et, dans les conteneurs, les processus lancés par ses
  `docker exec` (repérés par la variable ODOO_MANAGER_JOB : tuer le client docker ne suffit pas) ;
- lève `JobCancelled` au prochain point d'arrêt, une seule fois, pour que les blocs `finally`
  et `except BaseException` existants remettent les choses en ordre sans être interrompus ;
- exécute ensuite, du plus récent au plus ancien, les retours arrière enregistrés par l'action.
"""

import contextlib
import os
import shlex
import subprocess
import threading
import time
import uuid

from .platform import executable_search_path, hidden_process_kwargs


JOB_ENV_VARIABLE = "ODOO_MANAGER_JOB"
# Options de `docker exec` suivies d'une valeur : le conteneur est le premier argument libre après elles.
DOCKER_EXEC_VALUE_OPTIONS = frozenset({"-e", "--env", "--env-file", "-u", "--user", "-w", "--workdir", "--detach-keys"})
CONTAINER_KILL_GRACE_SECONDS = 3


class JobCancelled(BaseException):
    """Arrêt demandé par l'utilisateur.

    BaseException : un `except Exception` de reprise sur erreur ne doit pas l'absorber.
    """


def container_kill_script(token, signal):
    marker = shlex.quote(f"{JOB_ENV_VARIABLE}={token}")
    return (
        "found=1; "
        "for directory in /proc/[0-9]*; do "
        'pid=${directory#/proc/}; [ "$pid" = "$$" ] && continue; '
        f'if tr "\\000" "\\n" < "$directory/environ" 2>/dev/null | grep -qxF {marker}; then '
        f'kill -{signal} "$pid" 2>/dev/null; found=0; fi; '
        "done; exit $found"
    )


class JobControl:
    def __init__(self, token=None):
        self.token = token or uuid.uuid4().hex
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._raised = False
        self._protected = []
        self._processes = set()
        self._interrupts = {}
        self._containers = {}
        self._reverts = []

    @property
    def cancel_requested(self):
        return self._cancel.is_set()

    @property
    def protected_step(self):
        with self._lock:
            return self._protected[-1][0] if self._protected else ""

    @property
    def irreversible_step(self):
        with self._lock:
            return next((label for label, irreversible in reversed(self._protected) if irreversible), "")

    @property
    def cleaning_up(self):
        return self._raised

    def request_cancel(self):
        """Demande l'arrêt : `accepted`, `deferred` (étape protégée en cours), `refused` ou `already`.

        Une étape irréversible refuse l'arrêt : l'accepter pour l'appliquer après elle ferait
        annoncer « arrêtée » une action dont l'essentiel est déjà fait.
        """
        with self._lock:
            if self._cancel.is_set():
                return "already"
            if any(irreversible for _label, irreversible in self._protected):
                return "refused"
            self._cancel.set()
            deferred = bool(self._protected)
            token = self.token
        if deferred:
            return "deferred"
        self.interrupt_work(token)
        return "accepted"

    def begin_cleanup(self):
        """Plus aucun point d'arrêt ne lève : le retour arrière doit aller à son terme."""
        with self._lock:
            self._enter_cleanup()

    def _enter_cleanup(self):
        if not self._raised:
            self._raised = True
            # Les commandes du nettoyage portent un autre repère : l'arrêt en cours dans les
            # conteneurs ne doit viser que les processus de l'étape interrompue.
            self.token = uuid.uuid4().hex

    def _interruptible_now(self):
        return self._cancel.is_set() and not self._raised and not self._protected

    def checkpoint(self):
        with self._lock:
            if not self._interruptible_now():
                return
            self._enter_cleanup()
        raise JobCancelled()

    def sleep(self, seconds):
        if self._interruptible_now():
            self.checkpoint()
        if self._cancel.is_set():
            time.sleep(seconds)
        else:
            self._cancel.wait(seconds)
        self.checkpoint()

    @contextlib.contextmanager
    def protected(self, label, irreversible=False):
        with self._lock:
            self._protected.append((label, irreversible))
        try:
            yield
        finally:
            with self._lock:
                self._protected.pop()
        # Arrêt demandé pendant l'étape : pris en compte dès qu'elle est terminée.
        self.checkpoint()

    def interrupt_work(self, token):
        with self._lock:
            processes = list(self._processes)
            interrupts = list(self._interrupts.values())
            containers = dict(self._containers)
        for process in processes:
            try:
                if process.poll() is None:
                    process.terminate()
            except OSError:
                pass
        for interrupt in interrupts:
            try:
                interrupt()
            except Exception:
                pass
        if containers:
            threading.Thread(target=self.kill_container_processes, args=(containers, token), daemon=True).start()

    def kill_container_processes(self, containers, token):
        env = os.environ.copy()
        env["PATH"] = executable_search_path()
        for container, prefix in containers.items():
            for signal in ("TERM", "KILL"):
                try:
                    result = subprocess.run(
                        [*prefix, "exec", container, "sh", "-c", container_kill_script(token, signal)],
                        capture_output=True,
                        timeout=20,
                        check=False,
                        env=env,
                        **hidden_process_kwargs(),
                    )
                except (OSError, subprocess.SubprocessError):
                    break
                if result.returncode != 0:
                    break
                time.sleep(CONTAINER_KILL_GRACE_SECONDS)

    def track_process(self, process):
        with self._lock:
            self._processes.add(process)
            interrupt = self._interruptible_now()
        if interrupt and process.poll() is None:
            process.terminate()

    def untrack_process(self, process):
        with self._lock:
            self._processes.discard(process)

    @contextlib.contextmanager
    def interruptible(self, interrupt):
        key = object()
        with self._lock:
            self._interrupts[key] = interrupt
            now = self._interruptible_now()
        if now:
            interrupt()
        try:
            yield
        except BaseException:
            # L'erreur provoquée par l'interruption (connexion fermée) devient un arrêt :
            # le nettoyage qui suit ne doit pas rencontrer un second point d'arrêt.
            self.checkpoint()
            raise
        finally:
            with self._lock:
                self._interrupts.pop(key, None)

    def mark_container(self, prefix, container):
        with self._lock:
            self._containers[container] = tuple(prefix)

    def on_cancel(self, label, callback):
        entry = (label, callback)
        with self._lock:
            self._reverts.append(entry)
        return entry

    def discard_revert(self, entry):
        with self._lock:
            if entry in self._reverts:
                self._reverts.remove(entry)

    def run_reverts(self, log):
        """Exécute les retours arrière du plus récent au plus ancien ; retourne ceux qui ont échoué."""
        self.begin_cleanup()
        with self._lock:
            reverts = list(reversed(self._reverts))
            self._reverts.clear()
        failures = []
        for label, callback in reverts:
            log(f"Retour arrière : {label}...")
            try:
                callback()
            except BaseException as exc:
                failures.append(f"{label} ({exc})")
                log(f"Retour arrière incomplet : {label} : {exc}")
        return failures


_STATE = threading.local()


def current_control():
    return getattr(_STATE, "control", None)


@contextlib.contextmanager
def bind_control(control):
    previous = current_control()
    _STATE.control = control
    try:
        yield control
    finally:
        _STATE.control = previous


def checkpoint():
    control = current_control()
    if control:
        control.checkpoint()


def sleep(seconds):
    control = current_control()
    if control:
        control.sleep(seconds)
    else:
        time.sleep(seconds)


def protected(label, irreversible=False):
    control = current_control()
    return control.protected(label, irreversible) if control else contextlib.nullcontext()


def interruptible(interrupt):
    control = current_control()
    return control.interruptible(interrupt) if control else contextlib.nullcontext()


def on_cancel(label, callback):
    control = current_control()
    return control.on_cancel(label, callback) if control else None


def discard_revert(entry):
    control = current_control()
    if control and entry:
        control.discard_revert(entry)


def track_process(process):
    control = current_control()
    if control:
        control.track_process(process)


def untrack_process(process):
    control = current_control()
    if control:
        control.untrack_process(process)


def run_process(command, timeout, **kwargs):
    """`subprocess.run` interruptible par l'arrêt de l'action courante."""
    control = current_control()
    if control is None:
        return subprocess.run(command, timeout=timeout, check=False, **kwargs)
    control.checkpoint()
    process = subprocess.Popen(command, **kwargs)
    track_process(process)
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
        except BaseException:
            process.kill()
            process.wait()
            raise
    finally:
        untrack_process(process)
    # Processus terminé par l'arrêt : l'appelant reçoit l'arrêt, pas un code d'erreur trompeur.
    control.checkpoint()
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def mark_docker_arguments(prefix, arguments):
    """Ajoute le repère de l'action courante à un `docker exec`, pour pouvoir l'arrêter dans le conteneur.

    Un `docker exec -d` lance un service (le serveur Odoo) : il n'appartient pas à l'étape en
    cours et n'est pas repéré ; les retours arrière décident s'il faut l'arrêter.
    """
    control = current_control()
    arguments = list(arguments)
    if not control or not arguments or arguments[0] != "exec":
        return arguments
    index = 1
    detached = False
    while index < len(arguments) and str(arguments[index]).startswith("-"):
        option = str(arguments[index])
        if option in {"-d", "--detach"} or (option.startswith("-") and not option.startswith("--") and "d" in option[1:]):
            detached = True
        index += 2 if option in DOCKER_EXEC_VALUE_OPTIONS else 1
    if detached or index >= len(arguments):
        return arguments
    control.mark_container(prefix, arguments[index])
    return ["exec", "-e", f"{JOB_ENV_VARIABLE}={control.token}", *arguments[1:]]
