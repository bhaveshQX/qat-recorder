# -*- coding: utf-8 -*-
"""
Rows in lists, trees and tables, and the combo boxes built from them.

The third family where the widget receiving a click is not the thing clicked.
A row is a model index; the viewport gets the event; clicking the view on
replay clicks whatever row is in the middle that day.
"""

import pytest

from qat_recorder.backend import FakeNode
from qat_recorder.capture import CaptureSession
from qat_recorder.emit import emit_python
from qat_recorder.events import Locator
from qat_recorder.ir import ActionKind
from qat_recorder.items import combo_owner, is_combo, item_definition
from tests.fixtures import WIDGET, build_tree
from tests.test_capture import event


def _built(kind, t, row=0, column=0, text="", view="treeView", **extra):
    """A click delivered to a viewport, carrying the model index it hit."""
    from qat_recorder.events import RawEvent
    return RawEvent(
        kind=kind, t=t,
        target=Locator(
            cls="QWidget", object_name="qt_scrollarea_viewport",
            item_row=row, item_column=column, item_text=text,
            item_view=view, item_view_class="QTreeView",
            path=(("QTreeView", view), ("QWidget", "rootWidget"))),
        button=extra.get("button", 1), x=extra.get("x", 30),
        y=extra.get("y", 40))


@pytest.fixture()
def view_session():
    """A tree view with three rows, as Qat exposes them: container + row."""
    backend, nodes = build_tree()
    tree = nodes["root"].add(FakeNode(["QTreeView"] + WIDGET,
                                      {"objectName": "treeView"}))
    for index, text in enumerate(("alpha.txt", "beta.txt", "gamma.txt")):
        tree.add(FakeNode(WIDGET, {"row": index, "text": text}))
    return CaptureSession(backend, app_name="sample"), backend, nodes


def test_a_click_in_a_view_names_the_row(view_session):
    capture, _, _ = view_session
    capture.feed(_built("mouse_press", 1000, 1, 0, "beta.txt", "treeView"))
    capture.feed(_built("mouse_release", 1040, 1, 0, "beta.txt", "treeView"))
    recording = capture.finish()

    click = recording.actions[-1]
    assert click.kind is ActionKind.CLICK
    assert click.target.definition == {
        "container": {"objectName": "treeView"}, "row": 1}
    assert click.target.item_text == "beta.txt"


def test_the_release_is_not_a_second_click(view_session):
    capture, _, _ = view_session
    capture.feed(_built("mouse_press", 1000, 1, 0, "beta.txt"))
    capture.feed(_built("mouse_release", 1040, 1, 0, "beta.txt"))
    recording = capture.finish()

    assert len([a for a in recording.actions
                if a.kind is ActionKind.CLICK]) == 1


def test_a_row_qat_cannot_address_is_dropped_not_guessed(view_session):
    """Clicking the view instead would click a different row."""
    capture, _, _ = view_session
    capture.feed(_built("mouse_press", 1000, 99, 0, "nowhere"))
    recording = capture.finish()

    assert [a.kind for a in recording.actions] == [ActionKind.LAUNCH]
    assert "would click whichever row" in capture.failures[0]


def test_the_generated_step_finds_the_row_by_its_text(view_session):
    capture, _, _ = view_session
    capture.feed(_built("mouse_press", 1000, 1, 0, "beta.txt"))
    capture.feed(_built("mouse_release", 1040, 1, 0, "beta.txt"))

    source = emit_python(capture.finish())
    assert "def row(container, text" in source
    assert "click_row(TREEVIEW, 'beta.txt', recorded=1)" in source
    compile(source, "generated.py", "exec")


def test_the_row_helper_is_absent_when_no_view_was_touched():
    backend, _ = build_tree()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(event("mouse_press", 1000, "QPushButton", "loginButton",
                       button=1))
    capture.feed(event("mouse_release", 1040, "QPushButton", "loginButton",
                       button=1))
    assert "def row(container" not in emit_python(capture.finish())


# --- combo boxes ------------------------------------------------------------

@pytest.mark.parametrize("class_name, expected", [
    ("QComboBox", True), ("QFontComboBox", True), ("MyComboBox", True),
    ("QListView", False), ("QPushButton", False),
])
def test_combo_boxes_are_recognised(class_name, expected):
    assert is_combo(class_name) is expected


def test_the_owning_combo_is_found_through_the_popups_parents():
    locator = Locator(
        cls="QComboBoxListView", object_name="",
        path=(("QComboBoxPrivateContainer", ""),
              ("QComboBox", "themeSelector"),
              ("QDialog", "preferences")))
    assert combo_owner(locator) == "themeSelector"


def test_a_plain_view_has_no_owning_combo():
    locator = Locator(cls="QTreeView", object_name="treeView",
                      path=(("QWidget", "rootWidget"),))
    assert combo_owner(locator) is None


def _combo_click(t, text):
    from qat_recorder.events import RawEvent
    return RawEvent(
        kind="mouse_press", t=t,
        target=Locator(
            cls="QComboBoxListView", object_name="",
            item_row=2, item_text=text, item_view_class="QComboBoxListView",
            path=(("QComboBoxPrivateContainer", ""),
                  ("QComboBox", "envSelector"),
                  ("QWidget", "rootWidget"))),
        button=1)


def test_choosing_from_a_combo_records_the_value_not_the_clicks():
    """`{"type": "QComboBoxListView"}` does not exist unless the popup is open,
    which on a real replay is exactly what failed."""
    backend, _ = build_tree()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(_combo_click(1000, "Production"))
    recording = capture.finish()

    step = recording.actions[-1]
    assert step.kind is ActionKind.SELECT
    assert step.target.definition == {"objectName": "envSelector"}
    assert step.args == {"property": "currentText", "value": "Production"}


def test_the_click_that_opened_the_popup_is_removed():
    """Left in, replay opens the popup and leaves it over the next step."""
    backend, _ = build_tree()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(event("mouse_press", 900, "QComboBox", "envSelector", button=1))
    capture.feed(event("mouse_release", 940, "QComboBox", "envSelector", button=1))
    capture.feed(_combo_click(1000, "Production"))
    recording = capture.finish()

    kinds = [a.kind for a in recording.actions]
    assert kinds == [ActionKind.LAUNCH, ActionKind.SELECT]


def test_the_definition_helper_omits_a_zero_column():
    assert item_definition({"objectName": "v"}, 3) == {
        "container": {"objectName": "v"}, "row": 3}
    assert item_definition({"objectName": "v"}, 3, 2) == {
        "container": {"objectName": "v"}, "row": 3, "column": 2}
