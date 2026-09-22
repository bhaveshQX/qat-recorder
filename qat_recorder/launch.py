# -*- coding: utf-8 -*-
"""
Launching an application that is launched by something else.

Qat starts a process and then waits for `$TEMP/qat-<pid>.txt`, the file its
injected server writes to announce the port it is listening on. The pid is the
process Qat started (`app_launcher.py`: `create_qat_config_file_path(pid)`),
which is exactly right when that process *is* the Qt application -- a binary,
or an interpreter running a Python one.

A launch script is the other case, and it is a common one for any application
big enough to need a launcher: `start_spine.sh` exports half a dozen variables,
starts four helper daemons and *then* runs `./spine`, as a child. The
application is a different process with a different pid, and it writes
`qat-<its own pid>.txt`. Qat waits for a file nobody will ever write and ends
the session before the first step:

    Abort: app terminated

Nothing is wrong with the injection -- Qat is watching the wrong pid. So for a
script the recorder launches detached (Qat does not wait), watches the
temporary folder for the port file that actually appears, confirms the process
that wrote it descends from the one we started, points the context at it and
connects. The application is then the application, however many shells deep it
was started.

The other half of the same problem lives in `native/qatgate.c`: what keeps the
script itself working while it is being run under an injector.
"""

from __future__ import annotations

import inspect
import os
import signal
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

#: Long enough for a heavy application to open its main window on a loaded VM.
#: Shorter than Qat's own start timeout would be, because unlike Qat we can say
#: what we were waiting for.
DEFAULT_TIMEOUT_MS = 60_000

POLL_SECONDS = 0.1

#: The library the gate is built as, beside the event filter.
GATE_NAME = "libqatgate.so"


class LaunchError(RuntimeError):
    """The application did not come up, with a reason worth acting on."""


# -- where Qat keeps the port files ------------------------------------------

def temp_folder() -> Path:
    """The folder Qat writes port files into.

    `qat_environment.get_temp_folder()` reads TEMP and falls back to the
    platform temporary directory; it is repeated rather than imported so that a
    Qat that moves its internals does not take the recorder with it.
    """
    return Path(os.environ.get("TEMP") or tempfile.gettempdir())


def pid_of_port_file(path) -> Optional[int]:
    """The pid a `qat-1234.txt` file is named for."""
    stem = Path(path).stem
    _, _, digits = stem.partition("-")
    try:
        return int(digits)
    except ValueError:
        return None


def port_files(folder=None) -> dict:
    """Every port file currently in the folder, by pid."""
    folder = Path(folder) if folder is not None else temp_folder()
    found = {}
    try:
        entries = list(folder.glob("qat-*.txt"))
    except OSError:                                   # pragma: no cover
        return found
    for entry in entries:
        pid = pid_of_port_file(entry)
        if pid is not None:
            found[pid] = entry
    return found


def read_port(path, attempts: int = 30, sleep: Callable = time.sleep) -> int:
    """The port number, allowing for reading the file mid-write."""
    last = None
    for attempt in range(attempts):
        try:
            return int(Path(path).read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as error:
            last = error
            if attempt + 1 < attempts:
                sleep(POLL_SECONDS)
    raise LaunchError(f"could not read the port from {path}: {last}")


# -- who started whom --------------------------------------------------------

def parent_of(pid: int) -> Optional[int]:
    """The parent of `pid`, or None where that cannot be established."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8",
                                                   errors="replace")
    except OSError:
        return None
    # Field two is the executable name in parentheses and may itself contain
    # spaces and parentheses, so the fields after it are counted from the LAST
    # closing bracket rather than by splitting the line.
    closing = text.rfind(")")
    if closing < 0:
        return None
    fields = text[closing + 1:].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:                                # pragma: no cover
        return None


def descends_from(pid: int, ancestor: int, parent: Callable = parent_of,
                  limit: int = 12) -> bool:
    """Whether `pid` is `ancestor` or was started by it, however indirectly.

    The check is what makes following a child safe: a port file that appeared
    while we were waiting could belong to somebody else's Qat session on the
    same machine, and connecting the recorder to another person's application
    is a worse failure than not connecting at all.
    """
    seen = set()
    current = pid
    for _ in range(limit):
        if current == ancestor:
            return True
        if current in seen or current <= 1:
            return False
        seen.add(current)
        found = parent(current)
        if found is None:
            return False
        current = found
    return False


# -- what we were asked to launch --------------------------------------------

def is_script(path) -> bool:
    """Whether `path` needs a shell to run it.

    A `#!` line, or anything that is not an ELF binary. It decides one thing:
    whether the application may turn out to be a child of the process Qat
    starts, and therefore whether to follow it.
    """
    if not path:
        return False
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError:
        return False
    if head[:2] == b"#!":
        return True
    return head[:4] != b"\x7fELF"


def gate_for(lib_path) -> Optional[str]:
    """The gate library `build-filter` leaves beside the event filter."""
    if not lib_path:
        return None
    try:
        candidate = Path(lib_path).expanduser().resolve().parent / GATE_NAME
    except OSError:                                   # pragma: no cover
        return None
    return str(candidate) if candidate.exists() else None


def gate_env(app_path, lib_path) -> Optional[str]:
    """The gate to use for this application, or None to preload as before.

    Only a launch script needs it. An application Qat starts directly has the
    injector in its own LD_PRELOAD, arriving exactly as its authors intended
    and exactly as every recording so far has had it; routing that through a
    gate would be a change with nothing to gain. Where there is no script,
    there is no gate.
    """
    if not is_script(app_path):
        return None
    return gate_for(lib_path)


def discover_gate(lib_path=None) -> Optional[str]:
    """The gate, from the obvious places, for code that was not told where.

    A generated test knows the application but not where the filter was built;
    this is what lets it use the gate anyway rather than tripping over the same
    launch script the recording tripped over.
    """
    candidates = [
        os.environ.get("QATREC_GATE"),
        gate_for(lib_path or os.environ.get("QATREC_LIB")),
        str(Path.home() / "qatrec-filter" / GATE_NAME),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


# -- launching ---------------------------------------------------------------

def _supports_detached(function) -> bool:
    try:
        return "detached" in inspect.signature(function).parameters
    except (TypeError, ValueError):                   # pragma: no cover
        return False


def start(qat_module, name: str, *, app_path=None, follow: Optional[bool] = None,
          timeout_ms: Optional[int] = None, folder=None,
          parent: Callable = parent_of, sleep: Callable = time.sleep,
          clock: Callable = time.monotonic):
    """Start the registered application `name` and return a connected context.

    `follow` decides whether to expect the application to be a child of what we
    start; by default, whenever the registered target is a script. An
    application launched directly takes Qat's own path unchanged -- there is
    nothing to improve there, and nothing worth risking.
    """
    if follow is None:
        follow = is_script(app_path)
    if not follow or not _supports_detached(qat_module.start_application):
        return qat_module.start_application(name)

    before = set(port_files(folder))
    # Detached: Qat launches and returns instead of waiting for a port file it
    # is going to look for under the wrong pid.
    context = qat_module.start_application(name, None, True)

    timeout = (DEFAULT_TIMEOUT_MS if timeout_ms is None else timeout_ms) / 1000.0
    deadline = clock() + timeout
    launched = getattr(context, "pid", -1)

    while True:
        files = port_files(folder)
        if launched in files:
            # The ordinary case even for a script: one that ends in `exec`
            # keeps the pid, and then there is nothing to follow.
            return _connect(context, files[launched], launched, sleep=sleep,
                            qat_module=qat_module)

        for pid, path in sorted(files.items(),
                                key=lambda item: _mtime(item[1])):
            if pid in before or pid == launched:
                continue
            if descends_from(pid, launched, parent=parent):
                return _connect(context, path, pid, sleep=sleep,
                                qat_module=qat_module)

        if _has_exited(context):
            raise LaunchError(_died_message(context, name, app_path))
        if clock() >= deadline:
            raise LaunchError(
                f"{name} did not report a port within {timeout:.0f}s. The "
                f"process started (pid {launched}) and is still running, but "
                "neither it nor any process it started announced a Qat server. "
                "Either the application has not finished starting, or the "
                "injector never reached it -- run with QATREC_GATE_DEBUG=1 to "
                "see which processes the gate instrumented.")
        sleep(POLL_SECONDS)


def _mtime(path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:                                   # pragma: no cover
        return 0.0


def _has_exited(context) -> bool:
    try:
        return bool(context.is_finished())
    except Exception:                                 # noqa: BLE001
        return False


def _died_message(context, name, app_path) -> str:
    code = None
    try:
        code = context.return_code
    except Exception:                                 # noqa: BLE001  # pragma: no cover
        pass
    detail = f" (exit code {code})" if code is not None else ""
    message = (f"{name} exited{detail} before it or anything it started "
               "announced a Qat server.")
    if app_path and is_script(app_path):
        message += (
            f"\n{app_path} is a launch script, so its own output is the place "
            "to look: a script that cannot find its files usually cannot find "
            "them when run by hand either.")
    return message


def _connect(context, port_file, app_pid: int, sleep: Callable = time.sleep,
             qat_module=None):
    """Point the context at the port file that was actually written."""
    port = read_port(port_file, sleep=sleep)
    try:
        context.config_file = str(port_file)
    except Exception:                                 # noqa: BLE001  # pragma: no cover
        pass
    try:
        context.init_comm(port)
    except Exception:                                 # noqa: BLE001
        # The file exists before the server is accepting on it. Qat's own
        # connect_to() re-reads and retries once for the same reason.
        sleep(POLL_SECONDS)
        context.init_comm(read_port(port_file, sleep=sleep))
    try:
        context.init_version_info()
    except Exception:                                 # noqa: BLE001  # pragma: no cover
        pass
    _lock_ui_if_asked(qat_module)
    # Remembered rather than assigned to context.pid: the pid Qat holds owns the
    # process it launched, and closing the session has to kill that one too.
    context.qatrec_app_pid = app_pid
    return context


def _lock_ui_if_asked(qat_module) -> None:
    """What `start_application` does at the end of its own connect.

    Qat locks the application's UI for the duration of a test so that stray
    human input cannot corrupt it. Taking its launch apart means doing this
    part too, or a replay of a script-launched application would be the only
    one running unlocked. The recorder itself sets lock_ui to "never" -- while
    recording, locking would block the input being recorded.
    """
    if qat_module is None:
        return
    try:
        setting = qat_module.test_settings.Settings.lock_ui.lower()
    except Exception:                                 # noqa: BLE001
        return
    debugging = False
    try:
        from qat.internal.app_launcher import is_debugging  # noqa: PLC0415

        debugging = is_debugging()
    except Exception:                                 # noqa: BLE001
        pass
    if not (setting == "always" or (setting == "auto" and not debugging)):
        return
    try:
        qat_module.lock_application()
    except Exception:                                 # noqa: BLE001
        pass


# -- closing -----------------------------------------------------------------

def close(qat_module, context, *, sleep: Callable = time.sleep,
          clock: Callable = time.monotonic, grace: float = 5.0,
          kill: Optional[Callable] = None):
    """Close the application, and the launcher that started it.

    Order matters. Killing the script first leaves the application it started
    running -- Qat owns the script's pid, not the application's -- and a second
    recording then starts against a machine that already has one open. Asking
    the application to close first also lets the script run its own cleanup,
    which is usually what stops the helper daemons it started.
    """
    app_pid = getattr(context, "qatrec_app_pid", None)
    launcher = getattr(context, "pid", None)
    if app_pid and app_pid != launcher:
        _stop(app_pid, sleep=sleep, clock=clock, grace=grace, kill=kill)
        # Give the launcher a moment to notice and finish on its own.
        limit = clock() + grace
        while clock() < limit and not _has_exited(context):
            sleep(POLL_SECONDS)
    return qat_module.close_application(context)


def _stop(pid: int, *, sleep: Callable, clock: Callable, grace: float,
          kill: Optional[Callable] = None) -> None:
    send = kill if kill is not None else os.kill
    try:
        send(pid, signal.SIGTERM)
    except OSError:
        return
    limit = clock() + grace
    while clock() < limit:
        try:
            send(pid, 0)
        except OSError:
            return
        sleep(POLL_SECONDS)
    try:
        send(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:                                   # pragma: no cover
        pass


# -- registering -------------------------------------------------------------

def register_for_replay(qat_module, name: str, app_path: str,
                        wrapper=None, args: str = "") -> None:
    """Register an application for a replay the way the recording ran it.

    A recording made through a launch script has to be replayed through the
    same script, under the same gate: registering the script directly gives the
    generated test the identical failure the recorder had before it followed
    the application into its children.
    """
    if not is_script(app_path):
        qat_module.register_application(name, app_path, args)
        return

    if wrapper is None:
        from qat_recorder.ui.controller import default_wrapper  # noqa: PLC0415
        wrapper = default_wrapper()

    os.environ["QATREC_APP"] = str(app_path)
    gate = discover_gate()
    if gate:
        os.environ["QATREC_GATE"] = gate
    # Replay records nothing, so the event filter has no business being loaded:
    # it would connect to a receiver that belongs to a session that has ended.
    os.environ.pop("QATREC_LIB", None)
    os.environ.pop("QATREC_PORT", None)
    qat_module.register_application(name, str(wrapper), args)
