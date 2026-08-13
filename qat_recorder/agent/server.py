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
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

    def describe(self) -> dict:
        return {
            "session_id": self.id,
            "owner": self.owner,
            "app": self.app,
            "started_at": self.started_at,
            "state": self.controller.state.value,
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
        return {
            "agent": "qat-recorder-agent",
            "host": self.host_name,
            "protocol": PROTOCOL_VERSION,
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
            except Exception as error:                        # noqa: BLE001
                raise AgentError(str(error), 409)
            return {"state": controller.state.value,
                    "summary": controller.summary()}

    def artifacts(self, session_id: str) -> dict:
        from qat_recorder.emit import emit_gherkin, emit_python, emit_steps

        session = self._require(session_id)
        with session.lock:
            recording = session.controller.recording
            if recording is None:
                raise AgentError("nothing recorded", 409)
            problems = recording.validate()
            if problems:
                raise AgentError("recording is not valid: " + "; ".join(problems),
                                 409)
            return {
                "session_id": session.id,
                "recording": recording.to_dict(),
                "files": {
                    "test_recorded.py": emit_python(recording),
                    "recorded.feature": emit_gherkin(recording),
                    "steps.py": emit_steps(recording),
                },
            }

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
