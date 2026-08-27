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
    controller.media.settle()          # the picture is taken behind the pump

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
    controller.media.settle()
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


# --- which build is where --------------------------------------------------

def test_the_agent_reports_the_bundle_its_index_actually_loads(tmp_path, monkeypatch):
    """Read from index.html, not from a directory listing.

    An install over an older one can leave bundles behind, and picking the
    alphabetically last of several named a build the browser was never going to
    load -- which made the panel accuse a correctly installed machine of being
    out of date on every single connection.
    """
    from qat_recorder.agent import server as agent_server

    static = tmp_path / "web" / "static"
    (static / "assets").mkdir(parents=True)
    for stale in ("index-AAAAAAAA.js", "index-ZZZZZZZZ.js", "index-Mmmmmmmm.js"):
        (static / "assets" / stale).write_text("stale")
    (static / "index.html").write_text(
        '<!doctype html><script type="module" crossorigin '
        'src="/static/assets/index-Mmmmmmmm.js"></script>')

    monkeypatch.setattr(agent_server, "__file__",
                        str(tmp_path / "agent" / "server.py"))
    assert agent_server._ui_build() == "index-Mmmmmmmm.js"


def test_health_carries_the_version_and_the_bundle(panel):
    body = panel.client.get("/v1/health", headers=AUTH).json()
    assert body["version"], "nothing said which build the VM is running"
    assert body["ui_build"].startswith("index-")


# --- the panel must not be stalled by the recorder -------------------------

def test_a_request_does_not_wait_for_the_pump(panel):
    """Every agent call takes the session lock, and the pump holds it while it
    folds events.

    Waiting for it inside an `async def` blocks the whole event loop -- and the
    WebSocket that streams steps to the panel with it. One slow poll stalled
    everything at once, which is how a recording could finish before the panel
    had shown any of it.
    """
    import threading
    import time as clock

    session = panel.agent.session
    held = threading.Event()
    release = threading.Event()

    def hog():
        with session.lock:
            held.set()
            release.wait(3.0)

    threading.Thread(target=hog, daemon=True).start()
    assert held.wait(2.0), "could not take the session lock"

    # Health does not touch the session lock, so it must answer immediately even
    # while something else is holding it.
    started = clock.perf_counter()
    answer = panel.client.get("/v1/health", headers=AUTH)
    elapsed = clock.perf_counter() - started
    release.set()

    assert answer.status_code == 200
    assert elapsed < 2.0, (
        f"a request took {elapsed:.1f}s while the lock was held elsewhere")


# --- the steps, and which picture belongs to which -------------------------

def test_the_steps_come_from_the_recording_not_from_the_stream(panel):
    """The panel used to accumulate these from the event stream, which is
    append-only -- and a repair *inserts* a step in the middle.

    From the first repair onwards the browser's list was a different list in a
    different order, so every picture after that point belonged to the wrong
    step. There is one source of truth and this is it.
    """
    _with_a_gap(panel)
    # ...and a step after the gap, so filling it is genuinely an insert.
    panel.push(*click_pair(500, "QPushButton", "loginButton"))
    before = panel.preview()["steps"]
    assert len(before) == 2
    assert all("index" in step for step in before)
    assert all(step["kind"] != "launch" for step in before), (
        "the launch is bookkeeping, not something anybody did")

    panel.command("repair_drop", index=0, code="qat.mouse_click({'text': 'X'})")
    after = panel.preview()["steps"]

    assert len(after) == 3
    # Between the two clicks, where the event was lost -- not appended.
    assert [step["kind"] for step in after] == ["click", "custom_code", "click"]
    # And the indices still run in the recording's order, which is what a
    # picture is looked up by.
    assert [step["index"] for step in after] == sorted(
        step["index"] for step in after)


def test_a_step_carries_its_own_picture(panel, tmp_path):
    """Looking a picture up by row number assumed the browser's list and the
    recording's list were the same list."""
    from qat_recorder.media import SessionMedia
    from tests.test_media import FakeQat as ShootingQat

    controller = panel.agent.session.controller
    controller.media = SessionMedia(tmp_path / "session", record_video=False,
                                    qat_module=ShootingQat())
    panel.push(*click_pair(100, "QPushButton", "loginButton"))
    controller.media.settle()

    steps = panel.preview()["steps"]
    assert steps, "no steps at all"
    assert any(step["shot"] for step in steps), "no step carried a picture"


def test_a_picture_is_not_named_until_it_exists(panel, tmp_path):
    """A name is handed out the moment a step is folded, before the worker has
    taken anything. Advertising it straight away had the panel asking for files
    that did not exist yet."""
    from qat_recorder.media import SessionMedia
    from tests.test_media import FakeQat as ShootingQat

    controller = panel.agent.session.controller
    controller.media = SessionMedia(tmp_path / "session", record_video=False,
                                    qat_module=ShootingQat())
    panel.push(*click_pair(100, "QPushButton", "loginButton"))

    for step in panel.preview()["steps"]:
        if step["shot"]:
            assert (tmp_path / "session" / "shots" / step["shot"]).is_file()


# --- what a replay actually runs -------------------------------------------

def test_a_replay_includes_a_repair_made_after_an_earlier_save(panel, tmp_path):
    """The one outcome that makes correcting a recording pointless.

    The generated file used to be written once and reused, so a gap filled after
    a Save -- or after an earlier replay -- was silently left out, and the
    operator watched a replay of the version they had just corrected.
    """
    _with_a_gap(panel)
    # A Save writes the file...
    panel.client.post(f"/v1/sessions/{panel.session}/artifacts", headers=AUTH,
                      json={}).raise_for_status()
    panel.command("stop")

    # ...and only then is the gap filled.
    panel.command("repair_drop", index=0,
                  code="qat.mouse_click({'text': 'Cancel'})")
    panel.client.post(f"/v1/sessions/{panel.session}/replay", headers=AUTH,
                      json={"timeout": 1.0})

    written = panel.client.post(f"/v1/sessions/{panel.session}/artifacts",
                                headers=AUTH, json={}).json()
    assert "qat.mouse_click({'text': 'Cancel'})" in written["files"]["test_recorded.py"]


def test_a_script_the_operator_typed_is_not_regenerated_over(panel):
    """Their file, their words. Re-emitting would throw the work away."""
    _with_a_gap(panel)
    panel.command("stop")
    mine = "def test_recorded_session(application):\n    pass  # mine\n"
    panel.client.post(f"/v1/sessions/{panel.session}/artifacts", headers=AUTH,
                      json={"custom_script": mine}).raise_for_status()

    session = panel.agent.session
    assert session.hand_edited is True
    panel.client.post(f"/v1/sessions/{panel.session}/replay", headers=AUTH,
                      json={"timeout": 1.0})
    on_disk = (session.directory() / "test_recorded.py").read_text(encoding="utf-8")
    assert on_disk == mine, "the operator's own script was regenerated over"
