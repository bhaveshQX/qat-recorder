# -*- coding: utf-8 -*-
"""
The three things that were still open, and what was done about them.

Timing, assumed state, and controls the application gives no durable name.
"""

import json

import pytest

from qat_recorder.emit import emit_python
from qat_recorder.emit.python import (
    MAX_TIMEOUT_MS, MIN_TIMEOUT_MS, derived_timeout, emit_object_map,
)
from qat_recorder.ir import Action, ActionKind, Recording, Robustness, Target
from tests.test_items import _built, view_session          # noqa: F401


def target(label="thing", robustness=Robustness.STRONG, **definition):
    return Target(definition=definition or {"objectName": label},
                  strategy="objectName", robustness=robustness, label=label)


# --- timing -----------------------------------------------------------------

def test_a_brisk_session_still_waits_longer_than_qats_three_seconds():
    """Qat's default is 3s, which is fine for a button and not for a dialog."""
    recording = Recording(app="x")
    for when in (0.0, 0.2, 0.4):
        recording.add(Action(ActionKind.CLICK, target=target(), t=when))
    assert derived_timeout(recording) == MIN_TIMEOUT_MS


def test_a_long_pause_becomes_a_long_wait():
    """The person waited eleven seconds, so the application takes that long."""
    recording = Recording(app="x")
    recording.add(Action(ActionKind.CLICK, target=target(), t=0.0))
    recording.add(Action(ActionKind.CLICK, target=target(), t=11.0))
    assert derived_timeout(recording) == 22_000


def test_a_coffee_break_does_not_become_an_hour_long_hang():
    recording = Recording(app="x")
    recording.add(Action(ActionKind.CLICK, target=target(), t=0.0))
    recording.add(Action(ActionKind.CLICK, target=target(), t=900.0))
    assert derived_timeout(recording) == MAX_TIMEOUT_MS


def test_the_timeout_is_applied_and_can_be_overridden_from_the_environment():
    recording = Recording(app="x")
    recording.add(Action(ActionKind.CLICK, target=target(), t=0.0))
    recording.add(Action(ActionKind.CLICK, target=target(), t=11.0))

    source = emit_python(recording)
    assert "TIMEOUT_MS = int(os.environ.get('QATREC_TIMEOUT_MS') or 22000)" in source
    assert "qat.Settings.wait_for_object_timeout = TIMEOUT_MS" in source
    compile(source, "generated.py", "exec")


# --- assumed state ----------------------------------------------------------

def test_a_test_that_needs_data_says_so_before_it_touches_anything(view_session):
    capture, _, _ = view_session
    capture.feed(_built("mouse_press", 1000, 1, 0, "beta.txt"))
    capture.feed(_built("mouse_release", 1040, 1, 0, "beta.txt"))

    source = emit_python(capture.finish())
    assert "def preconditions():" in source
    assert "'beta.txt'" in source
    assert "recorded against data that is not here" in source
    # And it runs first, before any step.
    body = source.split("def test_recorded_session(application):")[1]
    assert body.strip().startswith("preconditions()")
    compile(source, "generated.py", "exec")


def test_a_session_that_assumes_nothing_gets_no_precondition_check():
    recording = Recording(app="x")
    recording.add(Action(ActionKind.CLICK, target=target(), t=0.0))
    assert "def preconditions():" not in emit_python(recording)


def test_every_missing_row_is_named_at_once(view_session):
    """Failing on the first would hide the second."""
    capture, _, _ = view_session
    capture.feed(_built("mouse_press", 1000, 0, 0, "alpha.txt"))
    capture.feed(_built("mouse_release", 1040, 0, 0, "alpha.txt"))
    capture.feed(_built("mouse_press", 2000, 2, 0, "gamma.txt"))
    capture.feed(_built("mouse_release", 2040, 2, 0, "gamma.txt"))

    source = emit_python(capture.finish())
    assert source.count("missing.append") == 2
    assert "', '.join(missing)" in source


# --- controls with no durable name ------------------------------------------

def test_definitions_can_be_replaced_from_an_object_map():
    recording = Recording(app="x")
    recording.add(Action(ActionKind.CLICK, target=Target(
        definition={"type": "QPushButton", "text": "OK"}, strategy="type+text",
        robustness=Robustness.WEAK, label="OK"), t=0.0))

    source = emit_python(recording)
    assert "OK = override('OK', [{'text': 'OK', 'type': 'QPushButton'}])" in source
    assert "def override(name, definition):" in source
    compile(source, "generated.py", "exec")


def test_the_object_map_ships_empty_but_shows_what_needs_fixing():
    recording = Recording(app="x")
    recording.add(Action(ActionKind.CLICK, target=Target(
        definition={"type": "QPushButton", "text": "OK"}, strategy="type+text",
        robustness=Robustness.WEAK, label="OK"), t=0.0))
    recording.add(Action(ActionKind.CLICK, target=target("loginButton"), t=1.0))

    document = json.loads(emit_object_map(recording))
    # Nothing is overridden until a person puts it there.
    assert [key for key in document if not key.startswith("_")] == []
    # But the weak one is listed, with what the recorder settled for.
    assert document["_weak"] == {"OK": {"type": "QPushButton", "text": "OK"}}
    assert "LOGINBUTTON" not in document["_weak"]      # strong: nothing to fix


def test_an_override_only_counts_when_it_is_a_definition(tmp_path):
    """The loader ignores the help text and anything that is not a dictionary,
    so a hand-edited file cannot turn a definition into a string."""
    recording = Recording(app="x")
    recording.add(Action(ActionKind.CLICK, target=target("loginButton"), t=0.0))
    source = emit_python(recording)

    namespace: dict = {}
    (tmp_path / "objects.json").write_text(json.dumps({
        "_help": ["ignored"],
        "_weak": {"LOGINBUTTON": {"objectName": "no"}},
        "LOGINBUTTON": {"objectName": "renamedLoginButton"},
        "BROKEN": "not a definition",
    }), encoding="utf-8")

    module = tmp_path / "test_recorded.py"
    module.write_text(source, encoding="utf-8")
    code = compile(source, str(module), "exec")
    namespace["__file__"] = str(module)
    exec(code, namespace)                                # noqa: S102

    assert namespace["LOGINBUTTON"] == {"objectName": "renamedLoginButton"}
    assert "BROKEN" not in namespace["OVERRIDES"]
    assert "_help" not in namespace["OVERRIDES"]
