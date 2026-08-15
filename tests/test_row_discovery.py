# -*- coding: utf-8 -*-
"""
Finding the clicked row without the event filter's help.

Which row was clicked is reported by the native filter. Making a *semantic*
decision depend on whether somebody re-ran `build-filter` was a design mistake:
when they had not, a click on the Preferences tab list became a click on the
middle of the tab list, the wrong tab opened, and the next step failed looking
for a widget on a page that was never shown --

    LookupError: Unable to find object: {"objectName":"spinMaxConnec", ...}

-- with a strong objectName, on a step that had nothing wrong with it.

Qat knows the rows itself: it wraps each item in a virtual widget with real
geometry, or it could not click one. So the rows can be walked and hit-tested.
Qat is the source of truth; the filter is only an accelerator.
"""

import pytest

from qat_recorder.backend import FakeBackend, FakeNode
from qat_recorder.capture import CaptureSession
from qat_recorder.emit import emit_python
from qat_recorder.events import Locator, RawEvent
from qat_recorder.ir import ActionKind
from qat_recorder.items import bounds_of, contains, discover_row, is_view
from tests.fixtures import WIDGET, build_tree

ROWS = ("Behaviour", "Downloads", "Connection", "Speed", "BitTorrent")
ROW_HEIGHT = 20


@pytest.fixture()
def tabs():
    """A tab list as Qat exposes one: rows addressed by {container, row}."""
    backend, nodes = build_tree()
    view = nodes["root"].add(FakeNode(["QListView"] + WIDGET,
                                      {"objectName": "tabSelection"}))
    for index, text in enumerate(ROWS):
        view.add(FakeNode(WIDGET, {
            "row": index, "text": text,
            "x": 0, "y": index * ROW_HEIGHT, "width": 120, "height": ROW_HEIGHT,
        }))
    return backend, nodes


def viewport_click(kind, t, x, y, view="tabSelection", view_class="QListView"):
    """A click through a viewport, as an OLD filter reports it: no row data."""
    return RawEvent(
        kind=kind, t=t, button=1, x=x, y=y,
        target=Locator(cls="QWidget", object_name="qt_scrollarea_viewport",
                       path=((view_class, view), ("QWidget", "rootWidget"))))


# --- the geometry helpers ---------------------------------------------------

@pytest.mark.parametrize("properties, expected", [
    ({"x": 1, "y": 2, "width": 3, "height": 4}, (1.0, 2.0, 3.0, 4.0)),
    ({"left": 1, "top": 2, "width": 3, "height": 4}, (1.0, 2.0, 3.0, 4.0)),
    ({"bounds": {"x": 5, "y": 6, "width": 7, "height": 8}}, (5.0, 6.0, 7.0, 8.0)),
    ({"geometry": {"x": 5, "y": 6, "width": 7, "height": 8}}, (5.0, 6.0, 7.0, 8.0)),
    ({"x": 1, "y": 2, "width": 0, "height": 4}, None),      # nothing to hit
    ({"text": "no geometry here"}, None),
    ({"x": True, "y": 2, "width": 3, "height": 4}, None),   # bools are not sizes
])
def test_geometry_is_read_however_qat_spells_it(properties, expected):
    assert bounds_of(properties) == expected


def test_a_point_belongs_to_exactly_one_row():
    assert contains((0, 20, 120, 20), 5, 20) is True
    assert contains((0, 20, 120, 20), 5, 39) is True
    assert contains((0, 20, 120, 20), 5, 40) is False       # the next row's
    assert contains((0, 20, 120, 20), 200, 25) is False


@pytest.mark.parametrize("class_name, expected", [
    ("QListView", True), ("QTreeView", True), ("TransferListWidget", True),
    ("QComboBoxListView", True), ("QPushButton", False), ("", False),
])
def test_views_are_recognised_by_shape_not_by_a_list(class_name, expected):
    assert is_view(class_name) is expected


# --- asking Qat -------------------------------------------------------------

def test_the_row_under_the_point_is_found(tabs):
    backend, _ = tabs
    assert discover_row(backend, {"objectName": "tabSelection"}, 10, 45) == \
        (2, "Connection")


def test_a_point_past_the_last_row_finds_nothing(tabs):
    backend, _ = tabs
    assert discover_row(backend, {"objectName": "tabSelection"}, 10, 999) is None


def test_a_view_that_exposes_no_geometry_is_not_guessed_at():
    """Better no step than a step that clicks the wrong row."""
    view = FakeNode(["QListView"], {"objectName": "opaque"})
    for index in range(3):
        view.add(FakeNode(["QWidget"], {"row": index, "text": f"row {index}"}))
    backend = FakeBackend([view])
    assert discover_row(backend, {"objectName": "opaque"}, 5, 5) is None


# --- through a session ------------------------------------------------------

def test_a_click_on_a_tab_names_the_tab_without_the_filter(tabs):
    """The exact case that broke test9, with a filter that reports no rows."""
    backend, _ = tabs
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(viewport_click("mouse_press", 1000, 10, 45))
    capture.feed(viewport_click("mouse_release", 1040, 10, 45))
    recording = capture.finish()

    click = recording.actions[-1]
    assert click.kind is ActionKind.CLICK
    assert click.target.definition == {
        "container": {"objectName": "tabSelection"}, "row": 2}
    assert click.target.item_text == "Connection"


def test_the_generated_step_says_which_tab(tabs):
    backend, _ = tabs
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(viewport_click("mouse_press", 1000, 10, 45))
    capture.feed(viewport_click("mouse_release", 1040, 10, 45))

    source = emit_python(capture.finish())
    assert "qat.mouse_click(row(TABSELECTION, 'Connection', recorded=2))" in source
    assert "clicks the widget, not the item" not in source
    compile(source, "generated.py", "exec")


def test_a_click_on_something_that_is_not_a_view_is_untouched(tabs):
    backend, _ = tabs
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(RawEvent(
        kind="mouse_press", t=1000, button=1,
        target=Locator(cls="QPushButton", object_name="loginButton")))
    capture.feed(RawEvent(
        kind="mouse_release", t=1040, button=1,
        target=Locator(cls="QPushButton", object_name="loginButton")))
    recording = capture.finish()

    assert recording.actions[-1].target.definition == {
        "objectName": "loginButton"}
