# -*- coding: utf-8 -*-
"""Tests for folding raw events into semantic actions."""

import json

import pytest

from qat_recorder.capture import CaptureSession, find_by_locator
from qat_recorder.events import Locator, RawEvent, parse_lines
from qat_recorder.ir import ActionKind, Robustness, is_secret
from tests.fixtures import build_tree

MOD_CONTROL = 0x04000000
KEY_TAB = 0x01000001


def event(kind, t, cls, object_name="", text="", title="", index=-1,
          path=(), **extra):
    return RawEvent(
        kind=kind, t=t,
        target=Locator(cls=cls, object_name=object_name, text=text,
                       title=title, index=index, path=tuple(path)),
        **extra)


def click_pair(t, cls, object_name="", **kwargs):
    return [
        event("mouse_press", t, cls, object_name, button=1, **kwargs),
        event("mouse_release", t + 40, cls, object_name, button=1, **kwargs),
    ]


@pytest.fixture()
def session():
    backend, nodes = build_tree()
    return CaptureSession(backend, app_name="sample"), backend, nodes


# --- folding ---------------------------------------------------------------

def test_press_and_release_become_one_click(session):
    capture, _, _ = session
    capture.feed_all(click_pair(1000, "QPushButton", "loginButton"))
    recording = capture.finish()

    kinds = [a.kind for a in recording.actions]
    assert kinds == [ActionKind.LAUNCH, ActionKind.CLICK]
    assert recording.actions[1].target.definition == {"objectName": "loginButton"}


def test_window_level_duplicate_is_discarded(session):
    """Qt delivers the click to the window and then the widget; only the widget
    interaction is meaningful."""
    capture, _, _ = session
    capture.feed(event("mouse_press", 500, "QWidgetWindow", "mainWindowWindow",
                       button=1))
    capture.feed(event("mouse_press", 500, "QPushButton", "loginButton", button=1,
                       path=[("QWidget", "rootWidget")]))
    capture.feed(event("mouse_release", 540, "QWidgetWindow", "mainWindowWindow",
                       button=1))
    capture.feed(event("mouse_release", 540, "QPushButton", "loginButton", button=1,
                       path=[("QWidget", "rootWidget")]))
    recording = capture.finish()

    actions = [a for a in recording.actions if a.kind is ActionKind.CLICK]
    assert len(actions) == 1
    assert actions[0].target.definition == {"objectName": "loginButton"}


def test_double_click_is_its_own_action(session):
    capture, _, _ = session
    capture.feed(event("mouse_double", 900, "QPushButton", "loginButton", button=1))
    recording = capture.finish()
    assert recording.actions[-1].kind is ActionKind.DOUBLE_CLICK


def test_right_click_becomes_context_click(session):
    capture, _, _ = session
    capture.feed_all([
        event("mouse_press", 100, "QPushButton", "loginButton", button=2),
        event("mouse_release", 130, "QPushButton", "loginButton", button=2),
    ])
    recording = capture.finish()
    assert recording.actions[-1].kind is ActionKind.CONTEXT_CLICK


def test_release_far_from_its_press_becomes_a_drag(session):
    capture, _, _ = session
    capture.feed(event("mouse_press", 0, "QPushButton", "loginButton",
                       button=1, x=10, y=10))
    capture.feed(event("mouse_release", 5000, "QLabel", "statusLabel",
                       button=1, x=60, y=90))
    recording = capture.finish()
    drag = recording.actions[-1]
    assert drag.kind is ActionKind.DRAG
    assert drag.args["dx"] == 50 and drag.args["dy"] == 80


# --- typing ----------------------------------------------------------------

def test_typing_reads_the_value_from_the_widget(session):
    """The keystrokes are never transmitted; the field's value is read back."""
    capture, _, nodes = session
    nodes["username"].props["text"] = "alice"

    for offset, key in enumerate(ord(c) for c in "ALICE"):
        capture.feed(event("key_press", 1000 + offset * 50, "QLineEdit",
                           "usernameField", key=key))
    recording = capture.finish()

    typed = [a for a in recording.actions if a.kind is ActionKind.TYPE]
    assert len(typed) == 1
    assert typed[0].args["text"] == "alice"
    assert typed[0].target.definition == {"objectName": "usernameField"}


def test_password_field_value_is_redacted(session):
    capture, _, nodes = session
    nodes["password"].props["text"] = "hunter2"

    for offset, key in enumerate(ord(c) for c in "HUNTER2"):
        capture.feed(event("key_press", 2000 + offset * 40, "QLineEdit",
                           "passwordField", key=key))
    recording = capture.finish()

    typed = [a for a in recording.actions if a.kind is ActionKind.TYPE][0]
    assert is_secret(typed.args["text"])
    assert "hunter2" not in json.dumps(recording.to_dict())
    assert recording.secrets()


def test_typing_in_a_second_field_closes_the_first_run(session):
    capture, _, nodes = session
    nodes["username"].props["text"] = "alice"
    nodes["password"].props["text"] = "secret"

    capture.feed(event("key_press", 100, "QLineEdit", "usernameField", key=ord("A")))
    capture.feed(event("key_press", 200, "QLineEdit", "passwordField", key=ord("B")))
    recording = capture.finish()

    typed = [a for a in recording.actions if a.kind is ActionKind.TYPE]
    assert len(typed) == 2
    assert typed[0].args["text"] == "alice"
    assert is_secret(typed[1].args["text"])


def test_a_click_interrupts_a_typing_run(session):
    capture, _, nodes = session
    nodes["username"].props["text"] = "alice"

    capture.feed(event("key_press", 100, "QLineEdit", "usernameField", key=ord("A")))
    capture.feed_all(click_pair(500, "QPushButton", "loginButton"))
    recording = capture.finish()

    kinds = [a.kind for a in recording.actions]
    assert kinds.index(ActionKind.TYPE) < kinds.index(ActionKind.CLICK)


def test_typing_into_a_non_text_widget_is_not_recorded_as_typing(session):
    """A QCheckBox has a `text` property too, but it holds the caption.

    Found in the end-to-end run: Tab moved focus onto a checkbox mid-typing and
    the recorder emitted `type_in(rememberBox, "Remember me")` -- the widget's
    own label, played back as if the user had typed it.
    """
    capture, _, _ = session
    capture.feed(event("key_press", 100, "QCheckBox", "rememberBox", key=ord("H")))
    recording = capture.finish()

    kinds = [a.kind for a in recording.actions]
    assert ActionKind.TYPE not in kinds
    assert recording.actions[-1].kind is ActionKind.KEY
    assert recording.actions[-1].args["key"] == "H"


def test_typing_into_a_line_edit_is_still_recorded_as_typing(session):
    capture, _, nodes = session
    nodes["username"].props["text"] = "alice"
    capture.feed(event("key_press", 100, "QLineEdit", "usernameField", key=ord("A")))
    recording = capture.finish()
    assert recording.actions[-1].kind is ActionKind.TYPE
    assert recording.actions[-1].args["text"] == "alice"


# --- keys ------------------------------------------------------------------

def test_modifier_keys_produce_shortcuts(session):
    capture, _, _ = session
    capture.feed(event("key_press", 300, "QLineEdit", "usernameField",
                       key=ord("S"), modifiers=MOD_CONTROL))
    recording = capture.finish()
    action = recording.actions[-1]
    assert action.kind is ActionKind.SHORTCUT
    assert action.args["keys"] == "Ctrl+S"


def test_shortcuts_are_attributed_to_the_window_not_the_focused_widget(session):
    """Qt dispatches shortcuts at window scope.

    Recording Ctrl+S against whichever field had focus made replay fail with
    "No widget accepted this event" -- found by actually running a generated
    test rather than only compiling it.
    """
    capture, _, nodes = session
    capture.feed(event("key_press", 300, "QLineEdit", "usernameField",
                       key=ord("S"), modifiers=MOD_CONTROL))
    recording = capture.finish()

    action = recording.actions[-1]
    assert action.kind is ActionKind.SHORTCUT
    assert action.target.definition == {"objectName": "mainWindow"}


def test_native_shortcut_event_is_recorded_directly(session):
    """A shortcut bound to a QAction never arrives as a key press.

    Qt consumes it and sends QEvent::Shortcut to the action's owner, so the
    native filter reports it separately, already formatted and already aimed at
    the right object.
    """
    capture, _, _ = session
    capture.feed(RawEvent(
        kind="shortcut", t=400,
        target=Locator(cls="QMainWindow", object_name="mainWindow"),
        keys="Ctrl+S"))
    recording = capture.finish()

    action = recording.actions[-1]
    assert action.kind is ActionKind.SHORTCUT
    assert action.args["keys"] == "Ctrl+S"
    assert action.target.definition == {"objectName": "mainWindow"}


def test_shortcut_delivered_to_a_non_widget_retargets_to_the_window(session):
    """Qt sends QEvent::Shortcut to the QAction that owns the shortcut.

    A QAction is not a widget, so replaying against it fails with "Associated
    widget was not found" -- found by running a generated test, not by compiling
    it. The owning window is both a widget and a stable target.
    """
    capture, _, _ = session
    capture.feed(RawEvent(
        kind="shortcut", t=400,
        target=Locator(cls="QAction", object_name="saveAction",
                       path=(("QMainWindow", "mainWindow"),)),
        keys="Ctrl+S"))
    recording = capture.finish()

    assert recording.actions[-1].target.definition == {"objectName": "mainWindow"}


def test_shortcut_prefers_the_nearest_named_ancestor(session):
    """Not the outermost. Walking all the way up lands on a QWidgetWindow, which
    is a QWindow rather than a QWidget and fails to replay identically."""
    capture, _, _ = session
    capture.feed(RawEvent(
        kind="shortcut", t=400,
        target=Locator(cls="QAction", object_name="saveAction",
                       path=(("QGroupBox", "credentialsGroup"),
                             ("QWidget", "rootWidget"),
                             ("QMainWindow", "mainWindow"))),
        keys="Ctrl+P"))
    recording = capture.finish()

    assert recording.actions[-1].target.definition == {
        "objectName": "credentialsGroup"}


def test_shortcut_falls_back_when_no_ancestor_can_be_named(session):
    capture, backend, nodes = session
    capture.feed(event("key_press", 300, "QMainWindow", "mainWindow",
                       key=ord("S"), modifiers=MOD_CONTROL))
    recording = capture.finish()
    assert recording.actions[-1].target.definition == {"objectName": "mainWindow"}


def test_special_keys_are_recorded_by_name(session):
    capture, _, _ = session
    capture.feed(event("key_press", 300, "QLineEdit", "usernameField", key=KEY_TAB))
    recording = capture.finish()
    assert recording.actions[-1].kind is ActionKind.KEY
    assert recording.actions[-1].args["key"] == "Tab"


def test_bare_modifier_press_is_ignored(session):
    capture, _, _ = session
    capture.feed(event("key_press", 300, "QLineEdit", "usernameField", key=0x01000020))
    recording = capture.finish()
    assert [a.kind for a in recording.actions] == [ActionKind.LAUNCH]


# --- locator resolution ----------------------------------------------------

def test_locator_resolves_unnamed_object_by_text(session):
    _, backend, nodes = session
    node = find_by_locator(backend, Locator(cls="QPushButton", text="Import"))
    assert node is nodes["import_btn"]


def test_locator_uses_ancestors_to_disambiguate(session):
    _, backend, nodes = session
    node = find_by_locator(backend, Locator(
        cls="QPushButton", text="Export",
        path=(("QGroupBox", "advancedGroup"), ("QWidget", "rootWidget"))))
    assert node is nodes["export_inner"]


def test_locator_uses_index_for_identical_siblings(session):
    _, backend, nodes = session
    first = find_by_locator(backend, Locator(
        cls="QPushButton", text="Apply", index=0,
        path=(("QGroupBox", "duplicateGroup"),)))
    second = find_by_locator(backend, Locator(
        cls="QPushButton", text="Apply", index=1,
        path=(("QGroupBox", "duplicateGroup"),)))
    assert first is nodes["apply_a"]
    assert second is nodes["apply_b"]


def test_unresolvable_events_are_counted_not_guessed(session):
    capture, _, _ = session
    capture.feed_all(click_pair(100, "QNotARealClass", "nothingLikeThis"))
    recording = capture.finish()
    assert capture.unresolved > 0
    assert [a.kind for a in recording.actions] == [ActionKind.LAUNCH]


def test_one_failing_event_does_not_destroy_the_recording():
    """A recording is minutes of someone's time.

    Qat raises rather than returning an empty list when nothing matches, and it
    adds `visible: true` to every definition — so a click on a widget that is
    later hidden used to abort the whole session and discard every event with
    it. Reproduced here with a backend that raises on one specific lookup.
    """
    backend, nodes = build_tree()
    real_find_all = backend.find_all

    def exploding_find_all(definition):
        if definition.get("objectName") == "statusLabel":
            raise LookupError("Unable to find object: no object found")
        return real_find_all(definition)

    backend.find_all = exploding_find_all
    capture = CaptureSession(backend, app_name="sample")

    capture.feed_all([
        *click_pair(1000, "QPushButton", "loginButton"),
        *click_pair(2000, "QLabel", "statusLabel"),      # this one explodes
        *click_pair(3000, "QComboBox", "envSelector"),
    ])
    recording = capture.finish()

    kinds = [a.kind for a in recording.actions]
    assert kinds.count(ActionKind.CLICK) == 2, "the good events must survive"
    assert capture.unresolved > 0
    assert capture.failures, "and the operator must be told why"


# --- wire format -----------------------------------------------------------

def test_parse_lines_skips_malformed_records():
    lines = [
        '{"kind": "mouse_press", "t": 1, "target": {"class": "QPushButton"}}',
        "not json at all",
        "",
        '{"kind": "key_press", "t": 2, "key": 65, "target": {"class": "QLineEdit"}}',
    ]
    events = list(parse_lines(lines))
    assert [e.kind for e in events] == ["mouse_press", "key_press"]
    assert events[1].key == 65


def test_real_native_record_parses():
    """A verbatim record produced by native/qatrec.cpp in the container."""
    line = ('{"kind": "mouse_press", "t": 1786533080600, "button": 1, '
            '"modifiers": 0, "x": 51, "y": 51, "target": {"class": "QGroupBox", '
            '"objectName": "credentialsGroup", "title": "Credentials", '
            '"index": 0, "path": [{"class": "QWidget", "objectName": '
            '"rootWidget"}, {"class": "QMainWindow", "objectName": '
            '"mainWindow"}]}}')
    parsed = list(parse_lines([line]))[0]
    assert parsed.kind == "mouse_press"
    assert parsed.target.object_name == "credentialsGroup"
    assert parsed.target.title == "Credentials"
    assert parsed.target.nearest_named_ancestor() == "rootWidget"
    assert not parsed.target.is_window
