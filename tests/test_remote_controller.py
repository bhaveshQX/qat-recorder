# -*- coding: utf-8 -*-
"""
The remote controller, driven against a real agent over a real socket.

Same assertions the local controller gets in test_ui_controller, so the two
implementations are held to one standard — that is the whole point of the panel
not knowing which it holds.
"""

import time

import pytest

from qat_recorder.agent.client import AgentClient
from qat_recorder.agent.remote import RemoteRecorderController
from qat_recorder.ir import ActionKind
from qat_recorder.ui.controller import ControllerError, State
from tests.test_agent import TOKEN, Harness
from tests.test_capture import click_pair, event


@pytest.fixture()
def remote():
    harness = Harness()
    client = AgentClient("127.0.0.1", harness.port, TOKEN, owner="alice",
                         insecure_plaintext=True, timeout=15)
    controller = RemoteRecorderController(
        client, app_path="/opt/demo", lib_path="/opt/lib.so",
        app_name="demo", poll_wait=1.0)
    controller._harness = harness
    yield controller
    try:
        if controller.state not in (State.IDLE, State.STOPPED):
            controller.stop()
    finally:
        harness.close()


def pump(controller, until, timeout=15.0):
    """Drive poll() until a condition holds. The panel's QTimer does this."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        controller.poll()
        if until():
            return True
        time.sleep(0.1)
    controller.poll()
    return until()


def kinds(controller):
    recording = controller.recording
    return [a.kind for a in recording.actions] if recording else []


# --- lifecycle -------------------------------------------------------------

def test_start_claims_the_remote_host(remote):
    remote.start()
    assert remote.state is State.RECORDING
    assert remote.session_id
    assert remote.recording is not None


def test_location_names_the_agent(remote):
    assert ":" in remote.location
    assert remote.location != "this machine"


def test_cannot_start_twice(remote):
    remote.start()
    with pytest.raises(ControllerError, match="cannot start"):
        remote.start()


def test_a_busy_host_is_reported_with_the_holders_name(remote):
    remote.start()
    other_client = AgentClient("127.0.0.1", remote._harness.port, TOKEN,
                               owner="bob", insecure_plaintext=True, timeout=15)
    other = RemoteRecorderController(other_client, app_path="/opt/demo",
                                     lib_path="/l.so", app_name="demo")
    with pytest.raises(ControllerError, match="alice"):
        other.start()


def test_start_without_a_path_is_refused(remote):
    remote.configure("", "", "")
    with pytest.raises(ControllerError, match="no application path"):
        remote.start()


# --- streaming -------------------------------------------------------------

def test_actions_arrive_and_fire_callbacks(remote):
    seen = []
    remote.on_action = seen.append
    remote.start()
    remote._harness.push(*click_pair(1000, "QPushButton", "loginButton"))

    assert pump(remote, lambda: ActionKind.CLICK in kinds(remote))
    assert any(a.kind is ActionKind.CLICK for a in seen)
    assert seen[0].kind is ActionKind.LAUNCH


def test_actions_are_not_delivered_twice(remote):
    """The reader long-polls faster than poll() is called; its cursor must
    advance on receipt, not on application."""
    remote.start()
    remote._harness.push(*click_pair(1000, "QPushButton", "loginButton"))
    pump(remote, lambda: ActionKind.CLICK in kinds(remote))

    time.sleep(2.0)          # several reader cycles with nothing new
    remote.poll()

    assert kinds(remote).count(ActionKind.CLICK) == 1
    assert kinds(remote).count(ActionKind.LAUNCH) == 1


def test_targets_survive_the_round_trip(remote):
    remote.start()
    remote._harness.push(*click_pair(1000, "QPushButton", "loginButton"))
    pump(remote, lambda: ActionKind.CLICK in kinds(remote))

    click = next(a for a in remote.recording.actions
                 if a.kind is ActionKind.CLICK)
    assert click.target.definition == {"objectName": "loginButton"}
    assert click.target.label == "loginButton"
    assert click.target.robustness.value == "strong"


def test_fragile_targets_keep_their_index_and_warning(remote):
    remote.start()
    remote._harness.push(*click_pair(
        1000, "QPushButton", text="Apply", index=1,
        path=[("QGroupBox", "duplicateGroup")]))
    assert pump(remote, lambda: ActionKind.CLICK in kinds(remote))

    click = next(a for a in remote.recording.actions
                 if a.kind is ActionKind.CLICK)
    assert click.target.robustness.value == "fragile"
    assert click.target.index is not None
    assert click.target.warnings


def test_poll_never_blocks(remote):
    remote.start()
    started = time.time()
    for _ in range(5):
        remote.poll()
    assert time.time() - started < 1.0


# --- commands --------------------------------------------------------------

def test_pause_and_resume(remote):
    remote.start()
    remote.pause()
    assert remote.state is State.PAUSED
    remote.resume()
    assert remote.state is State.RECORDING


def test_invalid_transition_surfaces_as_controller_error(remote):
    remote.start()
    with pytest.raises(ControllerError):
        remote.resume()


def test_undo_removes_locally_and_remotely(remote):
    remote.start()
    remote._harness.push(*click_pair(1000, "QPushButton", "loginButton"))
    pump(remote, lambda: ActionKind.CLICK in kinds(remote))
    before = len(remote.recording.actions)

    removed = remote.drop_last_action()
    assert removed.kind is ActionKind.CLICK
    assert len(remote.recording.actions) == before - 1

    # And the removal must not come back on the next read.
    time.sleep(1.5)
    remote.poll()
    assert len(remote.recording.actions) == before - 1


def test_summary_reflects_the_session(remote):
    remote.start()
    remote._harness.push(*click_pair(1000, "QPushButton", "loginButton"))
    pump(remote, lambda: ActionKind.CLICK in kinds(remote))
    summary = remote.summary()
    assert summary["state"] == "recording"
    assert summary["actions"] >= 1


# --- checkpoints -----------------------------------------------------------

def test_checkpoint_round_trip(remote):
    picked = []
    remote.on_picked = lambda target, props: picked.append((target, props))
    remote.start()
    remote.arm_checkpoint()
    remote._harness.push(*click_pair(2000, "QLabel", "statusLabel"))

    assert pump(remote, lambda: bool(picked))
    target, properties = picked[0]
    assert target.label == "statusLabel"
    assert properties["text"] == "ready"

    remote.add_checkpoint("text", "ready")
    assert pump(remote, lambda: ActionKind.VERIFY_PROPERTY in kinds(remote))


def test_checkpoint_without_a_pick_is_refused(remote):
    remote.start()
    with pytest.raises(ControllerError, match="nothing has been picked"):
        remote.add_checkpoint("text")


# --- artifacts -------------------------------------------------------------

def test_save_writes_the_generated_files_locally(remote, tmp_path):
    remote.start()
    remote._harness.push(*click_pair(1000, "QPushButton", "loginButton"))
    pump(remote, lambda: ActionKind.CLICK in kinds(remote))
    remote.stop()

    written = remote.save(str(tmp_path))
    names = sorted(p.replace("\\", "/").rsplit("/", 1)[-1] for p in written)
    assert names == ["recorded.feature", "recording.json", "steps.py",
                     "test_recorded.py"]
    generated = (tmp_path / "test_recorded.py").read_text(encoding="utf-8")
    assert "qat.mouse_click" in generated
    compile(generated, "remote.py", "exec")


def test_secrets_are_not_written_by_save(remote, tmp_path):
    remote.start()
    remote._harness.nodes["password"].props["text"] = "hunter2"
    remote._harness.push(event("key_press", 900, "QLineEdit", "passwordField",
                               key=ord("H")))
    pump(remote, lambda: ActionKind.TYPE in kinds(remote))
    remote.stop()

    remote.save(str(tmp_path))
    for path in tmp_path.iterdir():
        assert "hunter2" not in path.read_text(encoding="utf-8")


def test_stop_releases_the_host(remote):
    remote.start()
    remote.stop()
    assert remote.state is State.STOPPED

    other_client = AgentClient("127.0.0.1", remote._harness.port, TOKEN,
                               owner="bob", insecure_plaintext=True, timeout=15)
    assert other_client.start(app="/opt/demo", lib="/l.so",
                              name="demo")["owner"] == "bob"
