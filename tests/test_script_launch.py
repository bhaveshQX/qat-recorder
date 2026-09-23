# -*- coding: utf-8 -*-
"""
Applications that are started by something else.

One customer's application is launched by `start_spine.sh`: it exports a dozen
variables, starts four DICOM helper daemons and then runs `./spine`. Recording
it failed twice over, and neither failure was in anything the recorder does with
widgets.

**The script could not run.** Qat injects by setting LD_PRELOAD, which every
process inherits, and its injector announces itself on stdout in each one. A
line like `cd $(dirname "$0")/..` captures that announcement instead of a path,
so the script changed into a directory called "Loading injector..." and nothing
it owned was where it expected:

    start_spine.sh: line 31: cd: $'Loading injector\\nDetected Qt version...'
    ./start_storescp_carm_ge.sh: 105: exec: ./storescp_carm_ge: not found

**Qat watched the wrong process.** It waits for `$TEMP/qat-<pid>.txt` under the
pid it started -- the script -- while the file is written by `./spine`, a child
with a pid of its own. The wait ends in:

    Abort: app terminated

The gate fixes the first (native/qatgate.c), `qat_recorder.launch` the second.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import pytest

from qat_recorder import launch
from qat_recorder.native import source_dir

ROOT = Path(__file__).resolve().parents[1]


# --- the gate ---------------------------------------------------------------

def gate_source() -> str:
    return (ROOT / "native" / "qatgate.c").read_text(encoding="utf-8")


def without_comments(text: str) -> str:
    """The C with its comments removed, so an assertion about what the code
    does is not answered by what the comments say about it."""
    out = []
    rest = text
    while True:
        start = rest.find("/*")
        if start < 0:
            out.append(rest)
            break
        out.append(rest[:start])
        end = rest.find("*/", start)
        rest = rest[end + 2:] if end >= 0 else ""
    return "".join(out)


def test_the_gate_never_writes_to_stdout():
    """The entire point. stdout is what a command substitution captures, so a
    library preloaded into every process in a launch script must not put a
    single byte there -- which is why it uses write(2, ...) and no stdio."""
    code = without_comments(gate_source())
    assert "write(2, " in code
    assert "write(1, " not in code
    assert "STDOUT_FILENO" not in code
    for forbidden in ("printf", "fputs", "puts(", "stdout", "<stdio.h>"):
        assert forbidden not in code, f"{forbidden} could reach stdout"


def test_the_gate_instruments_the_process_qat_launched():
    text = gate_source()
    assert 'getenv("QATREC_PID")' in text
    assert "getpid()" in text


def test_the_gate_instruments_anything_that_has_qt_loaded():
    """However deep in a tree of shells the application was started."""
    text = gate_source()
    assert "RTLD_NOLOAD" in text
    assert "libQt5Core.so.5" in text
    assert "libQt6Core.so.6" in text


def test_the_gate_never_instruments_a_shell():
    """The recorder says so through QATREC_APP_IS_SCRIPT too, but the invariant
    is worth holding where it cannot be forgotten. A shell has no Qt and
    nothing to offer a recording -- and the injector's OnUnload, printed from a
    destructor, lands in the script's own command substitutions."""
    code = without_comments(gate_source())
    assert "is_a_shell" in code
    assert '"/proc/self/exe"' in code
    for shell in ('"bash"', '"dash"', '"sh"'):
        assert shell in code
    # Qt present wins regardless; the pid rule is what a shell is excluded from.
    assert ("if (!qt_is_loaded() && !(is_the_launched_process() "
            "&& !is_a_shell()))") in code


def test_the_gate_loads_what_the_wrapper_stashed():
    text = gate_source()
    assert 'getenv("QATREC_PRELOAD")' in text
    assert "dlopen(each, RTLD_LAZY | RTLD_GLOBAL)" in text


def test_the_gate_is_built_without_qt():
    """It is preloaded into /bin/sh, dirname and sed as well. A gate that drags
    Qt into every one of them would cost more than the bug it fixes."""
    text = (ROOT / "native" / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "add_library(qatgate SHARED qatgate.c)" in text
    assert "target_link_libraries(qatgate PRIVATE ${CMAKE_DL_LIBS})" in text
    assert "LANGUAGES C CXX" in text
    instructions = [line for line in text.splitlines()
                    if "qatgate" in line and not line.lstrip().startswith("#")]
    assert instructions
    assert all("Qt" not in line for line in instructions), instructions


def test_the_packaged_gate_is_the_one_in_the_repository():
    """`build-filter` builds the copy inside the wheel. Editing one and not the
    other means the VM quietly builds the old gate."""
    packaged = (source_dir() / "qatgate.c").read_bytes()
    assert packaged == (ROOT / "native" / "qatgate.c").read_bytes()
    assert ((source_dir() / "CMakeLists.txt").read_bytes()
            == (ROOT / "native" / "CMakeLists.txt").read_bytes())


# --- the wrapper ------------------------------------------------------------

def wrapper_source() -> str:
    return (ROOT / "qat_recorder" / "resources"
            / "wrapper.sh").read_text(encoding="utf-8")


def test_the_wrapper_preloads_the_gate_and_stashes_the_rest():
    text = wrapper_source()
    assert 'export LD_PRELOAD="${QATREC_GATE}"' in text
    assert "export QATREC_PRELOAD" in text
    assert "QATREC_PID=$$" in text


def test_the_launching_shell_is_not_injected_when_it_is_a_script():
    """A launch script is not the application, and injecting into it does
    active harm.

    Qat's injector prints `OnUnload` to stdout from a destructor -- the symbol
    sits beside std::cout in libinjector.so. A shell forks for every `$(...)`,
    and a substitution made of builtins, which is how a script finds its own
    root, exits that fork without exec'ing anything: the destructor runs and
    the substitution captures the word. Every path built from that root is
    then wrong, and the application reports that its configuration files
    cannot be found.
    """
    text = wrapper_source()
    assert 'if [ "${QATREC_APP_IS_SCRIPT:-0}" != "1" ]; then' in text
    assert "QATREC_PID=$$" in text
    assert "OnUnload" in text, "the reason has to travel with the code"


def test_the_recorder_tells_the_wrapper_when_the_application_is_a_script():
    """Both halves, or the wrapper defaults to injecting and the bug returns."""
    controller = (ROOT / "qat_recorder" / "ui"
                  / "controller.py").read_text(encoding="utf-8")
    cli = (ROOT / "qat_recorder" / "cli.py").read_text(encoding="utf-8")
    for source in (controller, cli):
        assert 'os.environ["QATREC_APP_IS_SCRIPT"]' in source


def test_the_wrapper_can_say_what_it_launched_with():
    """An application that starts from a terminal and not from the recorder
    differs in exactly one of these."""
    text = wrapper_source()
    assert 'if [ "${QATREC_GATE_DEBUG:-0}" = "1" ]; then' in text
    assert "${PWD}" in text          # never $(pwd): see the file's own reasons
    assert ">&2" in text


def test_the_wrapper_still_works_without_a_gate():
    """An older filter build has no gate beside it. Preloading everything is
    what the recorder always did, and it is right for an application that is
    launched directly."""
    text = wrapper_source()
    assert 'export LD_PRELOAD="${LD_PRELOAD}:${QATREC_LIB}"' in text
    assert 'QATREC_NO_GATE' in text


def test_the_wrapper_uses_no_command_substitution_at_all():
    """It runs with the injector already preloaded, so every `$(...)` in it
    would capture the injector's chatter. This is the same bug the wrapper
    exists to keep out of the application's own scripts."""
    code = "\n".join(line for line in wrapper_source().splitlines()
                     if not line.lstrip().startswith("#"))
    assert "$(" not in code
    assert "`" not in code


@pytest.mark.skipif(os.name == "nt", reason="shell wrapper is Linux-only")
def test_the_wrapper_is_valid_shell():
    result = subprocess.run(["bash", "-n", str(ROOT / "qat_recorder" /
                                               "resources" / "wrapper.sh")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name == "nt", reason="shell wrapper is Linux-only")
def test_the_wrapper_hands_the_gate_the_libraries_it_replaced(tmp_path):
    gate = tmp_path / "libqatgate.so"
    gate.write_bytes(b"not really a library")
    probe = tmp_path / "probe.sh"
    probe.write_text('#!/bin/sh\necho "PRELOAD=[$LD_PRELOAD]"\n'
                     'echo "STASH=[$QATREC_PRELOAD]"\n'
                     'echo "PID=[$QATREC_PID]"\n', encoding="utf-8")
    probe.chmod(0o755)

    result = subprocess.run(
        ["bash", str(ROOT / "qat_recorder" / "resources" / "wrapper.sh")],
        env={**os.environ,
             "LD_PRELOAD": "/qat/libinjector.so",
             "QATREC_LIB": "/opt/libqatrec.so",
             "QATREC_GATE": str(gate),
             "QATREC_APP": str(probe)},
        capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
    assert f"PRELOAD=[{gate}]" in result.stdout
    assert "STASH=[/qat/libinjector.so:/opt/libqatrec.so]" in result.stdout
    assert "PID=[" in result.stdout and "PID=[]" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="shell wrapper is Linux-only")
def test_a_script_the_kernel_cannot_exec_still_leaves_the_wrapper(tmp_path):
    """A byte-order mark before `#!` makes exec fail with ENOEXEC, and bash
    then runs the script inside the wrapper -- where Qat's injector is still
    loaded, and every $(...) captures its OnUnload."""
    probe = tmp_path / "start_app.sh"
    probe.write_bytes(b"\xef\xbb\xbf#!/bin/sh\nps -p $$ -o command=\n")
    probe.chmod(0o755)

    result = subprocess.run(
        ["bash", str(ROOT / "qat_recorder" / "resources" / "wrapper.sh")],
        env={**os.environ, "QATREC_APP": str(probe),
             "QATREC_APP_IS_SCRIPT": "1"},
        capture_output=True, text=True, timeout=30)

    assert "wrapper.sh" not in result.stdout, result.stdout


@pytest.mark.skipif(os.name == "nt", reason="shell wrapper is Linux-only")
def test_an_application_run_as_root_keeps_its_injection(tmp_path):
    """sudo drops LD_PRELOAD whatever it is told, so a plain `sudo app` starts
    the application with nothing injected. env, after sudo, puts it back."""
    gate = tmp_path / "libqatgate.so"
    gate.write_bytes(b"not really a library")
    sudo = tmp_path / "sudo"
    sudo.write_text('#!/bin/sh\nfor a in "$@"; do echo "ARG=[$a]"; done\n',
                    encoding="utf-8")
    sudo.chmod(0o755)

    result = subprocess.run(
        ["bash", str(ROOT / "qat_recorder" / "resources" / "wrapper.sh"), "-x"],
        env={**os.environ,
             "PATH": f"{tmp_path}:{os.environ['PATH']}",
             "LD_PRELOAD": "/qat/libinjector.so",
             "QATREC_GATE": str(gate),
             "QATREC_LIB": "/opt/libqatrec.so",
             "QATREC_PORT": "4242",
             "QATREC_SUDO": "1",
             "QATREC_APP_IS_SCRIPT": "1",
             "QATREC_APP": str(tmp_path / "start_app.sh")},
        capture_output=True, text=True, timeout=30)

    args = [line[5:-1] for line in result.stdout.splitlines()
            if line.startswith("ARG=[")]
    assert args[:2] == ["-n", "env"], result.stdout + result.stderr
    assert f"LD_PRELOAD={gate}" in args
    assert "QATREC_PRELOAD=/qat/libinjector.so:/opt/libqatrec.so" in args
    assert "QATREC_PORT=4242" in args
    assert args[-2:] == [str(tmp_path / "start_app.sh"), "-x"]


# --- following the application ----------------------------------------------

class FakeContext:
    def __init__(self, pid):
        self.pid = pid
        self.return_code = None
        self.config_file = None
        self.port = None
        self.versioned = False

    def is_finished(self):
        return self.return_code is not None

    def init_comm(self, port, timeout=None):
        self.port = port

    def init_version_info(self):
        self.versioned = True


class FakeQat:
    """Enough of Qat to launch, and a log of how it was asked to."""

    def __init__(self, context, log=None):
        self.context = context
        self.log = log if log is not None else []
        self.registered = {}

    def start_application(self, name, args=None, detached=False):
        self.log.append(("start", name, detached))
        return self.context

    def close_application(self, context=None):
        self.log.append(("close", getattr(context, "pid", None)))
        return 0

    def register_application(self, name, path, args=None):
        self.log.append(("register", name, str(path)))
        self.registered[name] = str(path)
        return None

    def list_applications(self):
        return dict(self.registered)


def fake_time():
    state = {"now": 0.0}

    def clock():
        return state["now"]

    def sleep(seconds):
        state["now"] += seconds

    return clock, sleep


def script(tmp_path, name="start_app.sh"):
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexec ./app\n", encoding="utf-8")
    return str(path)


def binary(tmp_path, name="app"):
    path = tmp_path / name
    path.write_bytes(b"\x7fELF" + b"\x00" * 60)
    return str(path)


def test_a_binary_takes_qats_own_path_unchanged(tmp_path):
    """Nothing to improve, and nothing worth risking."""
    context = FakeContext(pid=100)
    qat = FakeQat(context)
    assert launch.start(qat, "app", app_path=binary(tmp_path)) is context
    assert qat.log == [("start", "app", False)]


def test_a_script_is_launched_detached_and_followed(tmp_path):
    """The port file is written by the child, under the child's pid."""
    folder = tmp_path / "temp"
    folder.mkdir()
    context = FakeContext(pid=100)
    qat = FakeQat(context)
    clock, tick = fake_time()

    written = []

    def sleep(seconds):
        tick(seconds)
        if not written:
            written.append(True)
            (folder / "qat-205.txt").write_text("54321", encoding="utf-8")

    got = launch.start(qat, "app", app_path=script(tmp_path), folder=folder,
                       parent={205: 100}.get, sleep=sleep, clock=clock)

    assert qat.log == [("start", "app", True)]
    assert got.port == 54321
    assert got.versioned
    assert got.qatrec_app_pid == 205
    assert got.config_file == str(folder / "qat-205.txt")


def test_a_script_that_execs_the_application_keeps_its_pid(tmp_path):
    """`exec ./app` replaces the shell, so the pid Qat started is the
    application after all and there is nothing to follow."""
    folder = tmp_path / "temp"
    folder.mkdir()
    (folder / "qat-100.txt").write_text("4242", encoding="utf-8")
    context = FakeContext(pid=100)
    clock, tick = fake_time()

    got = launch.start(FakeQat(context), "app", app_path=script(tmp_path),
                       folder=folder, parent=lambda pid: None,
                       sleep=tick, clock=clock)
    assert got.port == 4242
    assert got.qatrec_app_pid == 100


def test_somebody_elses_application_is_not_followed(tmp_path):
    """A port file that appears while we are waiting can belong to another
    session on the same machine. Connecting the recorder to somebody else's
    application is worse than not connecting at all."""
    folder = tmp_path / "temp"
    folder.mkdir()
    context = FakeContext(pid=100)
    clock, tick = fake_time()

    written = []

    def sleep(seconds):
        tick(seconds)
        if not written:
            written.append(True)
            (folder / "qat-999.txt").write_text("1234", encoding="utf-8")

    with pytest.raises(launch.LaunchError) as error:
        launch.start(FakeQat(context), "app", app_path=script(tmp_path),
                     folder=folder, timeout_ms=2000,
                     parent={999: 7}.get, sleep=sleep, clock=clock)
    assert "did not report a port" in str(error.value)
    assert context.port is None


def test_port_files_that_were_already_there_are_not_mistaken_for_ours(tmp_path):
    folder = tmp_path / "temp"
    folder.mkdir()
    (folder / "qat-205.txt").write_text("1111", encoding="utf-8")
    context = FakeContext(pid=100)
    clock, tick = fake_time()

    with pytest.raises(launch.LaunchError):
        launch.start(FakeQat(context), "app", app_path=script(tmp_path),
                     folder=folder, timeout_ms=1000,
                     parent={205: 100}.get, sleep=tick, clock=clock)


def test_an_application_that_dies_says_so_instead_of_waiting(tmp_path):
    folder = tmp_path / "temp"
    folder.mkdir()
    context = FakeContext(pid=100)
    clock, tick = fake_time()
    path = script(tmp_path)

    def sleep(seconds):
        tick(seconds)
        context.return_code = 127

    with pytest.raises(launch.LaunchError) as error:
        launch.start(FakeQat(context), "app", app_path=path, folder=folder,
                     parent=lambda pid: None, sleep=sleep, clock=clock)

    message = str(error.value)
    assert "exited (exit code 127)" in message
    assert "launch script" in message
    assert path in message


def test_a_parent_that_cannot_be_read_is_not_an_ancestor():
    assert launch.descends_from(5, 1, parent=lambda pid: None) is False
    assert launch.descends_from(5, 5, parent=lambda pid: None) is True


def test_a_cycle_in_the_process_table_does_not_hang():
    assert launch.descends_from(5, 77, parent={5: 6, 6: 5}.get) is False


def test_the_parent_of_a_process_with_brackets_in_its_name(tmp_path):
    """/proc/<pid>/stat puts the executable name in brackets and does not
    escape it, so the fields after it are counted from the last bracket."""
    stat = tmp_path / "stat"
    stat.write_text("205 (my (odd) app) S 100 205 205 0 -1 4194560",
                    encoding="utf-8")
    text = stat.read_text(encoding="utf-8")
    closing = text.rfind(")")
    assert int(text[closing + 1:].split()[1]) == 100


# --- closing ----------------------------------------------------------------

class FakeKiller:
    def __init__(self, log):
        self.log = log
        self.dead = False

    def __call__(self, pid, sig):
        if sig == 0:
            if self.dead:
                raise OSError("no such process")
            return None
        self.log.append(("kill", pid, sig))
        self.dead = True


def test_the_application_is_closed_before_the_script_that_started_it(tmp_path):
    """The other order leaves the application running: Qat owns the script's
    pid, not the application's, so killing the script is not enough."""
    log = []
    context = FakeContext(pid=100)
    context.qatrec_app_pid = 205
    qat = FakeQat(context, log=log)
    clock, tick = fake_time()

    launch.close(qat, context, sleep=tick, clock=clock, grace=1.0,
                 kill=FakeKiller(log))

    assert log == [("kill", 205, signal.SIGTERM), ("close", 100)]


def test_a_launch_that_fails_does_not_leave_the_application_running(tmp_path):
    """Observed live: the first attempt timed out, the application it had
    started stayed up, and the second attempt started a second copy. Neither
    was being recorded."""
    folder = tmp_path / "temp"
    folder.mkdir()
    log = []
    context = FakeContext(pid=100)
    context.kill = lambda: log.append(("kill", 100, "context"))
    clock, tick = fake_time()

    with pytest.raises(launch.LaunchError):
        launch.start(FakeQat(context, log=log), "app",
                     app_path=script(tmp_path), folder=folder, timeout_ms=500,
                     parent={205: 100, 206: 205}.get, listing=[100, 205, 206],
                     sleep=tick, clock=clock, kill=FakeKiller(log))

    killed = [entry for entry in log if entry[0] == "kill"]
    # The application and the daemon it started, then the launcher itself.
    assert [entry[1] for entry in killed] == [206, 205, 100]


def test_an_application_run_as_root_is_closed_through_sudo(monkeypatch):
    """Everything Qat would touch on close belongs to root: the application,
    sudo itself, and the port file the application's server wrote."""
    log = []
    ran = []
    context = FakeContext(pid=100)
    context.qatrec_app_pid = 205
    context.config_file = "/tmp/qat-205.txt"
    qat = FakeQat(context, log=log)
    clock, tick = fake_time()
    monkeypatch.setenv("QATREC_SUDO", "1")
    monkeypatch.setattr(launch.subprocess, "run",
                        lambda command, **_: ran.append(command))

    launch.close(qat, context, sleep=tick, clock=clock, grace=1.0,
                 kill=FakeKiller(log))

    assert log == [("kill", 205, signal.SIGTERM), ("kill", 100, signal.SIGTERM),
                   ("close", 100)]
    assert ran == [["sudo", "-n", "rm", "-f", "/tmp/qat-205.txt"]]


def test_closing_an_ordinary_application_is_just_closing_it():
    log = []
    context = FakeContext(pid=100)
    qat = FakeQat(context, log=log)
    launch.close(qat, context, kill=FakeKiller(log))
    assert log == [("close", 100)]


# --- replaying --------------------------------------------------------------

def test_replaying_a_script_registers_the_wrapper_not_the_script(tmp_path,
                                                                 monkeypatch):
    gate = tmp_path / "libqatgate.so"
    gate.write_bytes(b"gate")
    monkeypatch.setenv("QATREC_GATE", str(gate))
    monkeypatch.setenv("QATREC_LIB", "/opt/libqatrec.so")
    monkeypatch.setenv("QATREC_PORT", "5000")

    path = script(tmp_path)
    qat = FakeQat(FakeContext(pid=1))
    launch.register_for_replay(qat, "app", path, wrapper="/opt/wrapper.sh")

    assert qat.registered["app"] == "/opt/wrapper.sh"
    assert os.environ["QATREC_APP"] == path
    assert os.environ["QATREC_GATE"] == str(gate)
    # Replay records nothing, so the event filter has no business being loaded.
    assert "QATREC_LIB" not in os.environ
    assert "QATREC_PORT" not in os.environ


def test_replaying_a_binary_registers_the_binary(tmp_path):
    path = binary(tmp_path)
    qat = FakeQat(FakeContext(pid=1))
    launch.register_for_replay(qat, "app", path)
    assert qat.registered["app"] == path


def test_the_generated_test_launches_the_same_way_the_recording_did():
    from qat_recorder.emit import emit_python
    from qat_recorder.ir import Action, ActionKind, Recording, Target

    recording = Recording(app="spine")
    recording.meta["app_path"] = "/opt/spine/bin/start_spine.sh"
    recording.add(Action(ActionKind.LAUNCH, args={"app": "spine"}))
    recording.add(Action(ActionKind.CLICK,
                         target=Target(definition={"objectName": "ok"},
                                       label="ok")))
    source = emit_python(recording)

    assert "context = launch_application(APP_NAME, APP_PATH)" in source
    assert "close_application(context)" in source
    assert "launch.register_for_replay(qat, name, path)" in source
    # Still runnable where the recorder is not installed beside the test.
    assert "except ImportError:" in source
    compile(source, "generated.py", "exec")


# --- the Qt the application actually loads ----------------------------------

def bundled_app(tmp_path, soname="libQt6Core.so.6"):
    """An application laid out the way a product that ships its own Qt is:
    `bin/start_app.sh` with `lib/Qt/lib/libQt6Core.so.6` beside it."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "lib" / "Qt" / "lib").mkdir(parents=True)
    (tmp_path / "lib" / "Qt" / "lib" / soname).write_bytes(b"not really Qt")
    app = tmp_path / "bin" / "start_app.sh"
    app.write_text("#!/bin/sh\nexec ./app\n", encoding="utf-8")
    return str(app)


def test_the_qt_an_application_carries_is_found_beside_it(tmp_path):
    assert launch.bundled_qt_majors(bundled_app(tmp_path)) == {6}


def test_an_application_that_carries_nothing_has_no_opinion(tmp_path):
    assert launch.bundled_qt_majors(script(tmp_path)) == set()


def test_the_search_does_not_leave_the_products_own_tree(tmp_path):
    """`/usr/bin/app` has `/usr` above it. Searching that for a bundled Qt is
    wrong and slow, and it cost this function a test run that never finished
    before the guard existed."""
    assert launch._bundle_roots(Path("/usr/bin")) == []
    assert launch._bundle_roots(Path("/opt")) == []

    (tmp_path / "bin").mkdir()
    roots = launch._bundle_roots(tmp_path / "bin")
    assert roots == [tmp_path / "bin", tmp_path]


def test_an_application_not_in_a_bin_directory_looks_no_higher(tmp_path):
    """Only the bin/ layout says the directory above is the same product."""
    (tmp_path / "product").mkdir()
    (tmp_path / "libQt6Core.so.6").write_bytes(b"somebody else's Qt")
    app = tmp_path / "product" / "app.sh"
    app.write_text("#!/bin/sh\n", encoding="utf-8")
    assert launch.bundled_qt_majors(str(app)) == set()


def test_an_application_that_does_not_exist_is_not_searched_for(tmp_path):
    """Path("").resolve() is the working directory; walking it is how a test
    run went from two seconds to never finishing."""
    assert launch.bundled_qt_majors("") == set()
    assert launch.bundled_qt_majors(str(tmp_path / "missing")) == set()


def test_a_filter_built_for_the_wrong_qt_is_reported_before_recording(tmp_path):
    """It loads without complaining and then sees none of the application's
    widgets -- a session where nothing is recorded and nothing says why."""
    app = bundled_app(tmp_path)
    warning = launch.qt_mismatch(app, "/home/x/qatrec-filter/libqatrec.5.15.so")
    assert "built against Qt 5" in warning
    assert "Qt 6" in warning
    assert "qt6-base-dev" in warning
    # The message is composed wherever the panel runs and read wherever the
    # application does, so the path it hands back is the one it was given.
    assert "build-filter --out /home/x/qatrec-filter --qt 6" in warning


def test_a_matching_filter_says_nothing(tmp_path):
    app = bundled_app(tmp_path)
    assert launch.qt_mismatch(app, "/x/libqatrec.6.2.so") == ""
    # Binary compatibility is within a major version, so 6.2 against 6.8 is
    # fine and only the major is compared.
    assert launch.qt_mismatch(app, "/x/libqatrec.6.8.so") == ""


def test_nothing_is_claimed_when_nothing_is_known(tmp_path):
    assert launch.qt_mismatch(script(tmp_path), "/x/libqatrec.5.15.so") == ""
    assert launch.qt_mismatch(bundled_app(tmp_path), "") == ""
    assert launch.qt_mismatch("", "/x/libqatrec.5.15.so") == ""


def test_the_exact_qt_an_application_carries_is_read_from_its_file_name(tmp_path):
    """`libQt6Core.so.6` is a symlink; `libQt6Core.so.6.8.6` is the answer."""
    app = bundled_app(tmp_path)
    (tmp_path / "lib" / "Qt" / "lib" / "libQt6Core.so.6.8.6").write_bytes(b"x")
    assert launch.bundled_qt_version(app) == (6, 8, 6)


def test_a_filter_built_against_a_newer_qt_minor_is_reported(tmp_path):
    """The direction matters. 6.2 inside 6.8.6 is what Qt guarantees; 6.10
    inside 6.8.6 can reference symbols that application's Qt does not have, and
    then the newest Qt installed on the machine decides whether recording
    works."""
    app = bundled_app(tmp_path)
    (tmp_path / "lib" / "Qt" / "lib" / "libQt6Core.so.6.8.6").write_bytes(b"x")

    warning = launch.qt_mismatch(app, "/x/libqatrec.6.10.so")
    assert "built against Qt 6.10" in warning
    assert "carries Qt 6.8.6" in warning
    assert "forward, not backward" in warning

    assert launch.qt_mismatch(app, "/x/libqatrec.6.2.so") == ""
    assert launch.qt_mismatch(app, "/x/libqatrec.6.8.so") == ""


def test_no_version_no_claim(tmp_path):
    """Without the versioned file there is nothing to compare, and a guess
    about somebody's build is worse than silence."""
    app = bundled_app(tmp_path)
    assert launch.bundled_qt_version(app) is None
    assert launch.qt_mismatch(app, "/x/libqatrec.6.10.so") == ""


# --- what a session cannot start without ------------------------------------

def test_a_filter_that_is_not_there_is_said_before_anything_starts(tmp_path):
    """It fails five layers down otherwise: one line of the application's
    standard error, while the panel says recording and no event arrives."""
    folder = tmp_path / "qatrec-filter"
    folder.mkdir()
    (folder / "libqatrec.6.2.so").write_bytes(b"the one that is there")

    missing = launch.missing_pieces("", str(folder / "libqatrec.6.10.so"))
    assert "libqatrec.6.10.so is not there" in missing
    # And what is, because that is the answer.
    assert "libqatrec.6.2.so" in missing


def test_an_empty_build_directory_says_that_instead(tmp_path):
    folder = tmp_path / "qatrec-filter"
    folder.mkdir()
    assert "no filter at all" in launch.missing_pieces(
        "", str(folder / "libqatrec.6.2.so"))


def test_no_filter_at_all_is_a_complete_sentence():
    assert "build-filter" in launch.missing_pieces("/some/app", "")


def test_an_application_that_is_not_there_is_reported_too(tmp_path):
    lib = tmp_path / "libqatrec.6.2.so"
    lib.write_bytes(b"filter")
    assert "is not there" in launch.missing_pieces(str(tmp_path / "gone"),
                                                   str(lib))
    assert launch.missing_pieces(str(lib), str(lib)) == ""


# --- building the right filter ----------------------------------------------

def test_a_rebuild_does_not_inherit_the_previous_builds_qt(tmp_path):
    """CMake caches which Qt it found. Installing the Qt 6 headers and building
    again in the same directory therefore built Qt 5 again, and the stale
    library left beside the new one sorts first -- so the path reported was the
    old one too."""
    from qat_recorder.native import discard_previous

    (tmp_path / "CMakeCache.txt").write_text("Qt5_DIR:PATH=/usr/lib/cmake/Qt5",
                                             encoding="utf-8")
    (tmp_path / "CMakeFiles").mkdir()
    (tmp_path / "CMakeFiles" / "junk").write_text("x", encoding="utf-8")
    (tmp_path / "libqatrec.5.15.so").write_bytes(b"old filter")

    discard_previous(tmp_path)

    assert not (tmp_path / "CMakeCache.txt").exists()
    assert not (tmp_path / "CMakeFiles").exists()
    # The filter that is there stays there until a new one exists: a build that
    # fails must not leave the machine with no filter and the recorder pointed
    # at a path that no longer resolves.
    assert (tmp_path / "libqatrec.5.15.so").exists()


def test_the_superseded_filter_goes_only_once_the_new_one_is_built(tmp_path):
    from qat_recorder.native import discard_superseded

    old = tmp_path / "libqatrec.5.15.so"
    new = tmp_path / "libqatrec.6.2.so"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    (tmp_path / "libqatgate.so").write_bytes(b"gate")

    discard_superseded(tmp_path, keep=new)

    assert new.exists()
    assert not old.exists()
    # The gate belongs to whichever filter is there; it is not Qt-specific.
    assert (tmp_path / "libqatgate.so").exists()


def test_discarding_is_fine_in_a_directory_that_has_nothing_in_it(tmp_path):
    from qat_recorder.native import discard_previous, discard_superseded

    discard_previous(tmp_path)                                # must not raise
    discard_superseded(tmp_path, keep=tmp_path / "nothing")   # nor this


def test_the_build_can_be_told_which_qt_to_use():
    """The application's Qt, not this machine's."""
    text = (ROOT / "native" / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "QATREC_QT_MAJOR" in text
    assert "find_package(Qt${QATREC_QT_MAJOR} REQUIRED COMPONENTS Core Gui)" in text


# --- finding the gate -------------------------------------------------------

def test_the_gate_is_found_beside_the_filter(tmp_path):
    (tmp_path / "libqatrec.5.15.so").write_bytes(b"filter")
    (tmp_path / "libqatgate.so").write_bytes(b"gate")
    found = launch.gate_for(str(tmp_path / "libqatrec.5.15.so"))
    assert found == str((tmp_path / "libqatgate.so").resolve())


def test_the_gate_is_only_used_where_something_needs_it(tmp_path):
    """An application Qat starts directly gets the injector in its own
    LD_PRELOAD, the way every recording so far has had it. Nothing to gain by
    routing that through a gate, and a change with nothing to gain is a
    regression waiting to happen."""
    (tmp_path / "libqatgate.so").write_bytes(b"gate")
    lib = tmp_path / "libqatrec.5.15.so"
    lib.write_bytes(b"filter")

    assert launch.gate_env(script(tmp_path), str(lib))
    assert launch.gate_env(binary(tmp_path), str(lib)) is None


def test_an_older_build_has_no_gate_beside_the_filter(tmp_path):
    (tmp_path / "libqatrec.5.15.so").write_bytes(b"filter")
    assert launch.gate_for(str(tmp_path / "libqatrec.5.15.so")) is None
    assert launch.gate_for("") is None


def test_a_script_is_recognised_by_what_is_in_it(tmp_path):
    assert launch.is_script(script(tmp_path)) is True
    assert launch.is_script(binary(tmp_path)) is False
    assert launch.is_script(str(tmp_path / "missing")) is False
    assert launch.is_script("") is False
