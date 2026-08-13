# -*- coding: utf-8 -*-
"""
End-to-end: real input -> native filter -> Qat resolution -> generated test.

Both halves at once. xdotool drives a real X server, so the events are genuinely
spontaneous; the native filter captures them; Qat resolves the structural
locators into validated definitions; the code generators emit a runnable test.

Run inside the qatrec-test image via run_e2e.sh.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/work")
sys.path.insert(0, str(REPO))

import qat                                                    # noqa: E402
from qat_recorder.backend import QatBackend                    # noqa: E402
from qat_recorder.capture import CaptureSession                # noqa: E402
from qat_recorder.emit import emit_gherkin, emit_python        # noqa: E402
from qat_recorder.events import EventReceiver                  # noqa: E402
from qat_recorder.ui.controller import default_wrapper         # noqa: E402

APP = "qatrec_e2e"


def xdo(*args):
    subprocess.run(["xdotool", *args], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.35)


def main():
    lib = os.environ["QATREC_LIB"]
    app_binary = os.environ["QATREC_APP"]
    wrapper = str(default_wrapper())      # shipped inside the package

    receiver = EventReceiver(port=0)
    receiver.start()
    os.environ["QATREC_PORT"] = str(receiver.actual_port)
    print(f"receiver listening on {receiver.actual_port}")

    # Qat locks the application's UI during a test so that stray human input
    # cannot corrupt a run. While RECORDING that is exactly backwards: it would
    # block the very input we are trying to capture.
    qat.test_settings.Settings.lock_ui = "never"

    qat.register_application(APP, wrapper, "")
    context = qat.start_application(APP)
    print(f"application started (pid {getattr(context, 'pid', '?')})")
    time.sleep(2)

    try:
        window = subprocess.run(
            ["xdotool", "search", "--name", "QatRecTestApp"],
            capture_output=True, text=True).stdout.split()
        if not window:
            print("FAIL: no application window found")
            return 1
        wid = window[0]
        xdo("windowactivate", wid)

        # A realistic little session.
        xdo("mousemove", "--window", wid, "60", "80", "click", "1")
        xdo("type", "--delay", "60", "alice")
        xdo("key", "Tab")
        xdo("type", "--delay", "60", "hunter2")
        xdo("key", "ctrl+s")
        time.sleep(1.5)

        events = receiver.drain(timeout=1.0)
        print(f"\ncaptured {len(events)} raw event(s)")
        for event in events[:6]:
            print(f"  {event.kind:<14} {event.target.cls:<14} "
                  f"{event.target.object_name}")

        if not events:
            print("FAIL: nothing captured")
            return 1

        backend = QatBackend(qat)
        session = CaptureSession(backend, app_name=APP)
        session.feed_all(events)
        recording = session.finish()

        print(f"\nfolded into {len(recording.actions)} action(s), "
              f"{session.unresolved} unresolved")
        for action in recording.actions:
            target = action.target.label if action.target else "-"
            print(f"  {action.kind.value:<16} {target:<20} {action.args}")

        problems = recording.validate()
        if problems:
            print(f"FAIL: invalid recording: {problems}")
            return 1

        secrets = recording.secrets()
        print(f"\nredacted values: {len(secrets)}")
        payload = recording.dumps()
        if "hunter2" in payload:
            print("FAIL: the password reached the recording")
            return 1
        print("ok: password never reached the recording")

        (REPO / "spike/e2e_recording.json").write_text(payload, encoding="utf-8")

        print("\n" + "=" * 66)
        print("GENERATED PYTEST")
        print("=" * 66)
        source = emit_python(recording, source="e2e_recording.json")
        print(source)
        compile(source, "generated.py", "exec")
        print("ok: generated module compiles")

        print("\n" + "=" * 66)
        print("GENERATED FEATURE")
        print("=" * 66)
        print(emit_gherkin(recording, source="e2e_recording.json"))
        return 0
    finally:
        receiver.stop()
        try:
            qat.close_application(context)
        finally:
            qat.unregister_application(APP)


if __name__ == "__main__":
    sys.exit(main())
