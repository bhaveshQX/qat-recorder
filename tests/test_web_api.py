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


# --- the screen, over the wire ---------------------------------------------

def test_media_is_listed_and_the_stills_are_fetchable(panel, tmp_path):
    """The application runs on the VM, the operator is somewhere else, and the
    only route between the two is this connection."""
    from qat_recorder.media import SessionMedia
    from tests.test_media import FakeQat as ShootingQat

    controller = panel.agent.session.controller
    controller.media = SessionMedia(tmp_path / "session", record_video=False,
                                    qat_module=ShootingQat())
    _with_a_gap(panel)

    listing = panel.client.get(f"/v1/sessions/{panel.session}/media",
                               headers=AUTH).json()
    assert listing["stills"], "the gap was not photographed"
    assert listing["gap_shots"]["0"] == listing["stills"][0]

    shot = panel.client.get(
        f"/v1/sessions/{panel.session}/media/{listing['stills'][0]}",
        headers=AUTH)
    assert shot.status_code == 200
    assert shot.content.startswith(b"\x89PNG")


def test_a_still_can_be_fetched_with_the_token_in_the_query(panel, tmp_path):
    """Nothing can put an Authorization header on an <img src>."""
    from qat_recorder.media import SessionMedia
    from tests.test_media import FakeQat as ShootingQat

    controller = panel.agent.session.controller
    controller.media = SessionMedia(tmp_path / "session", record_video=False,
                                    qat_module=ShootingQat())
    _with_a_gap(panel)
    name = panel.client.get(f"/v1/sessions/{panel.session}/media",
                            headers=AUTH).json()["stills"][0]

    assert panel.client.get(
        f"/v1/sessions/{panel.session}/media/{name}?token={TOKEN}").status_code == 200
    assert panel.client.get(
        f"/v1/sessions/{panel.session}/media/{name}").status_code == 401
    assert panel.client.get(
        f"/v1/sessions/{panel.session}/media/{name}?token=wrong").status_code == 401


def test_media_outside_the_session_is_not_served(panel, tmp_path):
    from qat_recorder.media import SessionMedia

    controller = panel.agent.session.controller
    controller.media = SessionMedia(tmp_path / "session", record_video=False)
    (tmp_path / "secrets.txt").write_text("not yours")
    answer = panel.client.get(
        f"/v1/sessions/{panel.session}/media/..%2Fsecrets.txt", headers=AUTH)
    assert answer.status_code == 404


def test_a_session_with_no_media_says_so_rather_than_failing(panel):
    listing = panel.client.get(f"/v1/sessions/{panel.session}/media",
                               headers=AUTH).json()
    assert listing["stills"] == []
    assert listing["video_note"]


# --- pointing at the control, over the wire --------------------------------

def test_pointing_arms_and_the_next_click_fills_the_gap(panel):
    """The whole path the panel drives, end to end.

    Arm from the browser, which pauses; say the control is on screen; the
    operator clicks it; the click is resolved the way every recorded step is
    resolved and lands in the gap, without being recorded as a step of its own.
    """
    _with_a_gap(panel)
    assert panel.preview()["open_gaps"] == 1

    armed = panel.command("arm_repair", index=0)
    assert armed.status_code == 200
    assert armed.json()["state"] == "paused", (
        "the panel shows this from the response, not from the next socket frame")
    assert panel.command("pick_now").json()["state"] == "picking"

    steps_before = len([g for g in panel.preview()["script"].splitlines()
                        if "mouse_click" in g])
    panel.push(*click_pair(400, "QPushButton", "loginButton"))

    after = panel.preview()
    assert after["open_gaps"] == 0
    assert after["gaps"][0]["repaired"] is True
    assert DROP_MARKER not in after["script"]
    steps_after = len([g for g in after["script"].splitlines()
                       if "mouse_click" in g])
    assert steps_after == steps_before + 1, (
        "the pointing click added the repair and nothing else")


def test_pointing_needs_a_running_application(panel):
    _with_a_gap(panel)
    panel.command("stop")
    assert panel.command("arm_repair", index=0).status_code == 409


def test_arming_can_be_called_off(panel):
    """Cancelling leaves the recording paused on purpose: the operator has been
    walking around the application, and resuming from wherever they ended up
    would record the next steps from the wrong place."""
    _with_a_gap(panel)
    panel.command("arm_repair", index=0)
    assert panel.command("cancel_repair").json()["state"] == "paused"
    assert panel.preview()["open_gaps"] == 1, "the gap is still open"


# --- pointing at a control you have to walk back to ------------------------

def test_arming_pauses_so_navigation_costs_nothing(panel):
    """The flaw that made pointing unusable in a real session.

    A gap is noticed from a different screen than the one it happened on.
    Getting back there takes clicks -- and the first version consumed the first
    of them as the pick, so anything more than one click away was unreachable,
    while navigating there before arming recorded the navigation as steps.
    """
    _with_a_gap(panel)
    armed = panel.command("arm_repair", index=0)
    assert armed.json()["state"] == "paused"
    assert armed.json()["summary"]["arming"] == 0

    steps_before = panel.preview()["script"].count("mouse_click")
    # Walking back to the control: four clicks, none of them the pick.
    for at in range(400, 800, 100):
        panel.push(*click_pair(at, "QPushButton", "loginButton"))

    after = panel.preview()
    assert after["script"].count("mouse_click") == steps_before, (
        "navigating back to the control was recorded as steps")
    assert after["open_gaps"] == 1, "and none of those clicks was taken as the pick"


def test_the_click_after_it_is_on_screen_now_is_the_pick(panel):
    _with_a_gap(panel)
    panel.command("arm_repair", index=0)
    panel.push(*click_pair(400, "QPushButton", "loginButton"))     # navigation

    ready = panel.command("pick_now")
    assert ready.json()["state"] == "picking"
    panel.push(*click_pair(700, "QPushButton", "loginButton"))     # the pick

    after = panel.preview()
    assert after["open_gaps"] == 0
    assert after["gaps"][0]["repaired"] is True


def test_after_filling_a_gap_the_recording_is_still_paused(panel):
    """The operator navigated to get here; resuming from this screen would
    record the next steps from the wrong place."""
    _with_a_gap(panel)
    panel.command("arm_repair", index=0)
    panel.command("pick_now")
    panel.push(*click_pair(700, "QPushButton", "loginButton"))
    assert panel.client.get("/v1/sessions", headers=AUTH).json()[
        "session"]["state"] == "paused"


def test_only_the_gap_being_filled_says_it_is_waiting(panel):
    """`arming` names the gap. A global picking flag put every gap on screen
    into Waiting at once."""
    _with_a_gap(panel)
    panel.push(*click_pair(900, "QNotEither", "alsoNothing"))
    assert panel.preview()["open_gaps"] == 2

    panel.command("arm_repair", index=1)
    assert panel.command("pick_now").json()["summary"]["arming"] == 1


def test_giving_up_leaves_the_recording_paused(panel):
    _with_a_gap(panel)
    panel.command("arm_repair", index=0)
    answer = panel.command("cancel_repair")
    assert answer.json()["summary"]["arming"] is None
    assert panel.preview()["open_gaps"] == 1
