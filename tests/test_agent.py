# -*- coding: utf-8 -*-
"""
Agent tests — a real server on a real socket, driven by the real client.

No mocking of the transport: the agent is started on localhost and every
assertion goes over HTTP, so routing, auth, error mapping and the long-poll all
get exercised. TLS is covered separately and skipped where openssl is absent.
"""

import shutil
import threading
import time

import pytest

from qat_recorder.agent.client import AgentClient
from qat_recorder.agent.protocol import AgentError, Busy
from qat_recorder.agent.security import (
    generate_self_signed, generate_token, parse_bearer, token_matches,
)
from qat_recorder.agent.server import Agent, AgentServer
from qat_recorder.ui.controller import RecorderController
from tests.fixtures import build_tree
from tests.test_capture import click_pair, event
from tests.test_ui_controller import FakeQat, FakeReceiver

TOKEN = "test-token-not-a-real-secret"


class Harness:
    """An agent backed by the synthetic tree, so no application is launched."""

    def __init__(self, ssl_context=None):
        self.receivers = []
        self.nodes = None

        def factory(app, lib, name):
            backend, nodes = build_tree()
            nodes["username"].props["text"] = "alice"
            self.nodes = nodes
            receiver = FakeReceiver()
            self.receivers.append(receiver)
            return RecorderController(
                FakeQat(), lib_path=lib, app_path=app, app_name=name or app,
                backend=backend, receiver=receiver)

        self.agent = Agent(
            TOKEN, controller_factory=factory,
            applications=lambda: [{"name": "demo", "path": "/opt/demo"}],
            host_name="test-vm")
        self.server = AgentServer(("127.0.0.1", 0), self.agent,
                                  ssl_context=ssl_context)
        self.server.serve_in_background()

    @property
    def port(self):
        return self.server.port

    def push(self, *events):
        self.receivers[-1].push(*events)

    def close(self):
        self.server.close()


@pytest.fixture()
def harness():
    h = Harness()
    yield h
    h.close()


@pytest.fixture()
def client(harness):
    return AgentClient("127.0.0.1", harness.port, TOKEN,
                       owner="alice", insecure_plaintext=True, timeout=15)


def drain(client, session_id, until=None, since=0, timeout=10.0):
    """Poll until `until` is satisfied, or the timeout expires.

    A single poll is not enough to see a specific action. At cursor 0 the LAUNCH
    action always exists, so the long-poll returns immediately with it, before
    the agent's pump has folded whatever was pushed a moment ago. A real client
    streams in a loop; so does this.

    Returns (all actions seen, last batch).
    """
    actions = []
    cursor = since
    batch = {}
    deadline = time.time() + timeout
    while True:
        batch = client.events(session_id, since=cursor, wait=1.0)
        actions.extend(batch["actions"])
        cursor = batch["next"]
        if until is not None and until(actions, batch):
            return actions, batch
        if until is None or time.time() >= deadline:
            return actions, batch


def has_kind(kind):
    return lambda actions, batch: any(a["kind"] == kind for a in actions)


def was_picked(actions, batch):
    return batch.get("picked") is not None


# --- auth ------------------------------------------------------------------

def test_valid_token_is_accepted(client):
    assert client.health()["host"] == "test-vm"


def test_wrong_token_is_rejected(harness):
    bad = AgentClient("127.0.0.1", harness.port, "wrong-token",
                      insecure_plaintext=True, timeout=15)
    with pytest.raises(AgentError, match="rejected the token"):
        bad.health()


def test_unauthorised_response_leaks_nothing(harness):
    bad = AgentClient("127.0.0.1", harness.port, "", insecure_plaintext=True,
                      timeout=15)
    with pytest.raises(AgentError) as info:
        bad.applications()
    assert info.value.status == 401
    assert "demo" not in str(info.value)


def test_agent_refuses_to_run_without_a_token():
    with pytest.raises(ValueError, match="refuses to run without a token"):
        Agent("", controller_factory=lambda **_: None)


def test_token_comparison_and_parsing():
    token = generate_token()
    assert token_matches(token, token)
    assert not token_matches(token, token[:-1] + "x")
    assert not token_matches(token, None)
    assert parse_bearer(f"Bearer {token}") == token
    assert parse_bearer(f"bearer {token}") == token
    assert parse_bearer("Basic abc") is None
    assert parse_bearer(None) is None


# --- discovery -------------------------------------------------------------

def test_applications_are_listed(client):
    apps = client.applications()
    assert apps == [{"name": "demo", "path": "/opt/demo"}]


def test_health_reports_no_session_initially(client):
    assert client.health()["session"] is None
    assert client.current() is None


# --- session lifecycle -----------------------------------------------------

def test_starting_a_session_claims_the_host(client):
    session = client.start(app="/opt/demo", lib="/opt/libqatrec.so", name="demo")
    assert session["owner"] == "alice"
    assert session["state"] == "recording"
    assert client.health()["session"]["session_id"] == session["session_id"]


def test_a_second_tester_is_told_who_holds_the_host(harness, client):
    client.start(app="/opt/demo", lib="/lib.so", name="demo")

    other = AgentClient("127.0.0.1", harness.port, TOKEN, owner="bob",
                        insecure_plaintext=True, timeout=15)
    with pytest.raises(Busy) as info:
        other.start(app="/opt/demo", lib="/lib.so", name="demo")

    assert info.value.owner == "alice"
    assert info.value.status == 409
    assert "alice" in str(info.value)


def test_releasing_frees_the_host_for_someone_else(harness, client):
    session = client.start(app="/opt/demo", lib="/lib.so", name="demo")
    client.release(session["session_id"])

    other = AgentClient("127.0.0.1", harness.port, TOKEN, owner="bob",
                        insecure_plaintext=True, timeout=15)
    assert other.start(app="/opt/demo", lib="/lib.so", name="demo")["owner"] == "bob"


def test_commands_against_an_unknown_session_are_refused(client):
    client.start(app="/opt/demo", lib="/lib.so", name="demo")
    with pytest.raises(AgentError) as info:
        client.command("deadbeefdead", "pause")
    assert info.value.status == 404


def test_events_without_a_session_are_refused(client):
    with pytest.raises(AgentError) as info:
        client.events("nosession")
    assert info.value.status == 404


# --- the event stream ------------------------------------------------------

def test_actions_reach_the_client(harness, client):
    session = client.start(app="/opt/demo", lib="/lib.so", name="demo")
    harness.push(*click_pair(1000, "QPushButton", "loginButton"))

    actions, batch = drain(client, session["session_id"], until=has_kind("click"))
    kinds = [a["kind"] for a in actions]
    assert "launch" in kinds
    assert "click" in kinds
    assert batch["next"] == len(actions)
    assert batch["state"] == "recording"


def test_the_cursor_only_returns_new_actions(harness, client):
    session = client.start(app="/opt/demo", lib="/lib.so", name="demo")
    harness.push(*click_pair(1000, "QPushButton", "loginButton"))
    first = client.events(session["session_id"], since=0, wait=5)

    harness.push(*click_pair(2000, "QLabel", "statusLabel"))
    second = client.events(session["session_id"], since=first["next"], wait=5)

    assert second["actions"]
    assert all(a["kind"] != "launch" for a in second["actions"])
    assert second["next"] > first["next"]


def test_long_poll_returns_empty_rather_than_hanging(client):
    session = client.start(app="/opt/demo", lib="/lib.so", name="demo")
    started = time.time()
    batch = client.events(session["session_id"], since=99, wait=1.0)
    elapsed = time.time() - started
    assert batch["actions"] == []
    assert 0.8 < elapsed < 6.0


def test_long_poll_wakes_up_when_something_happens(harness, client):
    session = client.start(app="/opt/demo", lib="/lib.so", name="demo")
    first = client.events(session["session_id"], since=0, wait=2)

    def push_later():
        time.sleep(0.6)
        harness.push(*click_pair(3000, "QPushButton", "loginButton"))

    threading.Thread(target=push_later, daemon=True).start()
    started = time.time()
    batch = client.events(session["session_id"], since=first["next"], wait=10)
    assert batch["actions"]
    assert time.time() - started < 8


def test_summary_travels_with_the_events(harness, client):
    session = client.start(app="/opt/demo", lib="/lib.so", name="demo")
    harness.push(*click_pair(1000, "QPushButton", "loginButton"))
    _, batch = drain(client, session["session_id"], until=has_kind("click"))
    assert batch["summary"]["actions"] >= 1
    assert "fragile" in batch["summary"]


# --- commands --------------------------------------------------------------

def test_pause_and_resume_round_trip(client):
    session = client.start(app="/opt/demo", lib="/lib.so", name="demo")
    sid = session["session_id"]
    assert client.command(sid, "pause")["state"] == "paused"
    assert client.command(sid, "resume")["state"] == "recording"


def test_invalid_transition_is_reported_not_crashed(client):
    session = client.start(app="/opt/demo", lib="/lib.so", name="demo")
    with pytest.raises(AgentError) as info:
        client.command(session["session_id"], "resume")   # not paused
    assert info.value.status == 409


def test_unknown_command_is_rejected(client):
    session = client.start(app="/opt/demo", lib="/lib.so", name="demo")
    with pytest.raises(AgentError, match="unknown command"):
        client.command(session["session_id"], "self_destruct")


def test_undo_removes_the_last_action(harness, client):
    session = client.start(app="/opt/demo", lib="/lib.so", name="demo")
    sid = session["session_id"]
    harness.push(*click_pair(1000, "QPushButton", "loginButton"))
    drain(client, sid, until=has_kind("click"))
    before = client.command(sid, "pause")["summary"]["actions"]

    client.command(sid, "undo")
    after = client.command(sid, "resume")["summary"]["actions"]
    assert after == before - 1


# --- checkpoints over the wire ---------------------------------------------

def test_picking_is_reported_with_the_objects_properties(harness, client):
    session = client.start(app="/opt/demo", lib="/lib.so", name="demo")
    sid = session["session_id"]
    client.command(sid, "arm_checkpoint")
    harness.push(*click_pair(2000, "QLabel", "statusLabel"))

    _, batch = drain(client, sid, until=was_picked)
    assert batch["picked"] is not None
    assert batch["picked"]["label"] == "statusLabel"
    assert batch["picked"]["properties"]["text"] == "ready"


def test_a_checkpoint_can_be_added_remotely(harness, client):
    session = client.start(app="/opt/demo", lib="/lib.so", name="demo")
    sid = session["session_id"]
    client.command(sid, "arm_checkpoint")
    harness.push(*click_pair(2000, "QLabel", "statusLabel"))
    drain(client, sid, until=was_picked)

    client.command(sid, "add_checkpoint",
                   {"property": "text", "expected": "ready"})
    actions, _ = drain(client, sid, until=has_kind("verify_property"))
    assert any(a["kind"] == "verify_property" for a in actions)


# --- artifacts -------------------------------------------------------------

def test_artifacts_come_back_generated(harness, client):
    session = client.start(app="/opt/demo", lib="/lib.so", name="demo")
    sid = session["session_id"]
    harness.push(*click_pair(1000, "QPushButton", "loginButton"))
    drain(client, sid, until=has_kind("click"))
    client.command(sid, "stop")

    artifacts = client.artifacts(sid)
    assert artifacts["recording"]["app"] == "demo"
    assert set(artifacts["files"]) == {
        "test_recorded.py", "recorded.feature", "steps.py", "objects.json"}
    compile(artifacts["files"]["test_recorded.py"], "remote.py", "exec")
    assert "qat.mouse_click" in artifacts["files"]["test_recorded.py"]


def test_secrets_do_not_cross_the_wire(harness, client):
    session = client.start(app="/opt/demo", lib="/lib.so", name="demo")
    sid = session["session_id"]
    harness.nodes["password"].props["text"] = "hunter2"
    harness.push(event("key_press", 900, "QLineEdit", "passwordField",
                       key=ord("H")))
    drain(client, sid, timeout=3)
    client.command(sid, "stop")

    artifacts = client.artifacts(sid)
    import json as _json
    assert "hunter2" not in _json.dumps(artifacts)


# --- TLS -------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("openssl") is None,
                    reason="openssl is needed to generate a test certificate")
def test_tls_with_a_pinned_fingerprint(tmp_path):
    from qat_recorder.agent.security import fingerprint, server_context

    cert = str(tmp_path / "agent.crt")
    key = str(tmp_path / "agent.key")
    generate_self_signed(cert, key, common_name="localhost")
    pin = fingerprint(cert)

    harness = Harness(ssl_context=server_context(cert, key))
    try:
        secure = AgentClient("127.0.0.1", harness.port, TOKEN,
                             fingerprint=pin, owner="alice", timeout=15)
        assert secure.health()["host"] == "test-vm"

        wrong = AgentClient("127.0.0.1", harness.port, TOKEN,
                            fingerprint="00" * 32, owner="alice", timeout=15)
        with pytest.raises(AgentError, match="fingerprint does not match"):
            wrong.health()
    finally:
        harness.close()


def test_connecting_without_a_pin_or_ca_is_refused():
    with pytest.raises(ValueError, match="fingerprint to pin"):
        AgentClient("127.0.0.1", 1, TOKEN)
