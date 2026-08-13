# -*- coding: utf-8 -*-
"""Tests for the recording session controller (no Qt, no application)."""

import json

import pytest

from qat_recorder.ir import ActionKind
from qat_recorder.ui.controller import ControllerError, RecorderController, State
from tests.fixtures import build_tree
from tests.test_capture import click_pair, event


class FakeReceiver:
    def __init__(self):
        self.pending = []
        self.actual_port = 45999
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def drain(self, timeout=0.0):
        pending, self.pending = self.pending, []
        return pending

    def push(self, *events):
        self.pending.extend(events)


class FakeSettings:
    lock_ui = "auto"


class FakeTestSettings:
    Settings = FakeSettings


class FakeQat:
    test_settings = FakeTestSettings

    def __init__(self):
        self.calls = []

    def register_application(self, name, path, args=""):
        self.calls.append(("register", name))

    def unregister_application(self, name, shared=None):
        self.calls.append(("unregister", name))

    def start_application(self, name, args=None, detached=False):
        self.calls.append(("start", name))
        return f"ctx:{name}"

    def close_application(self, context=None):
        self.calls.append(("close", context))


@pytest.fixture()
def controller():
    backend, nodes = build_tree()
    nodes["username"].props["text"] = "alice"
    receiver = FakeReceiver()
    qat = FakeQat()
    states = []
    actions = []
    picked = []
    errors = []
    ctrl = RecorderController(
        qat, lib_path="/tmp/lib.so", app_path="/tmp/app", app_name="sample",
        backend=backend, receiver=receiver,
        on_state=states.append,
        on_action=actions.append,
        on_picked=lambda target, props: picked.append((target, props)),
        on_error=errors.append,
    )
    ctrl._test = {"states": states, "actions": actions, "picked": picked,
                  "errors": errors, "receiver": receiver, "qat": qat,
                  "nodes": nodes}
    return ctrl


# --- lifecycle -------------------------------------------------------------

def test_start_launches_the_application_and_records(controller):
    controller.start()
    assert controller.state is State.RECORDING
    assert ("register", "_qat_recorder_session") in controller._test["qat"].calls
    assert ("start", "_qat_recorder_session") in controller._test["qat"].calls
    assert controller._test["receiver"].started


def test_start_disables_qats_ui_lock(controller):
    """Qat locks input during a test run; while recording that would block the
    very input being captured."""
    controller.start()
    assert controller._test["qat"].test_settings.Settings.lock_ui == "never"


def test_stop_closes_and_unregisters(controller):
    controller.start()
    controller.stop()
    calls = controller._test["qat"].calls
    assert ("close", "ctx:_qat_recorder_session") in calls
    assert ("unregister", "_qat_recorder_session") in calls
    assert controller.state is State.STOPPED
    assert not controller._test["receiver"].started


def test_cannot_start_twice(controller):
    controller.start()
    with pytest.raises(ControllerError, match="cannot start"):
        controller.start()


def test_cannot_stop_before_starting(controller):
    with pytest.raises(ControllerError, match="nothing to stop"):
        controller.stop()


# --- the event pump --------------------------------------------------------

def test_events_become_actions(controller):
    controller.start()
    controller._test["receiver"].push(
        *click_pair(1000, "QPushButton", "loginButton"))
    controller.poll()
    recording = controller.stop()

    kinds = [a.kind for a in recording.actions]
    assert ActionKind.CLICK in kinds
    assert any(a.kind is ActionKind.CLICK for a in controller._test["actions"])


def test_paused_events_are_discarded(controller):
    controller.start()
    controller.pause()
    controller._test["receiver"].push(
        *click_pair(1000, "QPushButton", "loginButton"))
    controller.poll()
    controller.resume()
    recording = controller.stop()

    assert [a.kind for a in recording.actions] == [ActionKind.LAUNCH]
    assert controller.events_dropped >= 2


def test_resume_discards_whatever_arrived_while_paused(controller):
    controller.start()
    controller.pause()
    controller._test["receiver"].push(
        *click_pair(1000, "QPushButton", "loginButton"))
    controller.resume()          # drains without recording
    controller.poll()
    recording = controller.stop()
    assert [a.kind for a in recording.actions] == [ActionKind.LAUNCH]


def test_cannot_pause_unless_recording(controller):
    with pytest.raises(ControllerError, match="cannot pause"):
        controller.pause()


# --- checkpoints -----------------------------------------------------------

def test_picking_a_target_does_not_record_the_click(controller):
    """The click that selects a checkpoint target is an instruction to the
    recorder, not part of the scenario."""
    controller.start()
    controller.arm_checkpoint()
    assert controller.state is State.PICKING

    controller._test["receiver"].push(
        *click_pair(2000, "QLabel", "statusLabel"))
    controller.poll()

    assert controller.state is State.RECORDING
    assert controller.picked_target.definition == {"objectName": "statusLabel"}
    recording = controller.recording
    assert [a.kind for a in recording.actions] == [ActionKind.LAUNCH]


def test_the_release_after_a_pick_does_not_become_a_drag(controller):
    """Dropping only the press would leave the folder with an orphan release,
    which it would reasonably interpret as a drag."""
    controller.start()
    controller.arm_checkpoint()
    controller._test["receiver"].push(
        *click_pair(2000, "QLabel", "statusLabel"))
    controller.poll()
    controller._test["receiver"].push(
        *click_pair(3000, "QPushButton", "loginButton"))
    controller.poll()
    recording = controller.stop()

    kinds = [a.kind for a in recording.actions]
    assert ActionKind.DRAG not in kinds
    assert kinds.count(ActionKind.CLICK) == 1


def test_checkpoint_records_the_current_value(controller):
    controller.start()
    controller.arm_checkpoint()
    controller._test["receiver"].push(
        *click_pair(2000, "QLabel", "statusLabel"))
    controller.poll()

    action = controller.add_checkpoint("text")
    assert action.kind is ActionKind.VERIFY_PROPERTY
    assert action.args == {"property": "text", "expected": "ready"}
    assert controller._test["picked"]


def test_checkpoint_accepts_an_explicit_expected_value(controller):
    controller.start()
    controller.arm_checkpoint()
    controller._test["receiver"].push(*click_pair(2000, "QLabel", "statusLabel"))
    controller.poll()
    action = controller.add_checkpoint("text", expected="signed in")
    assert action.args["expected"] == "signed in"


def test_checkpoint_without_a_pick_is_refused(controller):
    controller.start()
    with pytest.raises(ControllerError, match="nothing has been picked"):
        controller.add_checkpoint("text")


def test_picking_something_unidentifiable_reports_instead_of_guessing(controller):
    controller.start()
    controller.arm_checkpoint()
    controller._test["receiver"].push(
        *click_pair(2000, "QNotAClass", "nothingHere"))
    controller.poll()
    assert controller.picked_target is None
    assert controller._test["errors"]
    assert controller.state is State.RECORDING


def test_cancel_checkpoint_returns_to_recording(controller):
    controller.start()
    controller.arm_checkpoint()
    controller.cancel_checkpoint()
    assert controller.state is State.RECORDING


# --- editing ---------------------------------------------------------------

def test_drop_last_action_undoes_a_misclick(controller):
    controller.start()
    controller._test["receiver"].push(*click_pair(1000, "QPushButton", "loginButton"))
    controller.poll()
    assert controller.summary()["actions"] == 1

    controller.drop_last_action()
    assert controller.summary()["actions"] == 0


def test_drop_last_action_never_removes_the_launch(controller):
    controller.start()
    assert controller.drop_last_action() is None
    assert controller.recording.actions[0].kind is ActionKind.LAUNCH


# --- reporting and output --------------------------------------------------

def test_summary_counts_what_the_operator_needs_to_see(controller):
    controller.start()
    controller._test["nodes"]["password"].props["text"] = "hunter2"
    controller._test["receiver"].push(
        event("key_press", 500, "QLineEdit", "passwordField", key=ord("H")),
        *click_pair(1500, "QPushButton", text="Apply", index=0,
                    path=[("QGroupBox", "duplicateGroup")]),
    )
    controller.poll()
    summary = controller.summary()

    assert summary["state"] == "recording"
    assert summary["actions"] == 2
    assert summary["secrets"] == 1
    assert summary["fragile"] >= 1


def test_save_writes_every_artifact(controller, tmp_path):
    controller.start()
    controller._test["receiver"].push(*click_pair(1000, "QPushButton", "loginButton"))
    controller.poll()
    controller.stop()

    written = controller.save(str(tmp_path))
    names = sorted(p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for p in written)
    assert names == ["recorded.feature", "recording.json", "steps.py",
                     "test_recorded.py"]
    payload = json.loads((tmp_path / "recording.json").read_text(encoding="utf-8"))
    assert payload["app"] == "sample"


def test_save_refuses_an_empty_session(controller, tmp_path):
    with pytest.raises(ControllerError, match="nothing recorded"):
        controller.save(str(tmp_path))
