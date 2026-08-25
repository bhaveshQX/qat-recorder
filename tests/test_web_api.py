# -*- coding: utf-8 -*-
"""
The browser panel's HTTP surface, driven without a browser or an application.

The FastAPI layer holds no recording logic -- it wraps the same Agent the old
socket server wrapped -- so these tests check the wiring the panel depends on:
that a gap arrives with the position it happened at, that filling one over the
command endpoint changes the recording rather than the text in the pane, and
that the generated script reflects it immediately afterwards.

Skipped where FastAPI is not installed, exactly as the panel tests are skipped
without PySide6: the recorder itself needs neither.
"""

import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient                       # noqa: E402

from qat_recorder.agent.server import Agent                     # noqa: E402
from qat_recorder.emit.python import DROP_MARKER                # noqa: E402
from qat_recorder.ui.controller import RecorderController       # noqa: E402
from qat_recorder.web.app import create_app                     # noqa: E402
from tests.fixtures import build_tree                           # noqa: E402
from tests.test_capture import click_pair                       # noqa: E402
from tests.test_ui_controller import FakeQat, FakeReceiver      # noqa: E402

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOWHERE = ("QNotARealClass", "nothingLikeThis")


class Panel:
    """A client on an agent backed by the synthetic tree."""

    def __init__(self):
        self.receivers = []

        def factory(app, lib, name):
            backend, nodes = build_tree()
            receiver = FakeReceiver()
            self.receivers.append(receiver)
            return RecorderController(
                FakeQat(), lib_path=lib, app_path=app, app_name=name or app,
                backend=backend, receiver=receiver)

        self.agent = Agent(TOKEN, controller_factory=factory,
                           host_name="test-vm")
        self.client = TestClient(create_app(self.agent, token=TOKEN))
        self.session = self.client.post(
            "/v1/sessions", headers=AUTH,
            json={"app": "/opt/demo", "lib": "x", "name": "demo"},
        ).json()["session_id"]

    def push(self, *events):
        """Feed events and wait for the session's pump to fold them."""
        self.receivers[-1].push(*events)
        self.client.get(f"/v1/sessions/{self.session}/events",
                        params={"since": 0, "wait": 2.0}, headers=AUTH)
        time.sleep(0.3)

    def preview(self):
        return self.client.get(f"/v1/sessions/{self.session}/preview",
                               headers=AUTH).json()

    def command(self, name, **args):
        return self.client.post(f"/v1/sessions/{self.session}/command",
                                headers=AUTH,
                                json={"command": name, "args": args})


@pytest.fixture()
def panel():
    made = Panel()
    yield made
    made.agent.shutdown()


def _with_a_gap(panel):
    panel.push(*click_pair(100, "QPushButton", "loginButton"))
    panel.push(*click_pair(200, *NOWHERE))
    return panel


# --- what the panel is told ------------------------------------------------

def test_the_preview_reports_a_gap_and_where_it_is(panel):
    preview = _with_a_gap(panel).preview()
    assert preview["open_gaps"] == 1
    gap, = preview["gaps"]
    assert gap["index"] == 0
    assert gap["kind"] == "mouse_press"
    assert gap["after"] == 2
    assert gap["repaired"] is False


def test_the_script_the_panel_draws_carries_the_marker(panel):
    assert DROP_MARKER in _with_a_gap(panel).preview()["script"]


def test_a_session_with_nothing_dropped_reports_no_gaps(panel):
    panel.push(*click_pair(100, "QPushButton", "loginButton"))
    preview = panel.preview()
    assert preview["gaps"] == []
    assert preview["open_gaps"] == 0
    assert DROP_MARKER not in preview["script"]


# --- filling one -----------------------------------------------------------

def test_filling_a_gap_changes_the_recording_not_the_pane(panel):
    _with_a_gap(panel)
    answer = panel.command("repair_drop", index=0,
                           code="qat.mouse_click({'text': 'Cancel'})")
    assert answer.status_code == 200
    assert answer.json()["summary"]["gaps"] == 0

    after = panel.preview()
    assert after["open_gaps"] == 0
    assert DROP_MARKER not in after["script"]
    assert "qat.mouse_click({'text': 'Cancel'})" in after["script"]


def test_the_command_answers_with_the_envelope_not_a_status_field(panel):
    """The panel used to look for `status: ok`, which no response has ever had,
    and so reported failure after every successful insert."""
    _with_a_gap(panel)
    body = panel.command("custom_code", code="pass").json()
    assert "status" not in body
    assert body["protocol"] and body["state"]


def test_filling_a_gap_that_is_not_there_is_refused(panel):
    _with_a_gap(panel)
    assert panel.command("repair_drop", index=9, code="pass").status_code == 409


def test_an_empty_repair_is_refused(panel):
    _with_a_gap(panel)
    assert panel.command("repair_drop", index=0, code="  ").status_code == 409
