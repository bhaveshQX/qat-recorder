# -*- coding: utf-8 -*-
"""
Closing a window from its title bar, and knowing when the filter is too old.

Two problems from one session. A file dialog was dismissed with the X in the
corner and the recording did not contain it, so on replay the dialog stayed
open, modal, over every step that followed -- each of which then failed for
reasons of its own. And clicks on table rows went missing with no explanation,
because the filter on that machine predated the support for reporting them.
"""

import pytest

from qat_recorder.capture import CaptureSession
from qat_recorder.emit import emit_python
from qat_recorder.events import Locator, RawEvent, parse_lines
from qat_recorder.ir import ActionKind
from tests.fixtures import build_tree
from tests.test_capture import event


def close_event(t, cls="QDialog", name="preferences"):
    return RawEvent(kind="close_window", t=t,
                    target=Locator(cls=cls, object_name=name))


# --- the X in the corner ----------------------------------------------------

def test_a_window_closed_from_its_title_bar_is_recorded():
    """No click reaches the application -- the decoration is the window
    manager's -- but Qt gets a close event, and it is spontaneous."""
    backend, nodes = build_tree()
    from qat_recorder.backend import FakeNode
    from tests.fixtures import WIDGET
    nodes["root"].add(FakeNode(["QDialog"] + WIDGET,
                               {"objectName": "preferences"}))

    capture = CaptureSession(backend, app_name="sample")
    capture.feed(close_event(1000))
    capture.feed(event("mouse_press", 3000, "QPushButton", "loginButton",
                       button=1))
    recording = capture.finish()

    closed = [a for a in recording.actions
              if a.kind is ActionKind.CLOSE_WINDOW]
    assert len(closed) == 1
    assert closed[0].target.definition == {"objectName": "preferences"}
    assert "title bar" in closed[0].note


def test_it_replays_by_asking_the_window_to_close():
    from qat_recorder.backend import FakeNode
    from tests.fixtures import WIDGET

    backend, nodes = build_tree()
    nodes["root"].add(FakeNode(["QDialog"] + WIDGET,
                               {"objectName": "preferences"}))
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(close_event(1000))
    capture.feed(event("mouse_press", 3000, "QPushButton", "loginButton",
                       button=1))

    source = emit_python(capture.finish())
    assert "qat.wait_for_object(find(PREFERENCES)).close()" in source
    compile(source, "generated.py", "exec")


def test_the_filter_reports_close_events():
    from qat_recorder.native import source_dir

    text = (source_dir() / "qatrec.cpp").read_text(encoding="utf-8")
    assert 'case QEvent::Close:' in text
    assert '"close_window"' in text


# --- knowing what the filter can do -----------------------------------------

def test_the_filter_announces_itself():
    events = list(parse_lines([
        '{"kind":"hello","protocol":2,'
        '"features":["menuItem","itemRow","closeWindow"]}']))
    assert events[0].kind == "hello"
    assert "itemRow" in events[0].features


def test_a_session_remembers_what_the_filter_can_do():
    backend, _ = build_tree()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(RawEvent(kind="hello", t=0,
                          target=Locator(), features=("menuItem", "itemRow")))
    capture.finish()
    assert capture.filter_features == {"menuItem", "itemRow"}


def test_an_old_filter_is_named_as_the_cause_not_the_view():
    """It reported the wrong thing for three rounds of debugging: a view that
    could not be addressed, when the truth was a filter that could not say."""
    from qat_recorder.backend import FakeNode
    from tests.fixtures import WIDGET

    backend, nodes = build_tree()
    tree = nodes["root"].add(FakeNode(["QTreeWidget"] + WIDGET,
                                      {"objectName": "fileTree"}))
    tree.add(FakeNode(WIDGET, {"row": 0}))

    capture = CaptureSession(backend, app_name="sample")   # no hello at all
    capture.feed(RawEvent(
        kind="mouse_press", t=1000, button=1, x=5, y=5,
        target=Locator(cls="QWidget", object_name="qt_scrollarea_viewport",
                       path=(("QTreeWidget", "fileTree"),
                             ("QWidget", "rootWidget")))))
    capture.feed(event("mouse_press", 5000, "QPushButton", "loginButton",
                       button=1))

    assert capture.failures
    assert "build-filter" in capture.failures[0]
    assert "row and column" in capture.failures[0]


def test_a_current_filter_gets_the_detailed_diagnosis_instead():
    from qat_recorder.backend import FakeNode
    from tests.fixtures import WIDGET

    backend, nodes = build_tree()
    tree = nodes["root"].add(FakeNode(["QTreeWidget"] + WIDGET,
                                      {"objectName": "fileTree"}))
    tree.add(FakeNode(WIDGET, {"row": 0, "colour": "red"}))

    capture = CaptureSession(backend, app_name="sample")
    capture.feed(RawEvent(kind="hello", t=0, target=Locator(),
                          features=("menuItem", "itemRow")))
    capture.feed(RawEvent(
        kind="mouse_press", t=1000, button=1, x=5, y=5,
        target=Locator(cls="QWidget", object_name="qt_scrollarea_viewport",
                       path=(("QTreeWidget", "fileTree"),
                             ("QWidget", "rootWidget")))))
    capture.feed(event("mouse_press", 5000, "QPushButton", "loginButton",
                       button=1))

    assert "build-filter" not in capture.failures[0]
    assert "row 0 exposes" in capture.failures[0]
