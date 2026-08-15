# -*- coding: utf-8 -*-
"""
Reading the label a person actually clicked, and being honest when there is none.

Two failures in one recording:

* the generated test called `preconditions()`, which had not been written --
  a NameError before a single step ran, so nothing opened at all;
* the steps were `{'container': ..., 'row': 3}` with no text, described by a
  warning that said they would be "found by its text on replay". There was no
  text. The warning lied, and the step was purely positional.
"""

import pytest

from qat_recorder.backend import FakeNode
from qat_recorder.capture import CaptureSession
from qat_recorder.emit import emit_python
from qat_recorder.events import Locator, RawEvent
from qat_recorder.ir import ActionKind, Robustness
from qat_recorder.items import DISPLAY_ROLE, item_text_of
from tests.fixtures import WIDGET, build_tree


def sidebar(row_props):
    backend, nodes = build_tree()
    view = nodes["root"].add(FakeNode(["QListWidget"] + WIDGET, {
        "objectName": "tabSelection", "currentRow": 0}))
    for index, props in enumerate(row_props):
        view.add(FakeNode(WIDGET, {"row": index, **props}))
    return backend, nodes, view


def click(t, kind="mouse_press"):
    return RawEvent(
        kind=kind, t=t, button=1, x=10, y=45,
        target=Locator(cls="QWidget", object_name="qt_scrollarea_viewport",
                       path=(("QListWidget", "tabSelection"),
                             ("QWidget", "rootWidget"))))


def record(backend, view, row):
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(click(1000))
    view.props["currentRow"] = row
    capture.feed(click(1040, kind="mouse_release"))
    return capture.finish()


# --- finding the label ------------------------------------------------------

@pytest.mark.parametrize("props, expected", [
    ({"text": "Connection"}, "Connection"),
    ({"displayText": "Connection"}, "Connection"),
    ({"title": "Connection"}, "Connection"),
    ({"accessibleName": "Connection"}, "Connection"),
    ({"text": "  Connection  "}, "Connection"),
    ({"text": ""}, ""),
    ({}, ""),
])
def test_a_label_is_read_however_the_view_spells_it(props, expected):
    backend, _, _ = sidebar([props])
    node = backend.find_all({"container": {"objectName": "tabSelection"},
                             "row": 0})[0]
    assert item_text_of(backend, node) == expected


def test_the_model_is_asked_when_no_property_has_it():
    """Qt::DisplayRole is where an item's text really lives, and Qat can call
    for it even where nothing exposes it as a property."""
    backend, _, _ = sidebar([{"data": {DISPLAY_ROLE: "Connection"}}])
    node = backend.find_all({"container": {"objectName": "tabSelection"},
                             "row": 0})[0]
    assert item_text_of(backend, node) == "Connection"


# --- the honest grade -------------------------------------------------------

def test_a_row_with_a_label_is_clicked_by_that_label():
    backend, _, view = sidebar([{"text": "Behaviour"}, {"text": "Downloads"},
                                {"text": "Connection"}])
    step = record(backend, view, 2).actions[-1]

    assert step.kind is ActionKind.CLICK
    assert step.target.item_text == "Connection"
    assert step.target.robustness is Robustness.MODERATE


def test_a_row_with_no_label_admits_it_is_only_a_position():
    """It used to claim it would be found by its text. There was no text."""
    backend, _, view = sidebar([{}, {}, {}, {}])
    step = record(backend, view, 3).actions[-1]

    assert step.target.item_text == ""
    assert step.target.robustness is Robustness.FRAGILE
    assert "reordered" in step.target.warnings[0]
    assert step.target.strategy == "item+index"


def test_such_a_step_is_flagged_in_the_generated_script():
    backend, _, view = sidebar([{}, {}, {}, {}])
    source = emit_python(record(backend, view, 3))

    assert "# fragile:" in source
    assert "Review before relying on this script:" in source
    compile(source, "generated.py", "exec")


# --- the NameError ----------------------------------------------------------

def test_a_recording_whose_rows_have_no_text_still_runs():
    """The call was emitted whenever there were rows; the function only when a
    row had text. Rows without text produced a NameError before step one."""
    backend, _, view = sidebar([{}, {}, {}, {}])
    source = emit_python(record(backend, view, 3))

    assert "preconditions()" not in source
    compile(source, "generated.py", "exec")


def test_a_recording_whose_rows_have_text_checks_them_first():
    backend, _, view = sidebar([{"text": "Behaviour"}, {"text": "Downloads"},
                                {"text": "Connection"}])
    source = emit_python(record(backend, view, 2))

    assert "def preconditions():" in source
    body = source.split("def test_recorded_session(application):")[1]
    assert body.strip().startswith("preconditions()")
    compile(source, "generated.py", "exec")


def test_the_call_and_the_function_are_never_separated():
    """Whatever the recording, one is present exactly when the other is."""
    for rows, chosen in (([{}], 0),
                         ([{"text": "A"}], 0),
                         ([{}, {"text": "B"}], 1),
                         ([{"data": {DISPLAY_ROLE: "C"}}], 0)):
        backend, _, view = sidebar(rows)
        source = emit_python(record(backend, view, chosen))
        assert ("def preconditions():" in source) == \
            ("    preconditions()" in source), source
        compile(source, "generated.py", "exec")
