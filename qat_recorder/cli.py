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

    temporary = False
    app_name = args.app
    if args.launch:
        app_name = "_qat_recorder_audit"
        qat.register_application(app_name, args.launch, " ".join(args.args))
        temporary = True

    # Qat locks the application's UI when it starts a session, so that stray
    # human input cannot corrupt a running test. During an audit that is
    # actively unhelpful: the application looks frozen, and the operator cannot
    # navigate to the screen they actually want scanned.
    try:
        qat.test_settings.Settings.lock_ui = "never"
    except AttributeError:
        pass

    context = qat.start_application(app_name)
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
            qat.close_application(context)
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
    return 0


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

    # Qat locks the application's UI during a test run so stray input cannot
    # corrupt it. While recording that is backwards -- it would block the very
    # input being captured.
    qat.test_settings.Settings.lock_ui = "never"

    name = "_qat_recorder_session"
    qat.register_application(name, str(wrapper), "")
    context = qat.start_application(name)
    print(f"recording for {args.seconds}s -- interact with the application now")

    try:
        time.sleep(args.seconds)
        events = receiver.drain(timeout=1.0)
        print(f"captured {len(events)} raw event(s)")

        # The name identifies the application in generated code; the path lets
        # that code register it. Defaulting the name to the executable's
        # basename rather than its full path keeps the generated test readable.
        session = CaptureSession(
            QatBackend(qat),
            app_name=args.name or Path(args.app).name,
            app_path=args.app)
        session.feed_all(events)
        recording = session.finish()
    finally:
        receiver.stop()
        try:
            qat.close_application(context)
        finally:
            qat.unregister_application(name)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "recording.json").write_text(recording.dumps(), encoding="utf-8")
    (out / "test_recorded.py").write_text(emit_python(recording), encoding="utf-8")
    (out / "recorded.feature").write_text(emit_gherkin(recording), encoding="utf-8")
    (out / "steps.py").write_text(emit_steps(recording), encoding="utf-8")

    print(f"{len(recording.actions)} action(s), {session.unresolved} unresolved")
    print(f"written to {out}")

    _report_dropped(session, out)

    for action in recording.weakest_targets():
        print(f"  {action.target.robustness.value:<10} {action.target.label}",
              file=sys.stderr)
    return 0


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

    report = out / "unresolved.txt"
    report.write_text(
        "Events that could not be turned into steps.\n\n"
        "Each of these is something you did that the generated test will not "
        "do. If one of them mattered -- closing a dialog, for instance -- the "
        "replay diverges from your session at that point.\n\n"
        + "\n".join(f"{count:>4} x  {reason}"
                    for reason, count in counted.most_common()) + "\n",
        encoding="utf-8")
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
    from qat_recorder.native import BuildError, build, source_dir

    try:
        print(f"source: {source_dir()}")
        library = build(args.out, with_test_app=args.with_test_app,
                        jobs=args.jobs, verbose=args.verbose)
    except BuildError as error:
        print(f"\n{error}", file=sys.stderr)
        return 1

    print(f"\nbuilt: {library}")
    print("\nrecord with it:")
    print(f"  python -m qat_recorder record --lib {library} \\")
    print("      --app /path/to/your/app --seconds 60 --out ./recorded")
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

    panel_parser = sub.add_parser(
        "panel", help="open the recording control panel")
    panel_parser.add_argument("--lib", default="", help="path to libqatrec.*.so")
    panel_parser.add_argument("--app", default="", help="path to the application")
    panel_parser.add_argument("--name", default="",
                              help="name used in the generated test")
    panel_parser.add_argument("--agent", default="",
                              help="registered host name, or HOST[:PORT]")
    panel_parser.set_defaults(func=_cmd_panel)

    build_parser = sub.add_parser(
        "build-filter",
        help="compile the event filter for this machine's Qt (needed to record)")
    build_parser.add_argument(
        "--out", default="~/qatrec-filter",
        help="build directory (default: ~/qatrec-filter)")
    build_parser.add_argument(
        "--with-test-app", action="store_true",
        help="also build a small Qt application to try recording against")
    build_parser.add_argument("--jobs", type=int, default=0)
    build_parser.add_argument("--verbose", action="store_true",
                              help="show full compiler output")
    build_parser.set_defaults(func=_cmd_build_filter)

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
