# -*- coding: utf-8 -*-
"""
Session controller — all the recording logic, no Qt.

The widgets in `panel.py` are deliberately thin: everything that can go wrong
lives here, where it can be tested without a display, a running application, or
a human. Same seam as the backend port in Phase 1, for the same reason.

Checkpoint insertion uses our own click stream rather than Qat's picker.
`qat.activate_picker()` only toggles a server-side mode; it gives the client no
way to learn *what* was picked, so building on it would be guesswork. Instead,
"add checkpoint" arms picking, and the next click in the application selects a
target rather than being recorded as an action. That reuses the resolver we
already trust and needs no new mechanism.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from qat_recorder.capture import CaptureSession
from qat_recorder.events import RawEvent
from qat_recorder.ir import Action, ActionKind, Recording, Robustness


def default_wrapper() -> Path:
    """Path to the launcher wrapper, shipped inside the package.

    It must be found the same way whether this is a source checkout or a wheel
    installed on a VM — an earlier version resolved it relative to the repository
    layout, which pointed into site-packages once installed and left the file
    missing entirely.

    Wheels do not reliably preserve the executable bit, so it is restored here.
    If the install is read-only, a copy is staged in the temporary directory.
    """
    packaged = Path(__file__).resolve().parents[1] / "resources" / "wrapper.sh"
    if not packaged.exists():
        raise FileNotFoundError(
            f"launcher wrapper missing from the installation ({packaged})")

    if os.access(packaged, os.X_OK):
        return packaged

    try:
        packaged.chmod(packaged.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
                       | stat.S_IXOTH)
        if os.access(packaged, os.X_OK):
            return packaged
    except OSError:
        pass

    staged = Path(tempfile.gettempdir()) / "qatrec-wrapper.sh"
    shutil.copyfile(packaged, staged)
    staged.chmod(0o755)
    return staged


class State(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    PICKING = "picking"
    STOPPED = "stopped"


class ControllerError(RuntimeError):
    """A transition that does not make sense in the current state."""


class RecorderController:
    def __init__(
        self,
        qat_module,
        lib_path: str = "",
        app_path: str = "",
        app_name: str = "app",
        wrapper: Optional[str] = None,
        backend=None,
        receiver=None,
        on_state: Optional[Callable[[State], None]] = None,
        on_action: Optional[Callable[[Action], None]] = None,
        on_picked: Optional[Callable[[Any, dict], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ):
        self.qat = qat_module
        self.lib_path = lib_path
        self.app_path = app_path
        self.app_name = app_name
        self.wrapper = wrapper or str(default_wrapper())

        self._backend = backend
        self._receiver = receiver
        self._owns_receiver = receiver is None

        self.on_state = on_state
        self.on_action = on_action
        self.on_picked = on_picked
        self.on_error = on_error

        self._state = State.IDLE
        self.session: Optional[CaptureSession] = None
        self.context = None
        self.registered_name = "_qat_recorder_session"
        #: Where `save()` last wrote, so a replay knows what to run.
        self.saved_to = ""

        self.events_seen = 0
        self.events_dropped = 0
        self._drop_next_release = False
        self.picked_target = None
        self.picked_node = None
        self.picked_properties: dict = {}

    # -- state -------------------------------------------------------------

    @property
    def location(self) -> str:
        """Where the application under test runs. Local, here."""
        return "this machine"

    def configure(self, app_path: str, lib_path: str, app_name: str) -> None:
        """Adopt paths chosen in the UI before starting.

        Present on the remote controller too, so the panel can set up a session
        without knowing which kind it holds.
        """
        self.app_path = app_path
        self.lib_path = lib_path
        self.app_name = app_name or self.app_name

    @property
    def state(self) -> State:
        return self._state

    def _set_state(self, state: State) -> None:
        if state == self._state:
            return
        self._state = state
        if self.on_state:
            self.on_state(state)

    @property
    def recording(self) -> Optional[Recording]:
        return self.session.recording if self.session else None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._state not in (State.IDLE, State.STOPPED):
            raise ControllerError(f"cannot start while {self._state.value}")

        if self._owns_receiver:
            from qat_recorder.events import EventReceiver  # noqa: PLC0415
            self._receiver = EventReceiver(port=0)
        self._receiver.start()

        os.environ["QATREC_PORT"] = str(self._receiver.actual_port)
        os.environ["QATREC_LIB"] = self.lib_path
        os.environ["QATREC_APP"] = self.app_path

        # Qat locks the application's UI during a test run so stray input cannot
        # corrupt it. While recording that is exactly backwards -- it would block
        # the input being captured.
        try:
            self.qat.test_settings.Settings.lock_ui = "never"
        except AttributeError:
            pass

        self.qat.register_application(self.registered_name, self.wrapper, "")
        self.context = self.qat.start_application(self.registered_name)

        if self._backend is None:
            from qat_recorder.backend import QatBackend  # noqa: PLC0415
            self._backend = QatBackend(self.qat)

        self.session = CaptureSession(self._backend, app_name=self.app_name,
                                      app_path=self.app_path)
        self.events_seen = 0
        self.events_dropped = 0
        self._set_state(State.RECORDING)

    def stop(self) -> Optional[Recording]:
        if self._state is State.IDLE:
            raise ControllerError("nothing to stop")
        self.poll()
        recording = None
        if self.session is not None:
            before = len(self.session.recording.actions)
            recording = self.session.finish()
            self._emit_new_actions(before)

        if self._receiver is not None:
            self._receiver.stop()
        try:
            if self.context is not None:
                self.qat.close_application(self.context)
        except Exception as error:                            # noqa: BLE001
            self._report(f"could not close the application: {error}")
        finally:
            self.context = None
            try:
                self.qat.unregister_application(self.registered_name)
            except Exception:                                 # noqa: BLE001
                pass

        self._set_state(State.STOPPED)
        return recording

    def pause(self) -> None:
        if self._state is not State.RECORDING:
            raise ControllerError(f"cannot pause while {self._state.value}")
        self.poll()
        self._set_state(State.PAUSED)

    def resume(self) -> None:
        if self._state is not State.PAUSED:
            raise ControllerError(f"cannot resume while {self._state.value}")
        # Anything that happened while paused is deliberately discarded.
        self._discard_pending()
        self._set_state(State.RECORDING)

    #: Editing the recording is legal while it is being made and after it has
    #: been stopped. Not before: there is nothing to edit, and not while idle.
    _EDITABLE = (State.RECORDING, State.PAUSED, State.STOPPED)

    def inject_custom_code(self, code: str) -> None:
        """Append a step the operator wrote themselves."""
        if self._state not in self._EDITABLE:
            raise ControllerError(f"cannot insert a step while {self._state.value}")
        if self.session is None:
            raise ControllerError("nothing recorded")
        from qat_recorder.ir import Action, ActionKind  # noqa: PLC0415

        before = len(self.session.recording.actions)
        self.session.recording.add(
            Action(ActionKind.CUSTOM_CODE, args={"code": code}))
        self._emit_new_actions(before)

    def repair_drop(self, index: int, code: str) -> dict:
        """Fill the gap at `index` with a step, where the gap is.

        The point of doing this against the recording rather than against the
        generated text: everything is emitted from the recording -- the pytest
        file, the feature file, the step definitions, the object map -- and
        `save_as` regenerates all of them before it verifies. A fix that lives
        only in the script pane is discarded by the act of keeping it.
        """
        if self._state not in self._EDITABLE:
            raise ControllerError(f"cannot fill a gap while {self._state.value}")
        if self.session is None:
            raise ControllerError("nothing recorded")
        code = (code or "").strip()
        if not code:
            raise ControllerError("a repair needs some code to insert")

        from qat_recorder.ir import Action, ActionKind  # noqa: PLC0415

        recording = self.session.recording
        if not 0 <= index < len(recording.drops):
            raise ControllerError(f"no gap at {index}")
        note = f"fills the gap left by {recording.drops[index].label}"
        try:
            drop = recording.repair(
                index, Action(ActionKind.CUSTOM_CODE, args={"code": code},
                              note=note))
        except (IndexError, ValueError) as error:
            raise ControllerError(str(error)) from error

        self._report(f"filled the gap at step {drop.after}: {drop.label}")
        return {"index": index, "after": drop.after, "open": len(recording.open_drops())}

    # -- the event pump ----------------------------------------------------

    def poll(self) -> int:
        """Drain the receiver and process what arrived. Call this on a timer."""
        if self._receiver is None or self._state in (State.IDLE, State.STOPPED):
            return 0

        handled = 0
        for event in self._receiver.drain():
            self.events_seen += 1
            handled += 1
            self._process(event)

        # Close any interaction that is old enough that nothing more can belong
        # to it, so the operator sees each action as they perform it rather than
        # one behind.
        if self._state is State.RECORDING and self.session is not None:
            before = len(self.session.recording.actions)
            if self.session.flush_stale():
                self._emit_new_actions(before)
        return handled

    def _process(self, event: RawEvent) -> None:
        if self._state is State.PAUSED:
            self.events_dropped += 1
            return

        # A click consumed to select a checkpoint target must not also be
        # recorded, and neither must its release -- otherwise the folder sees a
        # release with no press and invents a drag.
        if self._drop_next_release and event.kind == "mouse_release":
            self._drop_next_release = False
            self.events_dropped += 1
            return

        if self._state is State.PICKING:
            self.events_dropped += 1
            if event.kind == "mouse_press":
                self._pick(event)
            return

        before = len(self.session.recording.actions)
        self.session.feed(event)
        self._emit_new_actions(before)

    def _emit_new_actions(self, before: int) -> None:
        if not self.on_action:
            return
        for action in self.session.recording.actions[before:]:
            self.on_action(action)

    # -- checkpoints -------------------------------------------------------

    def arm_checkpoint(self) -> None:
        if self._state is not State.RECORDING:
            raise ControllerError(f"cannot pick while {self._state.value}")
        self.picked_target = None
        self.picked_node = None
        self.picked_properties = {}
        self._set_state(State.PICKING)

    def cancel_checkpoint(self) -> None:
        if self._state is not State.PICKING:
            return
        self._set_state(State.RECORDING)

    def _pick(self, event: RawEvent) -> None:
        self._drop_next_release = True
        resolved = self.session.resolve_locator(event.target)
        self._set_state(State.RECORDING)
        if resolved is None:
            self._report("that object could not be identified; nothing to check")
            return
        node, target = resolved
        self.picked_node = node
        self.picked_target = target
        self.picked_properties = self.session.properties_of(node)
        if self.on_picked:
            self.on_picked(target, self.picked_properties)

    def add_checkpoint(self, property_name: str,
                       expected: Any = None) -> Optional[Action]:
        """Insert a verification against the object picked most recently."""
        if self.picked_target is None:
            raise ControllerError("nothing has been picked yet")
        if expected is None:
            expected = self.picked_properties.get(property_name)

        before = len(self.session.recording.actions)
        action = self.session.recording.add(Action(
            ActionKind.VERIFY_PROPERTY,
            target=self.picked_target,
            args={"property": property_name, "expected": expected},
            t=self.session.recording.actions[-1].t
            if self.session.recording.actions else 0.0,
        ))
        self._emit_new_actions(before)
        return action

    def drop_last_action(self) -> Optional[Action]:
        """Undo — the operator misclicked, which happens constantly."""
        if not self.session or len(self.session.recording.actions) <= 1:
            return None
        return self.session.recording.actions.pop()

    # -- output ------------------------------------------------------------

    def summary(self) -> dict:
        recording = self.recording
        actions = recording.actions if recording else []
        weak = [a for a in actions
                if a.target and a.target.robustness.rank >= Robustness.WEAK.rank]
        return {
            "state": self._state.value,
            "events": self.events_seen,
            "dropped": self.events_dropped,
            "actions": max(0, len(actions) - 1),      # the LAUNCH is bookkeeping
            "unresolved": self.session.unresolved if self.session else 0,
            # Gaps nobody has filled yet. `unresolved` counts raw events and
            # counts a press and its release twice; this counts the things the
            # operator is actually being asked about.
            "gaps": len(recording.open_drops()) if recording else 0,
            "fragile": len(weak),
            "secrets": len(recording.secrets()) if recording else 0,
        }

    def save(self, out_dir: str) -> list:
        """Write the artifacts into a directory of the caller's choosing."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        written = []
        for name, text in self.generated_files().items():
            path = out / name
            path.write_text(text, encoding="utf-8")
            written.append(str(path))
        self.saved_to = str(out)
        return written

    def generated_files(self) -> dict:
        """The artifacts for the current recording, as name -> text."""
        from qat_recorder.emit import (  # noqa: PLC0415
            emit_gherkin, emit_python, emit_steps)

        recording = self.recording
        if recording is None:
            raise ControllerError("nothing recorded")
        problems = recording.validate()
        if problems:
            raise ControllerError("recording is not valid: " + "; ".join(problems))

        from qat_recorder.emit.python import emit_object_map  # noqa: PLC0415

        files = {
            "recording.json": recording.dumps(),
            "test_recorded.py": emit_python(recording),
            "recorded.feature": emit_gherkin(recording),
            "steps.py": emit_steps(recording),
            # Empty of overrides, but it lists what the application gave no
            # durable name, so fixing one is editing a line rather than
            # inventing one.
            "objects.json": emit_object_map(recording),
        }
        if self.session is not None and self.session.failures:
            from qat_recorder.capture import dropped_report  # noqa: PLC0415
            files["unresolved.txt"] = dropped_report(self.session.failures)
        return files

    def save_as(self, name: str, verify: bool = True) -> dict:
        """Keep this recording in the library, and prove it replays.

        The proving is the point. Every failure this project has had followed
        one shape: the recorder emits steps it has never executed, and the
        operator finds out later -- sometimes days later, always after they have
        forgotten what they clicked. No amount of care about locators fixes
        that, because the recorder is predicting the future and only a replay
        settles it.

        The application has just been closed by `stop()`, the machine is right
        here, and the person who can still do something about a failure is
        standing in front of it. So the test is run once, immediately, and the
        verdict is stored with it. A test in this library has either been proved
        to replay or is marked as not having replayed; there is no third state
        where nobody knows.
        """
        from qat_recorder.library import TestLibrary        # noqa: PLC0415

        library = TestLibrary()
        case = library.save(
            app=self.app_name or Path(self.app_path).name or "app",
            name=name,
            files=self.generated_files(),
            app_path=self.app_path,
            summary=self.summary())
        self.saved_to = str(case.directory)

        if verify:
            from qat_recorder.replay import run_pytest       # noqa: PLC0415
            library.record_verdict(case, run_pytest(case.directory))
        return case.to_dict()

    def list_tests(self, app: str = "") -> list:
        from qat_recorder.library import TestLibrary        # noqa: PLC0415

        chosen = app or self.app_name or Path(self.app_path).name
        return [case.to_dict() for case in TestLibrary().list(chosen)]

    def replay_test(self, test_id: str, timeout: float = 0.0) -> dict:
        from qat_recorder.library import TestLibrary        # noqa: PLC0415
        from qat_recorder.replay import DEFAULT_TIMEOUT, run_pytest

        case = TestLibrary().get(test_id)
        result = run_pytest(case.directory,
                            timeout=timeout or DEFAULT_TIMEOUT)
        result["test"] = case.id
        result["name"] = case.name
        return result

    def replay(self, directory: str = "") -> dict:
        """Run the generated test on this machine.

        The recorded test launches the application itself, so it can only run
        where the application is. For a local session that is here; for a
        session driven from another machine, the agent calls this on the host,
        which is the point -- nobody should have to copy a file across to find
        out whether their recording replays.
        """
        from qat_recorder.replay import run_pytest                # noqa: PLC0415

        target = directory or self.saved_to
        if not target:
            target = str(Path(tempfile.gettempdir()) /
                         f"qatrec-{self.registered_name}")
            self.save(target)
        return run_pytest(target)

    # -- helpers -----------------------------------------------------------

    def _discard_pending(self) -> None:
        if self._receiver is not None:
            dropped = len(self._receiver.drain())
            self.events_dropped += dropped

    def _report(self, message: str) -> None:
        if self.on_error:
            self.on_error(message)
