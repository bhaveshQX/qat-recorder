# -*- coding: utf-8 -*-
"""Tests for the code generators and the player."""

import pytest

from qat_recorder.capture import CaptureSession
from qat_recorder.emit import emit_gherkin, emit_python, emit_steps
from qat_recorder.ir import (
    Action, ActionKind, Recording, Robustness, Target, secret_ref,
)
from qat_recorder.player import MissingSecret, Player
from tests.fixtures import build_tree
from tests.test_capture import click_pair, event


@pytest.fixture()
def recording():
    """A realistic session: type a user name, type a password, click Sign in,
    then click one of the two identical Apply buttons."""
    backend, nodes = build_tree()
    nodes["username"].props["text"] = "alice"
    nodes["password"].props["text"] = "hunter2"

    capture = CaptureSession(backend, app_name="sample", app_path="/opt/acme/sample")
    capture.feed(event("key_press", 1000, "QLineEdit", "usernameField", key=ord("A")))
    capture.feed(event("key_press", 1600, "QLineEdit", "passwordField", key=ord("H")))
    capture.feed_all(click_pair(2200, "QPushButton", "loginButton"))
    capture.feed_all(click_pair(
        3000, "QPushButton", text="Apply", index=1,
        path=[("QGroupBox", "duplicateGroup"), ("QWidget", "rootWidget")]))
    return capture.finish()


# --- python emitter --------------------------------------------------------

def test_generated_python_compiles(recording):
    source = emit_python(recording)
    compile(source, "generated.py", "exec")


def test_generated_python_uses_qat_api(recording):
    source = emit_python(recording)
    assert "import qat" in source
    assert "qat.start_application(APP_NAME)" in source
    assert "qat.mouse_click(" in source
    assert "qat.type_in(" in source


def test_generated_test_registers_the_application_itself(recording):
    """`start_application` takes a registered NAME, not a path.

    Without registration the generated test fails on every machine except the
    one it was recorded on, with "Application '...' is not defined in
    configuration file 'applications.json'". Found on a real VM.
    """
    source = emit_python(recording)
    assert "APP_NAME = 'sample'" in source
    assert "APP_PATH = '/opt/acme/sample'" in source
    assert "if APP_NAME not in qat.list_applications():" in source
    assert "qat.register_application(APP_NAME, APP_PATH)" in source
    compile(source, "generated.py", "exec")


def test_generated_steps_register_the_application_too(recording):
    steps = emit_steps(recording)
    assert "APP_PATH = '/opt/acme/sample'" in steps
    assert "qat.register_application(name, APP_PATH)" in steps
    compile(steps, "steps.py", "exec")


def test_a_recording_without_a_path_still_emits(recording):
    """Older recordings have no app_path; they must still generate, just
    without the self-registration convenience."""
    recording.meta.pop("app_path", None)
    source = emit_python(recording)
    assert "qat.start_application(APP_NAME)" in source
    assert "register_application" not in source
    compile(source, "generated.py", "exec")


def test_secrets_never_appear_as_literals(recording):
    source = emit_python(recording)
    assert "hunter2" not in source
    assert "secret('passwordField')" in source
    assert "alice" in source          # non-secret values are kept


def test_positional_target_becomes_a_lookup(recording):
    source = emit_python(recording)
    assert "qat.find_all_objects(" in source
    assert "[1]" in source


def test_fragile_targets_are_flagged_in_the_output(recording):
    source = emit_python(recording)
    assert "# fragile:" in source
    assert "Review before relying on this script:" in source


def test_definitions_become_named_constants(recording):
    source = emit_python(recording)
    assert "LOGINBUTTON = {'objectName': 'loginButton'}" in source


def test_a_menu_step_clicks_the_item_by_name():
    """No coordinates anywhere in the generated script — for anything."""
    backend, _ = build_tree()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(event("mouse_press", 1000, "QMenuBar", "menubar",
                       button=1, x=42, y=11, menu_item="&Options"))
    capture.feed(event("mouse_release", 1040, "QMenu", "menuOptions",
                       button=1, x=6, y=-11))
    capture.feed(event("mouse_press", 2000, "QMenu", "menuOptions",
                       button=1, x=40, y=30, menu_item="&Preferences"))
    capture.feed(event("mouse_release", 2050, "QMenu", "menuOptions",
                       button=1, x=40, y=30, menu_item="&Preferences"))

    source = emit_python(capture.finish())
    assert "OPTIONS = {'container': {'objectName': 'menubar'}, 'text': 'Options'}" \
        in source
    assert "qat.mouse_click(OPTIONS)  # opens the menu" in source
    assert "qat.mouse_click(PREFERENCES)" in source
    compile(source, "generated.py", "exec")


def test_ordinary_clicks_carry_no_coordinates(recording):
    source = emit_python(recording)
    assert "qat.mouse_click(LOGINBUTTON)" in source


def test_emitting_an_invalid_recording_is_refused():
    broken = Recording(app="x")
    broken.add(Action(ActionKind.CLICK, target=None))
    with pytest.raises(ValueError, match="invalid recording"):
        emit_python(broken)


def test_duplicate_labels_get_distinct_constants():
    rec = Recording(app="x")
    for name in ("a", "b"):
        rec.add(Action(ActionKind.CLICK, target=Target(
            definition={"objectName": name}, label="same", strategy="objectName",
            robustness=Robustness.STRONG)))
    source = emit_python(rec)
    assert "SAME = " in source
    assert "SAME_2 = " in source
    compile(source, "generated.py", "exec")


# --- gherkin emitter -------------------------------------------------------

def test_gherkin_has_the_expected_shape(recording):
    feature = emit_gherkin(recording)
    assert "Feature: Recorded session" in feature
    assert "Scenario:" in feature
    assert 'Given the application "sample" is running' in feature
    # Keyword-agnostic: repeated steps chain with `And`, so asserting on the
    # keyword here would just pin whichever step happened to come first.
    assert 'I click "loginButton"' in feature


def test_gherkin_hides_secrets_behind_a_named_step(recording):
    feature = emit_gherkin(recording)
    assert "hunter2" not in feature
    assert 'I type the secret "passwordField" into "passwordField"' in feature
    assert 'I type "alice" into "usernameField"' in feature


def test_gherkin_flags_fragile_steps(recording):
    feature = emit_gherkin(recording)
    assert "# fragile:" in feature


def test_repeated_keywords_are_chained_with_and(recording):
    """Four `When`s in a row reads like generated output, not a specification."""
    feature = emit_gherkin(recording)
    steps = [line.strip() for line in feature.splitlines()
             if line.strip().startswith(("Given ", "When ", "And ", "Then "))]
    assert steps[0].startswith("Given ")
    assert steps[1].startswith("When ")
    assert all(step.startswith("And ") for step in steps[2:]), steps
    assert feature.count("When I") == 1


def test_generated_steps_compile_and_carry_the_dictionary(recording):
    steps = emit_steps(recording)
    compile(steps, "steps.py", "exec")
    assert "OBJECTS = {" in steps
    assert '"loginButton"' in steps
    assert '"index": 1' in steps
    assert "hunter2" not in steps


def test_feature_names_objects_by_label_not_definition(recording):
    """Keeps the scenario readable, and keeps its diff stable when objects are
    re-resolved."""
    feature = emit_gherkin(recording)
    assert "objectName" not in feature


# --- player ----------------------------------------------------------------

class FakeQat:
    """Records the calls a real qat module would have received."""

    def __init__(self, matches=None):
        self.calls = []
        self._matches = matches or {}

    def start_application(self, name):
        self.calls.append(("start", name))
        return f"ctx:{name}"

    def close_application(self, context=None):
        self.calls.append(("close", context))

    def mouse_click(self, target, **kwargs):
        self.calls.append(("click", target))

    def double_click(self, target, **kwargs):
        self.calls.append(("double_click", target))

    def type_in(self, target, text):
        self.calls.append(("type", target, text))

    def press_key(self, target, key):
        self.calls.append(("key", target, key))

    def shortcut(self, target, keys):
        self.calls.append(("shortcut", target, keys))

    def find_all_objects(self, definition):
        return self._matches.get(repr(sorted(definition.items())), [])


def test_player_replays_every_action(recording):
    apply_key = repr(sorted(
        {"type": "QPushButton", "text": "Apply",
         "parent": {"objectName": "duplicateGroup"}}.items()))
    fake = FakeQat(matches={apply_key: ["apply_a", "apply_b"]})
    player = Player(fake, secrets={"passwordField": "hunter2"})

    played = player.play(recording)
    assert played == len(recording.actions)
    assert ("start", "sample") in fake.calls
    assert ("type", {"objectName": "usernameField"}, "alice") in fake.calls
    assert ("type", {"objectName": "passwordField"}, "hunter2") in fake.calls


def test_player_refuses_without_the_secret(recording):
    player = Player(FakeQat(), secrets={})
    with pytest.raises(MissingSecret, match="passwordField"):
        player.play(recording)


def test_player_explains_a_stale_positional_target():
    rec = Recording(app="x")
    rec.add(Action(ActionKind.CLICK, target=Target(
        definition={"type": "QPushButton"}, label="Apply",
        robustness=Robustness.FRAGILE, index=3)))
    player = Player(FakeQat(matches={}))
    with pytest.raises(LookupError, match="the UI has changed since recording"):
        player.play(rec)


def test_player_refuses_an_invalid_recording():
    broken = Recording(app="x")
    broken.add(Action(ActionKind.CLICK, target=None))
    with pytest.raises(ValueError, match="invalid recording"):
        Player(FakeQat()).play(broken)
