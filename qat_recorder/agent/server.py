# -*- coding: utf-8 -*-
"""
The agent: runs on the VM, beside the application under test.

It owns a `RecorderController` — the same one the local panel drives — so all the
recording logic is shared and only transport is new. Qat never leaves the machine
it was designed for.

Threading model: one background pump per session calls `controller.poll()` on a
timer, exactly as the panel's QTimer does. Every touch of the controller is under
one lock, because it was written for a single-threaded caller and an HTTP server
is not one.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from qat_recorder.agent import protocol
from qat_recorder.agent.protocol import (
    AgentError, Busy, Command, PROTOCOL_VERSION, envelope,
)
from qat_recorder.agent.security import parse_bearer, token_matches

PUMP_INTERVAL = 0.2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Session:
    """One recording session, owned by one tester."""

    def __init__(self, controller, owner: str, app: str):
        self.id = uuid.uuid4().hex[:12]
        self.owner = owner or "unknown"
        self.app = app
        self.started_at = _now()
        self.controller = controller
        self.lock = threading.RLock()
        self.picked: Optional[dict] = None
        self.errors: list = []
        self._stop = threading.Event()
        self._pump: Optional[threading.Thread] = None

        controller.on_picked = self._on_picked
        controller.on_error = self.errors.append

    def _on_picked(self, target, properties) -> None:
        # Surfaced through the next events poll; the remote client then asks for
        # a property and issues add_checkpoint.
        self.picked = {
            "label": target.label,
            "definition": dict(target.definition),
            "robustness": target.robustness.value,
            "properties": {key: str(value)
                           for key, value in properties.items()},
        }

    def start_pump(self) -> None:
        self._pump = threading.Thread(target=self._run_pump, daemon=True)
        self._pump.start()

    def _run_pump(self) -> None:
        while not self._stop.is_set():
            try:
                with self.lock:
                    self.controller.poll()
            except Exception as error:                        # noqa: BLE001
                self.errors.append(str(error))
            self._stop.wait(PUMP_INTERVAL)

    def shutdown(self) -> None:
        self._stop.set()
        if self._pump is not None:
            self._pump.join(timeout=3.0)

    def actions_since(self, cursor: int) -> list:
        recording = self.controller.recording
        if recording is None:
            return []
        return [action.to_dict() for action in recording.actions[cursor:]]

    def action_count(self) -> int:
        recording = self.controller.recording
        return len(recording.actions) if recording else 0

    def directory(self) -> Path:
        """Where this session's artifacts live on the host.

        Under the operator's own home directory, one folder per session, named
        after the session rather than the application: two recordings of the
        same application are two different things and must not overwrite each
        other.
        """
        root = os.environ.get("QATREC_SESSIONS") or \
            str(Path.home() / "qatrec-sessions")
        return Path(root).expanduser() / self.id

    def describe(self) -> dict:
        return {
            "session_id": self.id,
            "owner": self.owner,
            "app": self.app,
            "started_at": self.started_at,
            "state": self.controller.state.value,
            "directory": str(self.directory()),
        }


class Agent:
    """Agent logic, independent of HTTP so it can be tested directly."""

    def __init__(self, token: str, controller_factory: Callable,
                 applications: Optional[Callable] = None,
                 host_name: str = ""):
        if not token:
            raise ValueError("the agent refuses to run without a token")
        self.token = token
        self.controller_factory = controller_factory
        self._applications = applications
        self.host_name = host_name or _host_name()
        self.session: Optional[Session] = None
        self.lock = threading.RLock()

    # -- discovery ---------------------------------------------------------

    def health(self) -> dict:
        with self.lock:
            busy = self.session.describe() if self.session else None
        # The version of the package on *this* machine, and the name of the UI
        # bundle it serves. Both are shown in the panel, because the commonest
        # way for everything to look broken at once is a VM running an older
        # wheel than the one the panel expects -- and until now nothing said so.
        from qat_recorder import __version__               # noqa: PLC0415

        return {
            "agent": "qat-recorder-agent",
            "host": self.host_name,
            "protocol": PROTOCOL_VERSION,
            "version": __version__,
            "ui_build": _ui_build(),
            "session": busy,
        }

    def applications(self) -> dict:
        if self._applications is not None:
            return {"applications": list(self._applications())}
        try:
            import qat
            registered = qat.list_applications()
        except Exception as error:                            # noqa: BLE001
            raise AgentError(f"could not list applications: {error}", 500)
        return {"applications": [
            {"name": name, "path": str(path)}
            for name, path in sorted(registered.items())
        ]}

    # -- sessions ----------------------------------------------------------

    def start_session(self, app: str, lib: str, name: str, owner: str) -> dict:
        with self.lock:
            if self.session is not None and self.session.controller.state.value \
                    not in ("stopped", "idle"):
                raise Busy(self.session.owner, self.session.started_at,
                           self.session.id)
            if self.session is not None:
                self.session.shutdown()
                self.session = None

            controller = self.controller_factory(app=app, lib=lib, name=name)
            session = Session(controller, owner=owner, app=name or app)
            # Stills and video belong beside the rest of the session's files, on
            # the machine that can actually see the screen. Set before start(),
            # which is what begins filming.
            from qat_recorder.media import SessionMedia      # noqa: PLC0415

            if getattr(controller, "media", None) is None:
                controller.media = SessionMedia(
                    session.directory(),
                    qat_module=getattr(controller, "qat", None),
                    record_video=os.environ.get("QATREC_NO_VIDEO") != "1")
            try:
                with session.lock:
                    controller.start()
            except Exception as error:                        # noqa: BLE001
                raise AgentError(f"could not start the application: {error}", 500)
            session.start_pump()
            self.session = session
            return session.describe()

    def current(self) -> dict:
        with self.lock:
            if self.session is None:
                return {"session": None}
            return {"session": self.session.describe()}

    def _require(self, session_id: str) -> Session:
        with self.lock:
            session = self.session
        if session is None:
            raise AgentError("no session on this host", 404)
        if session_id and session_id != session.id:
            raise AgentError(
                f"session {session_id} is not the active session", 404)
        return session

    def events(self, session_id: str, since: int, wait: float) -> dict:
        session = self._require(session_id)
        wait = max(0.0, min(wait, protocol.MAX_POLL_SECONDS))
        deadline = time.time() + wait

        while True:
            with session.lock:
                actions = session.actions_since(since)
                picked = session.picked
                errors = session.errors[:]
                state = session.controller.state.value
                summary = session.controller.summary()
            if actions or picked or errors or time.time() >= deadline:
                break
            time.sleep(0.1)

        with session.lock:
            session.picked = None
            session.errors.clear()

        return {
            "session_id": session.id,
            "state": state,
            "actions": actions,
            "next": since + len(actions),
            "picked": picked,
            "errors": errors,
            "summary": summary,
        }

    def command(self, session_id: str, command: str, args: dict) -> dict:
        session = self._require(session_id)
        try:
            action = Command(command)
        except ValueError:
            raise AgentError(f"unknown command {command!r}", 400)

        with session.lock:
            controller = session.controller
            try:
                if action is Command.PAUSE:
                    controller.pause()
                elif action is Command.RESUME:
                    controller.resume()
                elif action is Command.ARM_CHECKPOINT:
                    controller.arm_checkpoint()
                elif action is Command.CANCEL_CHECKPOINT:
                    controller.cancel_checkpoint()
                elif action is Command.ADD_CHECKPOINT:
                    controller.add_checkpoint(
                        args.get("property", "text"), args.get("expected"))
                elif action is Command.UNDO:
                    controller.drop_last_action()
                elif action is Command.STOP:
                    controller.stop()
                elif action is Command.CUSTOM_CODE:
                    controller.inject_custom_code(args.get("code", ""))
                elif action is Command.REPAIR_DROP:
                    controller.repair_drop(int(args.get("index", -1)),
                                           args.get("code", ""))
                elif action is Command.ARM_REPAIR:
                    controller.arm_repair(int(args.get("index", -1)))
                elif action is Command.REPAIR_SUGGESTION:
                    controller.repair_suggestion(int(args.get("index", -1)),
                                                 args.get("text", ""))
                elif action is Command.SCREENSHOT:
                    controller.take_screenshot()
            except Exception as error:                        # noqa: BLE001
                raise AgentError(str(error), 409)
            return {"state": controller.state.value,
                    "summary": controller.summary()}

    def artifacts(self, session_id: str, custom_script: str = None) -> dict:
        from qat_recorder.emit import emit_gherkin, emit_python, emit_steps

        session = self._require(session_id)
        with session.lock:
            recording = session.controller.recording
            if recording is None:
                raise AgentError("nothing recorded", 409)
            # Validated whether or not the script was hand-edited. A custom
            # script replaces one file; the feature file, the step definitions
            # and the object map are still generated from the recording, and an
            # incoherent recording cannot produce them.
            problems = recording.validate()
            if problems:
                raise AgentError("recording is not valid: " + "; ".join(problems),
                                 409)
            from qat_recorder.emit.python import emit_object_map

            files = {
                "test_recorded.py": custom_script if custom_script is not None else emit_python(recording),
                "recorded.feature": emit_gherkin(recording),
                "steps.py": emit_steps(recording),
                "objects.json": emit_object_map(recording),
            }
            # Comes back with the artifacts, so a tester driving a VM from
            # their own machine can see what the recording lost without
            # logging in to read it there.
            capture = getattr(session.controller, "session", None)
            if capture is not None and capture.failures:
                from qat_recorder.capture import dropped_report
                files["unresolved.txt"] = dropped_report(capture.failures)

            # Written on the host as well as returned. The tester gets a copy on
            # their own machine, and the copy that can actually be *run* stays
            # where the application is.
            directory = session.directory()
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "recording.json").write_text(
                recording.dumps(), encoding="utf-8")
            for name, text in files.items():
                (directory / name).write_text(text, encoding="utf-8")

            return {
                "session_id": session.id,
                "recording": recording.to_dict(),
                "files": files,
                "directory": str(directory),
            }

    def media(self, session_id: str) -> dict:
        """What was captured of the screen, and what to ask for to see it."""
        session = self._require(session_id)
        with session.lock:
            holder = getattr(session.controller, "media", None)
            if holder is None:
                return {"stills": [], "video": "", "video_note":
                        "this session is not keeping any media", "filming": False}
            described = holder.describe()
            recording = session.controller.recording
            described["gap_shots"] = {
                str(index): drop.shot
                for index, drop in enumerate(recording.drops if recording else [])
                if drop.shot}
            return described

    def media_file(self, session_id: str, name: str):
        """The bytes of one still or the video, as a path. Never escapes."""
        session = self._require(session_id)
        holder = getattr(session.controller, "media", None)
        path = holder.path_of(name) if holder is not None else None
        if path is None:
            raise AgentError(f"no media called {name!r} in this session", 404)
        return path

    def preview(self, session_id: str) -> dict:
        from qat_recorder.emit import emit_python
        
        session = self._require(session_id)
        with session.lock:
            recording = session.controller.recording
            if recording is None:
                return {"script": "", "dropped": "", "failures": [],
                        "gaps": [], "open_gaps": 0}
            
            try:
                script = emit_python(recording)
            except Exception:
                script = "# Recording invalid or empty"
                
            failures_list = []
            dropped = ""
            capture = getattr(session.controller, "session", None)
            if capture is not None and capture.failures:
                from qat_recorder.capture import dropped_report
                failures_list = list(capture.failures)
                dropped = dropped_report(capture.failures)

            # Every gap, with the position it happened at and whether it has
            # been filled. The panel renders these; it does not have to read
            # them back out of the generated Python, though the markers are
            # there too so the script stands on its own.
            gaps = [dict(drop.to_dict(), index=index)
                    for index, drop in enumerate(recording.drops)]

            return {
                "script": script,
                "dropped": dropped,
                "failures": failures_list,
                "gaps": gaps,
                "open_gaps": len([one for one in gaps if not one["repaired"]]),
            }

    # -- the library -------------------------------------------------------

    def keep(self, session_id: str, name: str, verify: bool = True) -> dict:
        """Promote a finished session into the host's suite, and prove it runs.

        The proving happens here, on the host, because that is where the
        application is -- and it is why the tester's machine gets a verdict back
        with the name rather than a promise.
        """
        session = self._require(session_id)
        with session.lock:
            try:
                return {"test": session.controller.save_as(name, verify=verify)}
            except Exception as error:                        # noqa: BLE001
                raise AgentError(str(error), 409)

    def tests(self, app: str = "") -> dict:
        from qat_recorder.library import TestLibrary

        library = TestLibrary()
        return {"tests": [case.to_dict() for case in library.list(app)],
                "apps": library.apps(),
                "root": str(library.root)}

    def run_test(self, test_id: str, timeout: float = 0.0) -> dict:
        """Run one saved test. Deliberately independent of any session.

        A suite is worth more than the recording session that made it: a tester
        joining on Tuesday should be able to run what someone recorded on
        Monday, and the agent has long since forgotten that session.
        """
        from qat_recorder.library import TestLibrary
        from qat_recorder.replay import DEFAULT_TIMEOUT, run_pytest

        with self.lock:
            busy = (self.session is not None
                    and self.session.controller.state.value
                    not in ("stopped", "idle"))
        if busy:
            raise AgentError(
                "a recording is in progress on this host; stop it first", 409)

        try:
            case = TestLibrary().get(test_id)
        except LookupError as error:
            raise AgentError(str(error), 404)

        result = run_pytest(case.directory, timeout=timeout or DEFAULT_TIMEOUT)
        result["test"] = case.id
        result["name"] = case.name
        return result

    def replay(self, session_id: str, timeout: float = 0.0) -> dict:
        """Run this session's generated test, here, where the application is."""
        from qat_recorder.replay import DEFAULT_TIMEOUT, run_pytest

        session = self._require(session_id)
        with session.lock:
            # Recording holds the application open through Qat, and a replay
            # launches its own copy. Running both at once means two instances
            # fighting over one screen, which is not a test of anything.
            if session.controller.state.value not in ("stopped", "idle"):
                raise AgentError(
                    "stop the recording before replaying it", 409)
            directory = session.directory()
            if not (directory / "test_recorded.py").exists():
                self.artifacts(session_id)
            return run_pytest(directory,
                              timeout=timeout or DEFAULT_TIMEOUT)

    def release(self, session_id: str) -> dict:
        session = self._require(session_id)
        with session.lock:
            try:
                session.controller.stop()
            except Exception:                                 # noqa: BLE001
                pass
        session.shutdown()
        with self.lock:
            self.session = None
        return {"released": session.id}

    def shutdown(self) -> None:
        with self.lock:
            if self.session is not None:
                self.session.shutdown()
                self.session = None


def _ui_build() -> str:
    """The hashed name of the JS bundle this agent serves, or "" if none.

    Vite renames the bundle whenever its contents change, so this is a build
    identity that needs no build step of its own to maintain.
    """
    from pathlib import Path as _Path                       # noqa: PLC0415

    static = _Path(__file__).resolve().parents[1] / "web" / "static" / "assets"
    try:
        newest = sorted(static.glob("index-*.js"))
    except OSError:
        return ""
    return newest[-1].name if newest else ""


def _host_name() -> str:
    import socket
    try:
        return socket.gethostname()
    except Exception:                                         # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "qat-recorder-agent/1"
    agent: Agent = None            # set on the server instance

    def log_message(self, fmt, *args):    # noqa: A003
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # -- helpers -----------------------------------------------------------

    def _authorised(self) -> bool:
        presented = parse_bearer(self.headers.get("Authorization"))
        return token_matches(self.server.agent.token, presented)

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(envelope(payload)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, error: AgentError) -> None:
        self._send(error.status or 500,
                   {"error": str(error), "detail": error.detail})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            raise AgentError("request body is not valid JSON", 400)

    def _dispatch(self, method: str) -> None:
        if not self._authorised():
            # 401 without detail: an unauthenticated caller learns nothing about
            # what is running here.
            self._send(401, {"error": "unauthorised"})
            return

        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)
        agent = self.server.agent

        try:
            if parts == ["v1", "health"] and method == "GET":
                return self._send(200, agent.health())
            if parts == ["v1", "applications"] and method == "GET":
                return self._send(200, agent.applications())
            if parts == ["v1", "sessions"] and method == "GET":
                return self._send(200, agent.current())
            if parts == ["v1", "tests"] and method == "GET":
                return self._send(200, agent.tests(
                    (query.get("app") or [""])[0]))
            if parts == ["v1", "tests", "run"] and method == "POST":
                body = self._body()
                return self._send(200, agent.run_test(
                    body.get("test", ""), float(body.get("timeout") or 0.0)))
            if parts == ["v1", "sessions"] and method == "POST":
                body = self._body()
                return self._send(201, agent.start_session(
                    app=body.get("app", ""), lib=body.get("lib", ""),
                    name=body.get("name", ""), owner=body.get("owner", "")))
            if len(parts) == 4 and parts[:2] == ["v1", "sessions"]:
                session_id, tail = parts[2], parts[3]
                if tail == "events" and method == "GET":
                    since = int((query.get("since") or ["0"])[0])
                    wait = float((query.get("wait") or ["5"])[0])
                    return self._send(200, agent.events(session_id, since, wait))
                if tail == "command" and method == "POST":
                    body = self._body()
                    return self._send(200, agent.command(
                        session_id, body.get("command", ""),
                        body.get("args") or {}))
                if tail == "artifacts" and method == "GET":
                    return self._send(200, agent.artifacts(session_id))
                if tail == "replay" and method == "POST":
                    body = self._body()
                    return self._send(200, agent.replay(
                        session_id, float(body.get("timeout") or 0.0)))
                if tail == "keep" and method == "POST":
                    body = self._body()
                    return self._send(201, agent.keep(
                        session_id, body.get("name", ""),
                        bool(body.get("verify", True))))
            if len(parts) == 3 and parts[:2] == ["v1", "sessions"] \
                    and method == "DELETE":
                return self._send(200, agent.release(parts[2]))
            self._send(404, {"error": f"no route for {method} {parsed.path}"})
        except AgentError as error:
            self._fail(error)
        except Exception as error:                            # noqa: BLE001
            self._fail(AgentError(f"internal error: {error}", 500))

    def do_GET(self):        # noqa: N802
        self._dispatch("GET")

    def do_POST(self):       # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self):     # noqa: N802
        self._dispatch("DELETE")


class AgentServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, agent: Agent, ssl_context=None,
                 verbose: bool = False):
        super().__init__(address, _Handler)
        self.agent = agent
        self.verbose = verbose
        if ssl_context is not None:
            self.socket = ssl_context.wrap_socket(self.socket, server_side=True)

    def handle_error(self, request, client_address):
        """Stay quiet about connections that simply went away.

        A client that rejects our certificate, or times out a long poll, drops
        the socket mid-read. That is normal and expected — printing a stack
        trace for each one fills the log with noise and buries the errors worth
        reading.
        """
        import socket
        import ssl
        import sys

        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, BrokenPipeError,
                              ConnectionAbortedError, socket.timeout,
                              ssl.SSLError, ssl.SSLEOFError, TimeoutError)):
            if self.verbose:
                print(f"agent: connection from {client_address[0]} ended: "
                      f"{type(error).__name__}")
            return
        super().handle_error(request, client_address)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def serve_in_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread

    def close(self) -> None:
        self.shutdown()
        self.agent.shutdown()
        self.server_close()
