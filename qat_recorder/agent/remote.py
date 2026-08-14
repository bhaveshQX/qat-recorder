# -*- coding: utf-8 -*-
"""
A controller that drives a session on another machine.

Implements the same surface as `qat_recorder.ui.controller.RecorderController`,
so the panel cannot tell the difference — Phase 4 put all the logic behind a
narrow interface for testability, and that is precisely what makes remote drop in
without touching a widget.

The one thing that genuinely differs is polling. Locally, `poll()` drains an
in-process queue and returns immediately. Remotely, reading events is a *long*
poll that can hold for many seconds — calling that from the panel's timer would
freeze the UI. So a background thread does the long-polling and `poll()` drains
what it collected, which keeps callbacks firing on the caller's thread exactly as
the local controller does.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from qat_recorder.agent.protocol import AgentError, Busy
from qat_recorder.ir import Action, Recording, Robustness, Target
from qat_recorder.ui.controller import ControllerError, State

#: How long each background read may hold the connection open.
POLL_WAIT = 10.0


class RemoteRecorderController:
    def __init__(self, client, app_path: str = "", lib_path: str = "",
                 app_name: str = "",
                 on_state: Optional[Callable] = None,
                 on_action: Optional[Callable] = None,
                 on_picked: Optional[Callable] = None,
                 on_error: Optional[Callable] = None,
                 poll_wait: float = POLL_WAIT):
        self.client = client
        self.app_path = app_path
        self.lib_path = lib_path
        self.app_name = app_name
        self.poll_wait = poll_wait

        self.on_state = on_state
        self.on_action = on_action
        self.on_picked = on_picked
        self.on_error = on_error

        self._state = State.IDLE
        self.session_id = ""
        self.recording: Optional[Recording] = None
        self._summary: dict = {}
        #: Owned by the reader thread; the agent's `next` is authoritative.
        self._read_cursor = 0
        self._cursor_lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.picked_target: Optional[Target] = None
        self.picked_properties: dict = {}

    # -- identity ----------------------------------------------------------

    @property
    def location(self) -> str:
        return f"{self.client.host}:{self.client.port}"

    def configure(self, app_path: str, lib_path: str, app_name: str) -> None:
        """Paths refer to the remote machine, not this one."""
        self.app_path = app_path
        self.lib_path = lib_path
        self.app_name = app_name

    @property
    def state(self) -> State:
        return self._state

    def _set_state(self, value) -> None:
        try:
            new = State(value)
        except ValueError:
            return
        if new is self._state:
            return
        self._state = new
        if self.on_state:
            self.on_state(new)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._state not in (State.IDLE, State.STOPPED):
            raise ControllerError(f"cannot start while {self._state.value}")
        if not self.app_path:
            raise ControllerError("no application path for the remote host")

        try:
            session = self.client.start(
                app=self.app_path, lib=self.lib_path,
                name=self.app_name or Path(self.app_path).name)
        except Busy as busy:
            # Surfaced as-is: the panel shows who holds the host.
            raise ControllerError(str(busy)) from busy
        except AgentError as error:
            raise ControllerError(str(error)) from error

        self.session_id = session["session_id"]
        self.recording = Recording(app=session.get("app") or self.app_name)
        self.recording.actions.clear()
        with self._cursor_lock:
            self._read_cursor = 0
        self._summary = {}
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        self._set_state(session.get("state", "recording"))

    def stop(self) -> Optional[Recording]:
        """End the recording, but keep the session so its artifacts can be read.

        Stopping and giving up the machine are different acts: the operator
        stops, looks at what was captured, then saves. Releasing here would
        discard the session before `save()` could fetch anything.

        The host is not held hostage by that — the agent lets another tester
        claim a host whose session has stopped, so the lock effectively frees
        itself the moment recording ends.
        """
        if self._state is State.IDLE:
            raise ControllerError("nothing to stop")
        try:
            self._command("stop")
        except ControllerError:
            pass
        self.poll()                       # collect whatever the agent finished with
        self._shutdown_reader()
        self._set_state(State.STOPPED)
        return self.recording

    def release(self) -> None:
        """Give the host back explicitly, discarding the session."""
        self._shutdown_reader()
        if self.session_id:
            try:
                self.client.release(self.session_id)
            except AgentError:
                pass
        self.session_id = ""
        self._set_state(State.STOPPED)

    def pause(self) -> None:
        self._command("pause")

    def resume(self) -> None:
        self._command("resume")

    def arm_checkpoint(self) -> None:
        self.picked_target = None
        self.picked_properties = {}
        self._command("arm_checkpoint")

    def cancel_checkpoint(self) -> None:
        if self._state is State.PICKING:
            self._command("cancel_checkpoint")

    def add_checkpoint(self, property_name: str, expected: Any = None):
        if self.picked_target is None:
            raise ControllerError("nothing has been picked yet")
        self._command("add_checkpoint",
                      {"property": property_name, "expected": expected})
        return None                       # the action arrives through the stream

    def drop_last_action(self) -> Optional[Action]:
        if not self.recording or len(self.recording.actions) <= 1:
            return None
        self._command("undo")
        removed = self.recording.actions.pop()
        # The agent's list shrank too, so every later index shifts down by one.
        # Without this the next read would skip an action.
        with self._cursor_lock:
            self._read_cursor = max(0, self._read_cursor - 1)
        return removed

    # -- the pump ----------------------------------------------------------

    def _reader(self) -> None:
        """Long-poll in the background so the caller's thread never blocks.

        The cursor advances here, from the agent's authoritative `next`, the
        moment a batch arrives — not when `poll()` gets round to applying it. An
        earlier version read the applied cursor instead, so a reader that looped
        before the caller polled asked for the same range twice and delivered
        every action twice.
        """
        while not self._stop.is_set():
            with self._cursor_lock:
                since = self._read_cursor
            try:
                batch = self.client.events(
                    self.session_id, since=since, wait=self.poll_wait)
            except AgentError as error:
                self._queue.put(("error", str(error)))
                self._stop.wait(1.0)
                continue
            except Exception as error:                       # noqa: BLE001
                self._queue.put(("error", f"lost contact with the agent: {error}"))
                self._stop.wait(2.0)
                continue
            with self._cursor_lock:
                self._read_cursor = int(batch.get("next", since))
            self._queue.put(("batch", batch))

    def poll(self) -> int:
        """Apply whatever the reader collected. Never blocks."""
        applied = 0
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "error":
                if self.on_error:
                    self.on_error(payload)
                continue
            applied += self._apply(payload)
        return applied

    def _apply(self, batch: dict) -> int:
        self._summary = batch.get("summary") or self._summary

        for raw in batch.get("actions", []):
            action = Action.from_dict(raw)
            self.recording.add(action)
            if self.on_action:
                self.on_action(action)

        for message in batch.get("errors") or []:
            if self.on_error:
                self.on_error(message)

        picked = batch.get("picked")
        if picked:
            self.picked_properties = dict(picked.get("properties") or {})
            self.picked_target = Target(
                definition=dict(picked.get("definition") or {}),
                strategy="remote",
                robustness=Robustness(picked.get("robustness", "unresolved")),
                label=picked.get("label", ""),
            )
            if self.on_picked:
                self.on_picked(self.picked_target, self.picked_properties)

        self._set_state(batch.get("state", self._state.value))
        return len(batch.get("actions", []))

    def _shutdown_reader(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_wait + 5.0)
            self._thread = None

    # -- reporting and output ----------------------------------------------

    def summary(self) -> dict:
        summary = dict(self._summary)
        summary.setdefault("state", self._state.value)
        for key in ("events", "dropped", "actions", "unresolved", "fragile",
                    "secrets"):
            summary.setdefault(key, 0)
        summary["state"] = self._state.value
        return summary

    def replay(self, directory: str = "") -> dict:
        """Run the recording on the machine that made it.

        `directory` is ignored: the artifacts that matter live on the host, and
        that is the only place a recorded test can run, because it launches the
        application itself.
        """
        if not self.session_id:
            raise ControllerError("nothing recorded")
        try:
            return self.client.replay(self.session_id)
        except AgentError as error:
            raise ControllerError(str(error)) from error

    def save(self, out_dir: str) -> list:
        """Fetch the generated files from the agent and write them here."""
        if not self.session_id:
            raise ControllerError("nothing recorded")
        try:
            payload = self.client.artifacts(self.session_id)
        except AgentError as error:
            raise ControllerError(str(error)) from error

        import json

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        written = []

        path = out / "recording.json"
        path.write_text(json.dumps(payload["recording"], indent=2),
                        encoding="utf-8")
        written.append(str(path))

        for name, text in (payload.get("files") or {}).items():
            path = out / name
            path.write_text(text, encoding="utf-8")
            written.append(str(path))
        return written

    # -- helpers -----------------------------------------------------------

    def _command(self, command: str, args: Optional[dict] = None) -> dict:
        if not self.session_id:
            raise ControllerError("no remote session")
        try:
            result = self.client.command(self.session_id, command, args or {})
        except AgentError as error:
            raise ControllerError(str(error)) from error
        self._summary = result.get("summary") or self._summary
        self._set_state(result.get("state", self._state.value))
        return result
