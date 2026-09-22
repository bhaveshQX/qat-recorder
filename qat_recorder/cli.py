# -*- coding: utf-8 -*-
"""
Command line entry point.

    python -m qat_recorder audit <registered-app-name>
    python -m qat_recorder audit --launch <executable> [args...]

Runs the nameability audit against a live application and prints the report.
Exit code is 1 if any object could not be named durably, so it can gate CI.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path


def _cmd_audit(args) -> int:
    try:
        import qat
    except ImportError:
        print("qat is not installed in this environment", file=sys.stderr)
        return 2

    from qat_recorder.audit import audit
    from qat_recorder.backend import QatBackend
    from qat_recorder.ir import Robustness

    from qat_recorder import launch

    temporary = False
    app_name = args.app
    if args.launch:
        app_name = "_qat_recorder_audit"
        # Through the recorder's launcher, because --launch may well be a shell
        # script: see qat_recorder/launch.py for what that costs otherwise.
        launch.register_for_replay(qat, app_name, args.launch,
                                   args=" ".join(args.args))
        temporary = True

    # Qat locks the application's UI when it starts a session, so that stray
    # human input cannot corrupt a running test. During an audit that is
    # actively unhelpful: the application looks frozen, and the operator cannot
    # navigate to the screen they actually want scanned.
    try:
        qat.test_settings.Settings.lock_ui = "never"
    except AttributeError:
        pass

    context = launch.start(qat, app_name, app_path=args.launch)
    try:
        if args.pause:
            print("\nApplication is running and responsive. Navigate to the "
                  "screen you want scanned,\nthen press Enter here to audit it.")
            try:
                input()
            except EOFError:
                pass
        report = audit(QatBackend(qat), limit=args.limit,
                       include_internal=args.all)
        print(report.render())
    finally:
        try:
            launch.close(qat, context)
        finally:
            if temporary:
                qat.unregister_application(app_name)

    threshold = Robustness(args.fail_on)
    offenders = report.problems(threshold)
    if offenders:
        print(f"\n{len(offenders)} object(s) at or below "
              f"'{threshold.value}' durability", file=sys.stderr)
        return 1
    return 0


def _cmd_record_remote(args) -> int:
    """Record on a VM through its agent. Nothing but the artifacts comes back."""
    import time

    from qat_recorder.agent.registry import Registry, client_for
    from qat_recorder.agent.remote import RemoteRecorderController

    try:
        host = Registry.load().resolve(args.agent)
        controller = RemoteRecorderController(
            client_for(host), app_path=args.app, lib_path=args.lib,
            app_name=args.name or Path(args.app).name)
        controller.start()
    except Exception as error:                                # noqa: BLE001
        print(f"could not start on {args.agent}: {error}", file=sys.stderr)
        return 1

    print(f"recording on {host.name} ({host.address}) for {args.seconds}s "
          "— interact with the application there")
    deadline = time.time() + args.seconds
    try:
        while time.time() < deadline:
            controller.poll()
            time.sleep(0.2)
        controller.poll()
        controller.stop()
        written = controller.save(args.out)
    except KeyboardInterrupt:
        controller.stop()
        written = controller.save(args.out)
    finally:
        controller.release()

    summary = controller.summary()
    print(f"{summary['actions']} action(s), {summary.get('unresolved', 0)} "
          "unresolved")
    print(f"written to {args.out}")
    for path in written:
        print(f"  {path}")
    if summary.get("unresolved"):
        # The reasons live on the VM's side of the connection; they come back
        # with the artifacts rather than over the event stream.
        print("\n  what was dropped, and why: "
              f"{Path(args.out) / 'unresolved.txt'}", file=sys.stderr)
    return 0


def pump(receiver, session, seconds: float, interval: float = 0.05,
         clock=None, sleep=None) -> int:
    """Feed events into `session` as they arrive. Returns how many were fed.

    Resolution happens here, and it has to happen *while the session is
    running*. An object can only be named while it exists, and everything a
    dialog contains is destroyed when the dialog closes -- so a run that
    recorded for thirty seconds and only then resolved lost every click inside
    every dialog, including the OK button that closed one. The replay opened
    that dialog and never closed it, and every step afterwards ran against a
    screen the recording had never seen.

    The cost is a few Qat round trips per event, at human speed, against an
    application that is idle between clicks.
    """
    import time as _time

    clock = clock or _time.time
    sleep = sleep or _time.sleep

    captured = 0
    deadline = clock() + seconds
    while clock() < deadline:
        events = receiver.drain()
        if events:
            captured += len(events)
            session.feed_all(events)
        else:
            # Grouping waits for the next event to know an interaction is
            # finished. Nothing is coming, so close it on age instead.
            session.flush_stale()
        sleep(interval)

    # Whatever was still in flight when the clock ran out.
    events = receiver.drain(timeout=1.0)
    captured += len(events)
    session.feed_all(events)
    return captured


def _cmd_record(args) -> int:
    """Record a session and write the IR plus generated code."""
    if getattr(args, "agent", ""):
        return _cmd_record_remote(args)

    try:
        import qat
    except ImportError:
        print("qat is not installed in this environment", file=sys.stderr)
        return 2

    import os
    import time

    from qat_recorder.backend import QatBackend
    from qat_recorder.capture import CaptureSession
    from qat_recorder.emit import emit_gherkin, emit_python, emit_steps
    from qat_recorder.events import EventReceiver

    from qat_recorder.ui.controller import default_wrapper
    try:
        wrapper = default_wrapper()
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return 2

    receiver = EventReceiver(port=0)
    receiver.start()
    os.environ["QATREC_PORT"] = str(receiver.actual_port)
    os.environ["QATREC_LIB"] = args.lib
    os.environ["QATREC_APP"] = args.app

    # The gate decides which processes get the injector. Without it, an
    # application started by a launch script never starts at all: see
    # native/qatgate.c.
    from qat_recorder.launch import close, gate_env, qt_mismatch, start

    warning = qt_mismatch(args.app, args.lib)
    if warning:
        print(warning, file=sys.stderr)

    gate = gate_env(args.app, args.lib)
    if gate:
        os.environ["QATREC_GATE"] = gate
    else:
        os.environ.pop("QATREC_GATE", None)

    # Qat locks the application's UI during a test run so stray input cannot
    # corrupt it. While recording that is backwards -- it would block the very
    # input being captured.
    qat.test_settings.Settings.lock_ui = "never"

    name = "_qat_recorder_session"
    qat.register_application(name, str(wrapper), "")
    # Not qat.start_application: when the application is started by a launch
    # script it is a child of the process Qat launched, and Qat waits on the
    # wrong pid -- "Abort: app terminated" before anything was recorded.
    context = start(qat, name, app_path=args.app)
    print(f"recording for {args.seconds}s -- interact with the application now")

    # The name identifies the application in generated code; the path lets that
    # code register it. Defaulting the name to the executable's basename rather
    # than its full path keeps the generated test readable.
    session = CaptureSession(
        QatBackend(qat),
        app_name=args.name or Path(args.app).name,
        app_path=args.app)

    try:
        captured = pump(receiver, session, args.seconds)
        print(f"captured {captured} raw event(s)")
        recording = session.finish()
    finally:
        receiver.stop()
        try:
            # close(), not qat.close_application(): where the application was
            # started by a script, Qat owns the script and closing it would
            # leave the application running.
            close(qat, context)
        finally:
            qat.unregister_application(name)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "recording.json").write_text(recording.dumps(), encoding="utf-8")
    (out / "test_recorded.py").write_text(emit_python(recording), encoding="utf-8")
    (out / "recorded.feature").write_text(emit_gherkin(recording), encoding="utf-8")
    (out / "steps.py").write_text(emit_steps(recording), encoding="utf-8")

    from qat_recorder.emit.python import emit_object_map
    (out / "objects.json").write_text(emit_object_map(recording), encoding="utf-8")

    if session.filter_features and "itemRow" not in session.filter_features:
        print("\n  the event filter here is older than this recorder: it does "
              "not report\n  which row was clicked, so rows of lists, trees and "
              "tables will be lost.\n  Rebuild it with: python -m qat_recorder "
              "build-filter\n", file=sys.stderr)
    elif not session.filter_features:
        print("\n  the event filter here did not say what it supports, which "
              "means it\n  predates this recorder. Rebuild it with: python -m "
              "qat_recorder build-filter\n", file=sys.stderr)

    print(f"{len(recording.actions)} action(s), {session.unresolved} unresolved")
    print(f"written to {out}")

    _report_dropped(session, out)

    for action in recording.weakest_targets():
        print(f"  {action.target.robustness.value:<10} {action.target.label}",
              file=sys.stderr)
    return 0


def _cmd_replay(args) -> int:
    """Run a recorded test where the application is.

    With --agent, on that host: the recording was made there, the application
    lives there, and a recorded test launches the application itself. Without
    one, in the directory given.
    """
    if args.agent:
        from qat_recorder.agent.registry import Registry, client_for

        try:
            host = Registry.load().resolve(args.agent)
            client = client_for(host)
            session = client.current()
            if not session:
                print(f"{args.agent} has no session to replay; record one first",
                      file=sys.stderr)
                return 1
            print(f"replaying on {host.name} ({session.get('directory', '?')})")
            result = client.replay(session["session_id"], args.timeout)
        except Exception as error:                            # noqa: BLE001
            print(f"{args.agent}: {error}", file=sys.stderr)
            return 1
    else:
        from qat_recorder.replay import run_pytest

        result = run_pytest(args.directory, timeout=args.timeout or 300.0)

    print(result.get("output") or "")
    print("replay passed" if result.get("ok") else
          f"replay FAILED (exit code {result.get('exit_code')})")
    return 0 if result.get("ok") else 1


def _cmd_tests(args) -> int:
    """The saved suite: list it, or run one test or all of them.

    Runs where the application is — on the agent's host with --agent, here
    without one — because a recorded test launches the application itself.
    """
    remote = None
    if args.agent:
        from qat_recorder.agent.registry import Registry, client_for
        try:
            remote = client_for(Registry.load().resolve(args.agent))
        except Exception as error:                            # noqa: BLE001
            print(f"{args.agent}: {error}", file=sys.stderr)
            return 1

    if remote is not None:
        listing = remote.tests(args.app)
        cases = listing.get("tests", [])
        where = f"{args.agent}:{listing.get('root', '?')}"
    else:
        from qat_recorder.library import TestLibrary
        library = TestLibrary()
        cases = [case.to_dict() for case in library.list(args.app)]
        where = str(library.root)

    if args.tests_command == "list":
        if not cases:
            print(f"no saved tests in {where}")
            return 0
        print(f"{len(cases)} test(s) in {where}\n")
        for case in cases:
            print(f"  {case['id']:<40} {case['steps']:>3} steps  "
                  f"{(case.get('created') or '')[:10]}")
            if case.get("needs_review"):
                print(f"  {'':<40} {case['needs_review']} step(s) need review")
        return 0

    chosen = ([case for case in cases if case["id"] == args.test]
              if getattr(args, "test", "") else cases)
    if not chosen:
        print(f"nothing to run in {where}", file=sys.stderr)
        return 1

    # One after another, never in parallel: each drives the real UI of a real
    # application, and they would be fighting over one screen.
    failures = 0
    for index, case in enumerate(chosen, start=1):
        print(f"\n[{index}/{len(chosen)}] {case['id']}")
        if remote is not None:
            result = remote.run_test(case["id"], args.timeout)
        else:
            from qat_recorder.replay import run_pytest
            result = run_pytest(case["directory"], timeout=args.timeout or 300.0)
        if result.get("ok"):
            print("  passed")
        else:
            failures += 1
            print("  FAILED")
            print(result.get("output") or "")

    print(f"\n{len(chosen) - failures}/{len(chosen)} passed")
    return 1 if failures else 0


def _report_dropped(session, out: Path) -> None:
    """Say what did not make it into the recording, and why.

    A bare "19 unresolved" is a mystery, and the person who can say which of
    those nineteen mattered is the one who just did the clicking. Identical
    reasons are collapsed -- dropping the same control twenty times is one
    problem, not twenty -- and the full list is written out to be read
    afterwards.
    """
    if not session.failures:
        return

    counted = Counter(session.failures)
    print(f"\n{len(session.failures)} event(s) could not be recorded:",
          file=sys.stderr)
    for reason, count in counted.most_common(8):
        print(f"  {reason}" + (f"   (x{count})" if count > 1 else ""),
              file=sys.stderr)
    if len(counted) > 8:
        print(f"  ... and {len(counted) - 8} more kinds", file=sys.stderr)

    from qat_recorder.capture import dropped_report

    report = out / "unresolved.txt"
    report.write_text(dropped_report(session.failures), encoding="utf-8")
    print(f"  full list: {report}", file=sys.stderr)


def _cmd_emit(args) -> int:
    """Re-generate code from an existing recording, without re-recording."""

    from qat_recorder.emit import emit_gherkin, emit_python, emit_steps
    from qat_recorder.ir import Recording

    recording = Recording.loads(Path(args.recording).read_text(encoding="utf-8"))
    problems = recording.validate()
    if problems:
        print("invalid recording: " + "; ".join(problems), file=sys.stderr)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    source = Path(args.recording).name

    if args.format in ("python", "both"):
        (out / "test_recorded.py").write_text(
            emit_python(recording, source=source), encoding="utf-8")
    if args.format in ("gherkin", "both"):
        (out / "recorded.feature").write_text(
            emit_gherkin(recording, source=source), encoding="utf-8")
        (out / "steps.py").write_text(
            emit_steps(recording, source=source), encoding="utf-8")

    print(f"generated {args.format} from {args.recording} into {out}")
    return 0


def _cmd_build_filter(args) -> int:
    """Compile the event filter from the source shipped inside this package."""
    from qat_recorder.launch import bundled_qt_majors
    from qat_recorder.native import BuildError, build, gate_of, source_dir

    qt_major = int(args.qt) if args.qt and args.qt != "auto" else None
    if qt_major is None and getattr(args, "app", ""):
        # The application knows better than this machine does. One that ships
        # its own Qt is not the Qt installed here, and the filter has to match
        # the one the application will actually load.
        majors = bundled_qt_majors(args.app)
        if len(majors) == 1:
            qt_major = majors.pop()
            print(f"{Path(args.app).name} carries Qt {qt_major}; building "
                  "against that")

    try:
        print(f"source: {source_dir()}")
        library = build(args.out, with_test_app=args.with_test_app,
                        jobs=args.jobs, verbose=args.verbose,
                        qt_major=qt_major)
    except BuildError as error:
        print(f"\n{error}", file=sys.stderr)
        return 1

    print(f"\nbuilt: {library}")
    gate = gate_of(library)
    if gate:
        # Nothing to pass on the command line: it is found beside the filter.
        # Printed because an application started by a launch script does not
        # record without it.
        print(f"       {gate}  (used automatically)")
    print(f"\nbuilt against Qt {library.name.split('.')[1]}; the application "
          "must use the same major version")
    print("\nrecord with it:")
    print(f"  python -m qat_recorder record --lib {library} \\")
    print("      --app /path/to/your/app --seconds 60 --out ./recorded")
    return 0


def _cmd_qat_servers(args) -> int:
    """Report -- and optionally repair -- Qat's prebuilt server libraries.

    The failure this exists for is silent in every log but the application's
    own: injection reaches the application, finds its Qt, and then cannot load
    the server built for that Qt because it needs a newer glibc than this
    machine has. See qat_recorder/servers.py.
    """
    from qat_recorder import servers

    folder = servers.qat_bin_dir()
    if folder is None:
        print("qat is not installed in this environment", file=sys.stderr)
        return 2

    if args.restore:
        restored = servers.restore(folder)
        if not restored:
            print("nothing to restore: no originals are being kept")
            return 0
        for path in restored:
            print(f"restored {path}")
        return 0

    print(servers.report(folder))

    if not args.fix:
        return 0

    machine = servers.Machine.here()
    plan = servers.repairs(servers.survey(folder), machine)
    if not plan:
        return 0
    try:
        done = servers.apply_repairs(plan)
    except OSError as error:
        print(f"\ncould not write to {folder}: {error}", file=sys.stderr)
        print("the environment may be read-only, or owned by another user",
              file=sys.stderr)
        return 1
    print()
    for target, standin in done:
        print(f"{target.name} <- {standin.name}  "
              f"(original kept as {target.name}{servers.BACKUP_SUFFIX})")
    print("\nRecord again; the injection should now get past "
          '"Failed to load Qat server".')
    return 0


def _cmd_hosts(args) -> int:
    from qat_recorder.agent.registry import Host, Registry, client_for

    registry = Registry.load()

    if args.hosts_command == "list":
        if not registry.hosts:
            print(f"no hosts registered ({registry.path})")
            return 0
        for name in registry.names():
            host = registry.hosts[name]
            flags = []
            if host.insecure_plaintext:
                flags.append("PLAINTEXT")
            if not host.fingerprint and not host.ca_file:
                flags.append("UNVERIFIED")
            suffix = ("  [" + ", ".join(flags) + "]") if flags else ""
            print(f"  {name:<20} {host.address:<24}{suffix}")
            if host.note:
                print(f"  {'':<20} {host.note}")
        return 0

    if args.hosts_command == "add":
        address, _, port = args.address.rpartition(":")
        host = Host(
            name=args.name,
            host=address or args.address,
            port=int(port) if port.isdigit() else 8765,
            fingerprint=(args.fingerprint or "").replace(":", "").lower(),
            ca_file=args.ca_file or "",
            token_file=args.token_file or "",
            token_env=args.token_env or "",
            ngrok_url=args.ngrok_url or "",
            insecure_plaintext=args.insecure_plaintext,
            note=args.note or "",
        )
        problems = host.validate()
        for problem in problems:
            print(f"warning: {problem}", file=sys.stderr)
        registry.add(host)
        path = registry.save()
        print(f"added {host.name} -> {host.address} ({path})")
        return 1 if problems else 0

    if args.hosts_command == "remove":
        if not registry.remove(args.name):
            print(f"no host named {args.name!r}", file=sys.stderr)
            return 1
        registry.save()
        print(f"removed {args.name}")
        return 0

    if args.hosts_command == "check":
        try:
            host = registry.resolve(args.name)
            client = client_for(host)
            health = client.health()
        except Exception as error:                            # noqa: BLE001
            print(f"{args.name}: {error}", file=sys.stderr)
            return 1
        busy = health.get("session")
        state = (f"in use by {busy['owner']} since {busy['started_at']}"
                 if busy else "free")
        print(f"{args.name}: {health.get('host')} — {state}")
        return 0

    return 2


def _cmd_panel(args) -> int:
    try:
        from qat_recorder.ui.panel import main as panel_main
    except ImportError as error:
        print(f"the control panel needs PySide6: {error}", file=sys.stderr)
        return 2
    argv = ["--lib", args.lib, "--app", args.app, "--name", args.name]
    if getattr(args, "agent", ""):
        argv += ["--agent", args.agent]
    return panel_main(argv)


def _cmd_web_panel(args) -> int:
    """Start a local web server that serves the React UI.

    The UI itself lets the user type a VM address or ngrok URL and connect.
    No qat, no registry, no tokens needed on this machine.
    """
    import webbrowser

    try:
        import uvicorn
        from fastapi import FastAPI
        from fastapi.staticfiles import StaticFiles
        from fastapi.responses import FileResponse
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError as error:
        print(f"web panel needs fastapi and uvicorn: pip install fastapi uvicorn", file=sys.stderr)
        return 2

    static_dir = Path(__file__).parent / "web" / "static"
    if not (static_dir / "index.html").exists():
        print(f"UI not built — run 'npm run build' inside qat_recorder/web/frontend/", file=sys.stderr)
        return 2

    app = FastAPI(docs_url=None, redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.get("/")
    async def index():
        return FileResponse(str(static_dir / "index.html"))

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    port = args.port
    url = f"http://127.0.0.1:{port}"
    print(f"QAT Recorder Web Panel -> {url}")
    webbrowser.open(url)

    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qat_recorder")
    sub = parser.add_subparsers(dest="command", required=True)

    audit_parser = sub.add_parser(
        "audit", help="report how durably the application's objects can be named")
    audit_parser.add_argument(
        "app", nargs="?", help="name of an application already registered with Qat")
    audit_parser.add_argument(
        "--launch", metavar="EXE",
        help="register and launch this executable instead")
    audit_parser.add_argument(
        "args", nargs="*", default=[], help="arguments passed to --launch")
    audit_parser.add_argument("--limit", type=int, default=5000)
    audit_parser.add_argument(
        "--all", action="store_true",
        help="also grade internal objects (layouts, models, scrollbars). Off by "
             "default: nobody clicks a layout, and grading them buries the real "
             "controls")
    audit_parser.add_argument(
        "--pause", action="store_true",
        help="keep the application open and wait for Enter before scanning, so "
             "you can navigate to the screen you want audited")
    audit_parser.add_argument(
        "--fail-on", default="fragile",
        choices=["moderate", "weak", "fragile", "unresolved"],
        help="exit non-zero if any object is at or below this durability")
    audit_parser.set_defaults(func=_cmd_audit)

    record_parser = sub.add_parser(
        "record", help="record a session and generate a test from it")
    record_parser.add_argument("--lib", required=True,
                               help="path to libqatrec.*.so")
    record_parser.add_argument("--app", required=True,
                               help="path to the application binary")
    record_parser.add_argument("--seconds", type=float, default=30.0,
                               help="how long to record for (default 30)")
    record_parser.add_argument("--name", default="",
                               help="application name used in generated code")
    record_parser.add_argument("--out", default="recorded",
                               help="output directory (default ./recorded)")
    record_parser.add_argument("--agent", default="",
                               help="record on a remote host: a registered name "
                                    "or HOST[:PORT]. --lib and --app then refer "
                                    "to that machine")
    record_parser.set_defaults(func=_cmd_record)

    emit_parser = sub.add_parser(
        "emit", help="regenerate code from an existing recording")
    emit_parser.add_argument("recording", help="path to recording.json")
    emit_parser.add_argument("--format", default="both",
                             choices=["python", "gherkin", "both"])
    emit_parser.add_argument("--out", default="recorded")
    emit_parser.set_defaults(func=_cmd_emit)

    replay_parser = sub.add_parser(
        "replay", help="run a recorded test where the application is")
    replay_parser.add_argument(
        "directory", nargs="?", default="recorded",
        help="folder holding test_recorded.py (ignored with --agent)")
    replay_parser.add_argument(
        "--agent", default="",
        help="registered host to replay on. The recording was made there and "
             "the application lives there, so that is where it can run")
    replay_parser.add_argument("--timeout", type=float, default=0.0)
    replay_parser.set_defaults(func=_cmd_replay)

    tests_parser = sub.add_parser(
        "tests", help="the saved suite: list it, or run it")
    tests_sub = tests_parser.add_subparsers(dest="tests_command", required=True)
    for name, help_text in (("list", "show saved tests"),
                            ("run", "run one saved test, or all of them")):
        child = tests_sub.add_parser(name, help=help_text)
        child.add_argument("--app", default="",
                           help="only this application's tests")
        child.add_argument("--agent", default="",
                           help="the host holding the suite (default: here)")
        child.add_argument("--timeout", type=float, default=0.0)
        if name == "run":
            child.add_argument("test", nargs="?", default="",
                               help="test id (app/name). Omit to run them all")
    tests_parser.set_defaults(func=_cmd_tests)

    panel_parser = sub.add_parser(
        "panel", help="open the recording control panel")
    panel_parser.add_argument("--lib", default="", help="path to libqatrec.*.so")
    panel_parser.add_argument("--app", default="", help="path to the application")
    panel_parser.add_argument("--name", default="",
                              help="name used in the generated test")
    panel_parser.add_argument("--agent", default="",
                              help="registered host name, or HOST[:PORT]")
    panel_parser.set_defaults(func=_cmd_panel)

    web_panel_parser = sub.add_parser(
        "web-panel", help="open the web-based recording control panel")
    web_panel_parser.add_argument("--port", type=int, default=8766,
                                  help="port to run the local web server on (default: 8766)")
    web_panel_parser.set_defaults(func=_cmd_web_panel)

    build_parser = sub.add_parser(
        "build-filter",
        help="compile the event filter for this machine's Qt (needed to record)")
    build_parser.add_argument(
        "--out", default="~/qatrec-filter",
        help="build directory (default: ~/qatrec-filter)")
    build_parser.add_argument(
        "--with-test-app", action="store_true",
        help="also build a small Qt application to try recording against")
    build_parser.add_argument(
        "--qt", choices=("auto", "5", "6"), default="auto",
        help="Qt major version to build against (default: the newest installed, "
             "or the one --app carries)")
    build_parser.add_argument(
        "--app", default="",
        help="the application this filter is for; an application that ships "
             "its own Qt decides which Qt to build against")
    build_parser.add_argument("--jobs", type=int, default=0)
    build_parser.add_argument("--verbose", action="store_true",
                              help="show full compiler output")
    build_parser.set_defaults(func=_cmd_build_filter)

    servers_parser = sub.add_parser(
        "qat-servers",
        help="check Qat's prebuilt servers against this machine, and repair them")
    servers_parser.add_argument(
        "--fix", action="store_true",
        help="stand a server that loads here in for one that does not")
    servers_parser.add_argument(
        "--restore", action="store_true",
        help="undo --fix, putting every original back")
    servers_parser.set_defaults(func=_cmd_qat_servers)

    hosts_parser = sub.add_parser(
        "hosts", help="manage the list of VMs you can record on")
    hosts_sub = hosts_parser.add_subparsers(dest="hosts_command", required=True)

    hosts_sub.add_parser("list", help="show registered hosts")

    add_parser = hosts_sub.add_parser("add", help="register a host")
    add_parser.add_argument("name")
    add_parser.add_argument("address", help="HOST[:PORT]")
    add_parser.add_argument("--fingerprint", default="",
                            help="SHA-256 fingerprint printed by the agent")
    add_parser.add_argument("--ca-file", default="")
    add_parser.add_argument("--token-file", default="",
                            help="path to the shared token (preferred)")
    add_parser.add_argument("--token-env", default="",
                            help="environment variable holding the token")
    add_parser.add_argument("--ngrok-url", default="",
                            help="ngrok HTTPS URL (bypasses fingerprint checking)")
    add_parser.add_argument("--insecure-plaintext", action="store_true")
    add_parser.add_argument("--note", default="")

    remove_parser = hosts_sub.add_parser("remove", help="forget a host")
    remove_parser.add_argument("name")

    check_parser = hosts_sub.add_parser(
        "check", help="connect to a host and report whether it is free")
    check_parser.add_argument("name")

    hosts_parser.set_defaults(func=_cmd_hosts)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "audit" and not args.app and not args.launch:
        parser.error("give a registered app name, or use --launch")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
