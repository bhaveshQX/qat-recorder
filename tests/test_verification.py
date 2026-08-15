# -*- coding: utf-8 -*-
"""
A test proves itself before it enters the library.

Every failure this project has had followed one shape: the recorder emits steps
it has never executed, and the operator finds out later. No amount of care about
locators fixes that -- the recorder is predicting the future, and only a replay
settles it. So saving runs it, once, while the application is still fresh in the
operator's mind, and the verdict is stored with the test.

A test in the library has either been proved to replay, or is marked as not
having replayed. There is no third state where nobody knows.
"""

import json

import pytest

from qat_recorder.library import TestLibrary
from qat_recorder.ui.controller import RecorderController
from tests.fixtures import build_tree
from tests.test_capture import click_pair
from tests.test_ui_controller import FakeQat, FakeReceiver

PASSING = {"test_recorded.py": "def test_ok():\n    assert True\n"}


@pytest.fixture()
def recorded(tmp_path, monkeypatch):
    """A controller with a finished recording, and a library in tmp_path."""
    monkeypatch.setenv("QATREC_TESTS", str(tmp_path))
    backend, _ = build_tree()
    receiver = FakeReceiver()
    controller = RecorderController(
        FakeQat(), lib_path="/tmp/lib.so", app_path="/tmp/sample",
        app_name="sample", backend=backend, receiver=receiver)
    controller.start()
    receiver.push(*click_pair(1000, "QPushButton", "loginButton"))
    controller.poll()
    controller.stop()
    return controller


def test_keeping_a_test_runs_it_and_stores_the_verdict(recorded, monkeypatch):
    seen = {}

    def fake_run(directory, **kwargs):
        seen["directory"] = str(directory)
        return {"ok": True, "output": "1 passed", "exit_code": 0}

    monkeypatch.setattr("qat_recorder.replay.run_pytest", fake_run)
    case = recorded.save_as("Sign in")

    assert case["verified"] == "passed"
    assert case["verified_at"]
    assert seen["directory"] == case["directory"]


def test_a_test_that_does_not_replay_is_marked_not_hidden(recorded, monkeypatch):
    """It is still saved. Losing the recording would be worse, and the operator
    is the one who decides what to do about it."""
    monkeypatch.setattr(
        "qat_recorder.replay.run_pytest",
        lambda directory, **kwargs: {
            "ok": False, "exit_code": 1,
            "output": "LookupError: Unable to find object: spinMaxConnec"})

    case = recorded.save_as("Preferences")
    assert case["verified"] == "failed"

    listed = TestLibrary().list("sample")
    assert [entry.verified for entry in listed] == ["failed"]


def test_why_it_failed_is_kept_with_it(recorded, monkeypatch):
    """Whoever reads this later wants to know why, not merely that."""
    monkeypatch.setattr(
        "qat_recorder.replay.run_pytest",
        lambda directory, **kwargs: {
            "ok": False, "exit_code": 1, "output": "spinMaxConnec not found"})

    case = recorded.save_as("Preferences")
    meta = json.loads(
        (TestLibrary().get(case["id"]).directory / "meta.json")
        .read_text(encoding="utf-8"))
    assert "spinMaxConnec not found" in meta["last_output"]


def test_verification_can_be_turned_off(recorded, monkeypatch):
    def refuse(directory, **kwargs):
        raise AssertionError("should not have run")

    monkeypatch.setattr("qat_recorder.replay.run_pytest", refuse)
    case = recorded.save_as("Sign in", verify=False)
    assert case["verified"] == ""


def test_a_never_run_test_is_not_reported_as_passing(tmp_path, monkeypatch):
    monkeypatch.setenv("QATREC_TESTS", str(tmp_path))
    library = TestLibrary()
    case = library.save("sample", "hand made", PASSING)
    assert case.verified == ""
    assert library.get(case.id).verified == ""


def test_the_verdict_survives_being_read_back(tmp_path, monkeypatch):
    monkeypatch.setenv("QATREC_TESTS", str(tmp_path))
    library = TestLibrary()
    case = library.save("sample", "one", PASSING)
    library.record_verdict(case, {"ok": True, "output": "1 passed"})

    reread = library.get(case.id)
    assert reread.verified == "passed"
    assert reread.verified_at == case.verified_at
