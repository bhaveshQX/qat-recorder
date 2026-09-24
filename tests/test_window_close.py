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
from tests.test_capture import click_pair, event


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


# --- close events that are nobody closing anything --------------------------

def _session():
    backend, _ = build_tree()
    return CaptureSession(backend, app_name="qbittorrent")


def _steps(capture):
    return [action for action in capture.finish().actions
            if action.kind is not ActionKind.LAUNCH]


def test_the_window_qt_wraps_a_widget_in_is_not_a_step():
    """`menuOptionsWindow` was recorded after nearly every step.

    Every top-level widget has a QWidgetWindow, named after it with "Window"
    appended. The window system's close reaches it first and is passed on to
    the widget, which gets its own -- so this one is always a duplicate, and its
    name is not one Qat can address. Since reported targets were kept rather
    than dropped, it became `close()` on an object that does not exist, and the
    replay failed there.
    """
    capture = _session()
    capture.feed_all(click_pair(100, "QPushButton", "loginButton"))
    capture.feed(event("close_window", 300, "QWidgetWindow", "menuOptionsWindow"))
    capture.feed_all(click_pair(500, "QPushButton", "loginButton"))

    kinds = [step.kind for step in _steps(capture)]
    assert ActionKind.CLOSE_WINDOW not in kinds
    assert kinds == [ActionKind.CLICK, ActionKind.CLICK]


def test_a_menu_closing_is_not_a_step():
    """A menu closes every time an item is chosen from it."""
    capture = _session()
    capture.feed(event("close_window", 300, "QMenu", "menuOptions",
                       path=(("QMenuBar", "menubar"), ("QMainWindow", "MainWindow"))))
    assert _steps(capture) == []


def test_a_menu_of_the_applications_own_class_is_not_a_step_either():
    """The filter asks Qt what a class inherits; this is the fallback for one
    built before it learned to, and a QMenu subclass is overwhelmingly named
    for what it is."""
    capture = _session()
    capture.feed(event("close_window", 300, "TorrentContextMenu", "transferMenu"))
    assert _steps(capture) == []


def test_popups_closing_are_not_steps():
    capture = _session()
    for at, cls in enumerate(("QComboBoxPrivateContainer", "QTipLabel",
                              "QCompleter", "QCalendarPopup")):
        capture.feed(event("close_window", 300 + at, cls, f"popup{at}"))
    # The list inside a combo box's drop-down closes with it.
    capture.feed(event("close_window", 400, "QListView", "comboList",
                       path=(("QComboBoxPrivateContainer", ""),)))
    assert _steps(capture) == []


def test_a_phantom_close_is_not_a_gap_either():
    """Nobody closed anything, so there is nothing for anybody to repair."""
    capture = _session()
    capture.feed(event("close_window", 300, "QWidgetWindow", "menuOptionsWindow"))
    capture.feed(event("close_window", 301, "QMenu", "menuOptions"))
    assert capture.finish().drops == []


def test_a_dialog_closed_from_its_title_bar_is_still_recorded():
    """The case the close event exists for. A dialog nobody closed stays open,
    modal, over every step that follows."""
    capture = _session()
    capture.feed(event("close_window", 300, "TorrentCreatorDialog",
                       "TorrentCreatorDialog",
                       path=(("QMainWindow", "MainWindow"),)))
    steps = _steps(capture)
    assert [step.kind for step in steps] == [ActionKind.CLOSE_WINDOW]
    assert steps[0].target.definition["objectName"] == "TorrentCreatorDialog"


def test_a_real_close_and_its_wrapper_make_one_step_not_two():
    """The same dismissal arrives twice: once on the QWindow, once on the widget."""
    capture = _session()
    capture.feed(event("close_window", 300, "QWidgetWindow",
                       "TorrentCreatorDialogWindow"))
    capture.feed(event("close_window", 301, "TorrentCreatorDialog",
                       "TorrentCreatorDialog",
                       path=(("QMainWindow", "MainWindow"),)))
    assert [step.kind for step in _steps(capture)] == [ActionKind.CLOSE_WINDOW]


def test_a_qml_window_is_a_real_window():
    """In a QML application the QQuickWindow *is* the top level, with no widget
    behind it. Closing it is a step."""
    from qat_recorder.capture import is_dismissed_window

    assert is_dismissed_window(
        event("close_window", 300, "QQuickWindow", "mainWindow").target)


def test_the_filter_drops_phantom_closes_in_process():
    """Only the filter can ask what a class inherits from, so the real check
    lives there; the Python one is the fallback for older builds."""
    from qat_recorder.native import source_dir

    text = (source_dir() / "qatrec.cpp").read_text(encoding="utf-8")
    assert "isDismissedWindow" in text
    assert 'inherits("QWidgetWindow")' in text
    assert '"QMenu"' in text
    assert "type == QEvent::Close && !isDismissedWindow(object)" in text


def test_the_packaged_filter_is_the_one_in_the_repository():
    """The wheel carries its own copy of the filter source for `build-filter`
    on the VM. Editing one and not the other means the VM quietly builds the
    old filter -- which is exactly how a fix that works here does nothing
    there."""
    from pathlib import Path

    from qat_recorder.native import source_dir

    packaged = source_dir()
    repository = Path(__file__).resolve().parents[1] / "native"
    for name in ("qatrec.cpp", "CMakeLists.txt", "test_app.cpp",
                 "qatembedded.cpp"):
        assert (packaged / name).read_bytes() == (repository / name).read_bytes(), (
            f"qat_recorder/resources/native/{name} differs from native/{name}")


def test_a_click_in_a_qml_window_is_recorded_on_the_item_not_the_window():
    """QtQuick hands a window's input to its items with sendEvent, which clears
    spontaneous(): only the window got through, as VIS::QuickView_C '<unnamed>',
    and every step of a QML session was unrecordable. The press is held until
    delivery ends, so it names the item that took it -- the same one its release
    goes to -- and not the label on top that was offered it first.

    Checked live against a QML window clicked with QTest: press and release both
    on the Button, keys on the TextField."""
    from qat_recorder.native import source_dir

    text = (source_dir() / "qatrec.cpp").read_text(encoding="utf-8")
    assert 'object->inherits("QQuickWindow")' in text
    assert 'object->inherits("QQuickItem")' in text
    assert "QTimer::singleShot(0, flushQuickHeld)" in text


def test_a_qml_class_is_named_the_way_qat_names_it():
    """Qt calls the class it makes for RoundButton.qml RoundButton_QMLTYPE_111,
    numbered in load order, so it changes between runs -- and Qat never matches
    it: against Qat 1.8's server {"type": "RoundButton_QMLTYPE_0"} is missing and
    {"type": "RoundButton"} is found. Every such step failed on replay."""
    locator = Locator.from_dict({
        "class": "RoundButton_QMLTYPE_111", "objectName": "roundButton",
        "path": [{"class": "Panel_QML_3", "objectName": "openCase"},
                 {"class": "QQuickItem", "objectName": ""}]})
    assert locator.cls == "RoundButton"
    assert locator.path == (("Panel", "openCase"), ("QQuickItem", ""))


def test_the_filters_hello_does_not_start_the_clock():
    """It has no time, so the session's first step used to be dated from zero --
    'at 1790230298.31s'."""
    backend, _ = build_tree()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(RawEvent(kind="hello", t=0, target=Locator(), features=()))
    capture.feed_all(click_pair(1_700_000_000_000, "QPushButton", text="Import"))
    capture.feed_all(click_pair(1_700_000_002_000, "QPushButton", text="Export"))
    recording = capture.finish()
    times = [action.t for action in recording.actions]
    assert max(times) < 10, times
