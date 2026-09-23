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
import re
import signal
import subprocess
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


def missing_pieces(app_path, lib_path) -> str:
    """What a session cannot start without, checked before it is started.

    A filter path that does not exist fails five layers down, inside the
    application, as one line on the agent's standard error:

        qatrec-gate: could not load: /home/x/qatrec-filter/libqatrec.6.10.so
        qatrec-gate:   reason: cannot open shared object file

    Everything else looks like it worked -- the application opens, the panel
    says recording -- and no event ever arrives. A path is a thing we can check
    in a microsecond, so it is checked where somebody is looking.
    """
    problems = []
    if not lib_path:
        problems.append(
            "no event filter was given. Build one with `python -m "
            "qat_recorder build-filter` and give the panel the "
            "libqatrec.*.so it produces.")
    elif not Path(lib_path).exists():
        # os.path for the name in the message, Path for the questions about
        # disk: a posix path must come back out of the message as the posix
        # path that went in, wherever the panel happens to be running.
        shown = os.path.dirname(str(lib_path))
        folder = Path(lib_path).parent
        built = sorted(path.name for path in folder.glob("libqatrec*.so")) \
            if folder.is_dir() else []
        detail = (f" {shown} holds " + ", ".join(built)) if built else \
            (f" {shown} holds no filter at all" if folder.is_dir()
             else f" {shown} does not exist")
        problems.append(
            f"the event filter {lib_path} is not there.{detail}. Nothing is "
            "recorded without it.")
    if app_path and not Path(app_path).exists():
        problems.append(f"the application {app_path} is not there.")
    return "\n".join(problems)


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


#: How deep to look beside an application for the Qt it carries with it.
#: `bin/spine` with `lib/Qt/lib/libQt6Core.so.6` is four levels from the binary;
#: anything deeper than that is somebody else's tree.
BUNDLE_DEPTH = 5


def bundled_qt_majors(app_path, depth: int = BUNDLE_DEPTH) -> set:
    """Which Qt an application will load, judged from the files beside it.

    An application that ships its own Qt is the case that matters: the Qt it
    runs has nothing to do with the Qt this machine has, so the filter built
    from this machine's headers can be the wrong major version entirely --
    which produces a recording where nothing is ever recorded, and no error
    anywhere saying why.

    Empty when nothing was found, which means "no opinion", not "no Qt".
    """
    # An application we cannot see is not an application to look beside. Left
    # out, `Path("").resolve()` is the working directory and this walks the
    # tree the recorder happens to have been started in.
    if not app_path:
        return set()
    try:
        application = Path(app_path).resolve()
        if not application.exists():
            return set()
        start = application.parent
    except OSError:                                   # pragma: no cover
        return set()

    found = set()
    for base in _bundle_roots(start):
        for major, name in ((5, "libQt5Core.so.5"), (6, "libQt6Core.so.6")):
            if major in found:
                continue
            if _find_within(base, name, depth):
                found.add(major)
    return found


#: Directories that are not one product's tree. `/usr/bin/app` has `/usr` above
#: it, and searching /usr for a bundled Qt is both wrong and slow.
SYSTEM_ROOTS = frozenset((
    "/", "/usr", "/usr/local", "/opt", "/bin", "/sbin", "/lib", "/lib64",
    "/home", "/var", "/tmp", "/srv", "/etc", "/snap",
))


def _bundle_roots(start: Path) -> list:
    """Where an application's own Qt could be, and nowhere else.

    The directory the application is in, plus the one above it when the
    application sits in a `bin/` -- the layout every self-contained product
    uses, and the one that puts `lib/Qt/lib` a level up from the executable.
    """
    roots = [start] if start.is_dir() else []
    above = start.parent
    if (start.name in ("bin", "sbin", "lib", "lib64")
            and above != start
            and above.is_dir()
            and above.as_posix() not in SYSTEM_ROOTS):
        roots.append(above)
    return roots


#: A ceiling on the search, so a product that ships ten thousand files costs a
#: bounded amount of time before a recording rather than an unbounded one.
BUNDLE_BUDGET = 2000


def _find_within(base: Path, name: str, depth: int,
                 budget: int = BUNDLE_BUDGET) -> bool:
    """`name` anywhere under `base`, no deeper than `depth`."""
    roots = [(base, 0)]
    while roots and budget > 0:
        budget -= 1
        folder, level = roots.pop()
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file() or entry.is_symlink():
                    if entry.name == name:
                        return True
                elif entry.is_dir() and level < depth:
                    roots.append((entry, level + 1))
            except OSError:                           # pragma: no cover
                continue
    return False


def filter_qt_major(lib_path) -> Optional[int]:
    """The Qt major the filter was built against, from its name."""
    version = filter_qt_version(lib_path)
    return version[0] if version else None


def filter_qt_version(lib_path) -> Optional[tuple]:
    """The Qt the filter was built against. `libqatrec.6.2.so` -> (6, 2)."""
    if not lib_path:
        return None
    match = re.match(r"^libqatrec\.(\d+)\.(\d+)\.so$", Path(lib_path).name)
    return (int(match.group(1)), int(match.group(2))) if match else None


def bundled_qt_version(app_path, depth: int = None) -> Optional[tuple]:
    """The exact Qt an application carries, from the file name it carries it as.

    `libQt6Core.so.6.8.6` is what the SONAME `libQt6Core.so.6` points at, and
    the full version is the part that matters for which way compatibility runs.
    """
    if not app_path:
        return None
    try:
        application = Path(app_path).resolve()
        if not application.exists():
            return None
    except OSError:                                   # pragma: no cover
        return None

    best = None
    for base in _bundle_roots(application.parent):
        for found in _versioned_qt_cores(base,
                                         depth if depth is not None
                                         else BUNDLE_DEPTH):
            if best is None or found > best:
                best = found
    return best


def _versioned_qt_cores(base: Path, depth: int,
                        budget: int = 2000) -> list:
    """Every `libQt[56]Core.so.<major>.<minor>.<patch>` under `base`."""
    pattern = re.compile(r"^libQt[56]Core\.so\.(\d+)\.(\d+)\.(\d+)$")
    versions = []
    roots = [(base, 0)]
    while roots and budget > 0:
        budget -= 1
        folder, level = roots.pop()
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                match = pattern.match(entry.name)
                if match:
                    versions.append(tuple(int(n) for n in match.groups()))
                elif entry.is_dir() and level < depth:
                    roots.append((entry, level + 1))
            except OSError:                           # pragma: no cover
                continue
    return versions


def qt_mismatch(app_path, lib_path) -> str:
    """A warning when the filter cannot possibly see this application's widgets.

    A warning and not a refusal: this is read off a directory listing, and a
    guess is not allowed to stop somebody recording.
    """
    built = filter_qt_version(lib_path)
    if built is None:
        return ""
    major = built[0]
    majors = bundled_qt_majors(app_path)
    if not majors:
        return ""

    if major in majors:
        # Right major version. Compatibility within it runs one way only: a
        # filter built against 6.2 works inside a 6.8 application, and one
        # built against 6.10 does not -- it can reference symbols the
        # application's Qt does not have, and then it is the newest Qt on this
        # machine that decides whether recording works.
        carried = bundled_qt_version(app_path)
        if carried and carried[0] == major and built[1] > carried[1]:
            version = ".".join(str(part) for part in carried)
            return (f"{Path(lib_path).name} was built against Qt {built[0]}."
                    f"{built[1]}, but {Path(app_path).name} carries Qt "
                    f"{version}. Qt is binary compatible forward, not "
                    "backward, so a filter built against a newer minor can "
                    "reference symbols this application's Qt does not have. "
                    f"Build against Qt {carried[0]}.{carried[1]} or older.")
        return ""
    carries = " and ".join(f"Qt {one}" for one in sorted(majors))
    wanted = min(majors)
    # os.path, not Path: the message is composed wherever the panel runs and
    # read wherever the application does, and a posix path must come back out
    # as the posix path that was put in.
    out = os.path.dirname(str(lib_path))
    return (f"{Path(lib_path).name} was built against Qt {major}, but "
            f"{Path(app_path).name} carries {carries} beside it. A filter "
            "built against the wrong major version loads without complaining "
            "and then sees none of the application's widgets. To build the "
            "right one:\n"
            f"    sudo apt install qt{wanted}-base-dev"
            f"     (RHEL: sudo dnf install qt{wanted}-qtbase-devel)\n"
            f"    python -m qat_recorder build-filter --out {out} "
            f"--qt {wanted}\n"
            f"then point the recorder at the libqatrec.{wanted}.*.so it "
            "builds.")


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
          clock: Callable = time.monotonic, kill: Optional[Callable] = None,
          listing=None):
    """Start the registered application `name` and return a connected context.

    `follow` decides whether to expect the application to be a child of what we
    start; by default, whenever the registered target is a script. An
    application launched directly takes Qat's own path unchanged -- there is
    nothing to improve there, and nothing worth risking.
    """
    if follow is None:
        # Under sudo the application is sudo's child, whatever it is.
        follow = is_script(app_path) or as_root()
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
            # The launcher is gone, but a script that dies does not take the
            # daemons it started with it.
            abandon(context, sleep=sleep, clock=clock, kill=kill,
                    listing=listing, parent=parent)
            raise LaunchError(_died_message(context, name, app_path))
        if clock() >= deadline:
            # Everything we started, before saying so. A launch that fails and
            # leaves the application up means the next attempt runs against a
            # machine that already has one open -- observed as two copies of
            # the same application, neither of them being recorded.
            abandon(context, sleep=sleep, clock=clock, kill=kill,
                    listing=listing, parent=parent)
            raise LaunchError(
                f"{name} did not report a port within {timeout:.0f}s. The "
                f"process started (pid {launched}) and is still running, but "
                "neither it nor any process it started announced a Qat server. "
                "Either the application has not finished starting, or the "
                "injector never reached it -- run with QATREC_GATE_DEBUG=1 to "
                "see which processes the gate instrumented."
                + server_hint())
        sleep(POLL_SECONDS)


def children_of(pid: int, listing=None, parent: Callable = parent_of) -> list:
    """Every process descended from `pid`, deepest last.

    /proc is walked rather than remembered: the application we want is started
    by a script we did not write, and the only record of what it started is the
    process table.
    """
    if listing is None:
        try:
            listing = [int(entry) for entry in os.listdir("/proc")
                       if entry.isdigit()]
        except OSError:                                # pragma: no cover
            return []
    found = [other for other in sorted(listing)
             if other != pid and descends_from(other, pid, parent=parent)]
    return found


def abandon(context, *, sleep: Callable = time.sleep,
            clock: Callable = time.monotonic, grace: float = 2.0,
            kill: Optional[Callable] = None, listing=None,
            parent: Callable = parent_of) -> None:
    """Stop everything a failed launch started, quietly.

    Best effort by definition -- we are already reporting a failure and have
    nothing to gain by failing differently while cleaning up.
    """
    launched = getattr(context, "pid", None)
    if not launched or launched <= 0:
        return
    for pid in reversed(children_of(launched, listing=listing, parent=parent)):
        try:
            _stop(pid, sleep=sleep, clock=clock, grace=grace, kill=kill)
        except Exception:                              # noqa: BLE001
            pass
    try:
        context.kill()
    except Exception:                                  # noqa: BLE001
        try:
            _stop(launched, sleep=sleep, clock=clock, grace=grace, kill=kill)
        except Exception:                              # noqa: BLE001
            pass


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
    return message + server_hint()


def server_hint() -> str:
    """The other reason no port file ever appears, named where it is noticed.

    Injection can reach the application, find its Qt and still fail, because
    the server Qat ships for that Qt version was built on a newer distribution
    than this one. From here that is indistinguishable from a timeout -- the
    only evidence is a line in the application's own output -- so the timeout
    says it out loud instead of leaving it to be discovered.
    """
    try:
        from qat_recorder import servers  # noqa: PLC0415

        folder = servers.qat_bin_dir()
        if folder is None:
            return ""
        plan = servers.repairs(servers.survey(folder), servers.Machine.here())
    except Exception:                                 # noqa: BLE001
        return ""
    if not plan:
        return ""
    names = ", ".join(sorted(path.name for path in plan))
    return ("\n\nNote: this machine cannot load " + names + " -- they need a "
            "newer glibc than it has. An application built against that Qt "
            "version will start normally and never answer Qat. Run "
            "`python -m qat_recorder qat-servers` for the detail, or "
            "`--fix` to stand a loadable server in for it.")


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
    if as_root():
        # Qat removes the port file and kills the process it launched, and both
        # belong to root: sudo, and the file the application's server wrote.
        if launcher and not _has_exited(context):
            _stop(launcher, sleep=sleep, clock=clock, grace=grace, kill=kill)
        port_file = getattr(context, "config_file", None)
        if port_file:
            subprocess.run(["sudo", "-n", "rm", "-f", str(port_file)],
                           capture_output=True, check=False)
    return qat_module.close_application(context)


def as_root() -> bool:
    """QATREC_SUDO=1: wrapper.sh starts the application through `sudo -n`.

    For an application that has to run as root -- one whose data folder only
    root can write, say. The agent stays the user it was started as; only the
    application is root, which means only sudo can signal it.
    """
    return os.environ.get("QATREC_SUDO") == "1"


def _sudo_kill(pid: int, sig: int) -> None:
    """os.kill for a process owned by root, failing the same way."""
    result = subprocess.run(["sudo", "-n", "kill", f"-{int(sig)}", str(pid)],
                            capture_output=True, check=False)
    if result.returncode != 0:
        raise ProcessLookupError(pid)


def _stop(pid: int, *, sleep: Callable, clock: Callable, grace: float,
          kill: Optional[Callable] = None) -> None:
    if kill is not None:
        send = kill
    else:
        send = _sudo_kill if as_root() else os.kill
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
