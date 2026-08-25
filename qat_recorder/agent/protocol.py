# -*- coding: utf-8 -*-
"""
The wire contract between a tester's machine and an agent on a VM.

Deliberately plain HTTP with JSON bodies and long-polled event reads. No
WebSocket, no framework: recorded events arrive at human speed, so polling with a
held connection is entirely adequate, and it keeps the agent installable on a
locked-down RHEL box with nothing but the standard library and `qat`.

Every response carries `protocol`, so a client talking to an out-of-date agent
finds out immediately rather than by misparsing a field.
"""

from __future__ import annotations

from enum import Enum

PROTOCOL_VERSION = 1

#: Sent as `Authorization: Bearer <token>`.
AUTH_SCHEME = "Bearer"

#: Long-poll ceiling. The agent holds an events request open for at most this
#: long before returning an empty batch, which keeps proxies and load balancers
#: from killing an idle connection.
MAX_POLL_SECONDS = 25.0


class SessionState(str, Enum):
    """Mirrors qat_recorder.ui.controller.State across the wire."""

    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    PICKING = "picking"
    STOPPED = "stopped"


class Command(str, Enum):
    PAUSE = "pause"
    RESUME = "resume"
    ARM_CHECKPOINT = "arm_checkpoint"
    ADD_CHECKPOINT = "add_checkpoint"
    CANCEL_CHECKPOINT = "cancel_checkpoint"
    UNDO = "undo"
    STOP = "stop"
    CUSTOM_CODE = "custom_code"
    REPAIR_DROP = "repair_drop"
    ARM_REPAIR = "arm_repair"


class AgentError(RuntimeError):
    """The agent refused or failed a request."""

    def __init__(self, message: str, status: int = 0, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail or {}


class Busy(AgentError):
    """Another tester already holds this host.

    Recording is inherently exclusive: the operator drives the application's real
    UI, so two sessions on one host would fight over the same display. The agent
    says who holds it rather than queueing or failing opaquely.
    """

    def __init__(self, owner: str, since: str, session_id: str = ""):
        super().__init__(
            f"host is in use by {owner!r} since {since}", status=409,
            detail={"owner": owner, "since": since, "session_id": session_id})
        self.owner = owner
        self.since = since
        self.session_id = session_id


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

ROUTES = {
    # GET  -> what this agent is and what it can run
    "health": "/v1/health",
    # GET  -> applications registered with Qat on this host
    "applications": "/v1/applications",
    # GET  -> the current session, if any
    # POST -> claim the host and start recording
    "sessions": "/v1/sessions",
    # GET  -> long-polled batch of new actions since a cursor
    "events": "/v1/sessions/{session_id}/events",
    # POST -> pause/resume/checkpoint/undo/stop
    "command": "/v1/sessions/{session_id}/command",
    # GET  -> recording.json plus generated files
    "artifacts": "/v1/sessions/{session_id}/artifacts",
    # POST -> run the generated test ON THE HOST and return the result.
    # A recorded test launches the application itself, so it can only run where
    # the application is. Without this the tester has to copy the file to the VM
    # by hand, which is the errand the agent exists to remove.
    "replay": "/v1/sessions/{session_id}/replay",
    # POST -> keep this session as a named test in the host's library
    "keep": "/v1/sessions/{session_id}/keep",
    # GET  -> the saved tests on this host, optionally for one application
    "tests": "/v1/tests",
    # POST -> run one saved test, by `app/name`. Needs no session: the suite
    # outlives the recording that produced it.
    "run": "/v1/tests/run",
}


def route(name: str, **params) -> str:
    return ROUTES[name].format(**params)


def envelope(payload: dict) -> dict:
    """Every response body carries the protocol version."""
    body = {"protocol": PROTOCOL_VERSION}
    body.update(payload)
    return body


def check_protocol(body: dict) -> None:
    """Fail loudly on a version mismatch instead of misreading fields."""
    seen = body.get("protocol")
    if seen is None:
        raise AgentError("response has no protocol version; is this an agent?")
    if int(seen) != PROTOCOL_VERSION:
        raise AgentError(
            f"agent speaks protocol {seen}, this client speaks "
            f"{PROTOCOL_VERSION}; upgrade one of them")
