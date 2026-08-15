# -*- coding: utf-8 -*-
"""
No step may depend on geometry.

Not a coordinate, not a scroll of so many degrees, not a drag of so many
pixels. Every one of those replays correctly exactly once -- on the machine,
window size and content it was recorded against -- and then quietly does
something else.

Where a gesture changed a value, the value is what gets recorded: the same
trick that records typing. "Set this to 40" survives a different window; "turn
the wheel three notches here" does not.
"""

import ast

import pytest

from qat_recorder.backend import FakeNode
from qat_recorder.capture import CaptureSession, value_property
from qat_recorder.emit import emit_python
from qat_recorder.ir import ActionKind
from tests.fixtures import WIDGET, build_tree
from tests.test_capture import click_pair, event

#: Keyword arguments that carry geometry into a Qat call.
GEOMETRIC = {"x", "y", "dx", "dy", "x_degrees", "y_degrees"}


def sliders():
    """A tree with the things a wheel can act on: a list, and a value widget."""
    backend, nodes = build_tree()
    nodes["root"].add(FakeNode(["QListWidget", "QAbstractItemView"] + WIDGET,
                               {"objectName": "torrentList"}))
    nodes["root"].add(FakeNode(["QSlider", "QAbstractSlider"] + WIDGET,
                               {"objectName": "speedLimit", "value": 40}))
    nodes["root"].add(FakeNode(["QSpinBox"] + WIDGET,
                               {"objectName": "portBox", "value": 8080}))
    return backend, nodes


@pytest.mark.parametrize("class_name, expected", [
    ("QSpinBox", "value"),
    ("QSlider", "value"),
    ("QComboBox", "currentText"),
    ("QDoubleSpinBox", "value"),
    ("MyAppSpinBox", "value"),
    ("QListWidget", ""),
    ("QPushButton", ""),
    ("", ""),
])
def test_only_some_widgets_have_a_value_a_wheel_changes(class_name, expected):
    assert value_property(class_name) == expected


def test_scrolling_a_list_is_not_a_step():
    backend, _ = sliders()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(event("wheel", 1000, "QListWidget", "torrentList", dy=-120))
    recording = capture.finish()

    assert [a.kind for a in recording.actions] == [ActionKind.LAUNCH]
    assert "navigation, not a step" in capture.failures[0]


def test_scrolling_a_spin_box_records_the_value_it_reached():
    """The wheel changed a number. The number is the step."""
    backend, nodes = sliders()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(event("wheel", 1000, "QSpinBox", "portBox", dy=-120))
    capture.feed(event("wheel", 1040, "QSpinBox", "portBox", dy=-120))
    capture.feed(event("wheel", 1080, "QSpinBox", "portBox", dy=-120))
    # Three notches later the widget holds this:
    nodes["root"].children_list[-1].props["value"] = 8077
    recording = capture.finish()

    step = recording.actions[-1]
    assert step.kind is ActionKind.SELECT
    assert step.args == {"property": "value", "value": 8077}
    assert step.target.definition == {"objectName": "portBox"}


def test_a_burst_of_wheel_events_is_one_step():
    backend, _ = sliders()
    capture = CaptureSession(backend, app_name="sample")
    for when in range(1000, 1200, 20):
        capture.feed(event("wheel", when, "QSpinBox", "portBox", dy=-120))
    recording = capture.finish()

    assert len([a for a in recording.actions
                if a.kind is ActionKind.SELECT]) == 1


def test_dragging_a_slider_records_where_it_ended_up():
    backend, nodes = sliders()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(event("mouse_press", 1000, "QSlider", "speedLimit",
                       button=1, x=10, y=5))
    nodes["root"].children_list[-2].props["value"] = 75
    capture.feed(event("mouse_release", 1400, "QLabel", "statusLabel",
                       button=1, x=90, y=5))
    recording = capture.finish()

    step = recording.actions[-1]
    assert step.kind is ActionKind.SELECT
    assert step.args == {"property": "value", "value": 75}


def test_dragging_something_with_no_value_is_dropped():
    backend, _ = sliders()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(event("mouse_press", 1000, "QPushButton", "loginButton",
                       button=1, x=10, y=10))
    capture.feed(event("mouse_release", 5000, "QLabel", "statusLabel",
                       button=1, x=60, y=90))
    recording = capture.finish()

    assert ActionKind.DRAG not in [a.kind for a in recording.actions]
    assert "no durable meaning" in capture.failures[0]


def test_a_value_step_sets_the_value_through_the_object():
    """Assigning to the definition dict would set an attribute on a dictionary
    and change nothing in the application."""
    backend, nodes = sliders()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(event("wheel", 1000, "QSpinBox", "portBox", dy=-120))
    nodes["root"].children_list[-1].props["value"] = 8077

    source = emit_python(capture.finish())
    assert "qat.wait_for_object(find(PORTBOX)).value = 8077" in source
    compile(source, "generated.py", "exec")


# --- the standing rule ------------------------------------------------------

def test_a_recorded_session_generates_no_geometry_at_all():
    """The whole point, asserted over a session that touches everything."""
    backend, nodes = sliders()
    capture = CaptureSession(backend, app_name="sample")

    capture.feed_all(click_pair(1000, "QPushButton", "loginButton", x=17, y=9))
    capture.feed(event("key_press", 1500, "QLineEdit", "usernameField",
                       key=ord("a")))
    capture.feed(event("wheel", 2000, "QListWidget", "torrentList", dy=-120))
    capture.feed(event("wheel", 2500, "QSpinBox", "portBox", dy=-120))
    nodes["root"].children_list[-1].props["value"] = 8079
    capture.feed(event("mouse_press", 3000, "QMenuBar", "menubar",
                       button=1, x=42, y=11, menu_item="&Options"))
    capture.feed(event("mouse_release", 3040, "QMenuBar", "menubar",
                       button=1, x=42, y=11, menu_item="&Options"))

    source = emit_python(capture.finish())
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg in GEOMETRIC:
                offenders.append(ast.unparse(node))
        # Positional coordinates: qat.mouse_click(TARGET, 42, 11)
        if (isinstance(node.func, ast.Attribute)
                and node.func.attr in ("mouse_click", "double_click")
                and len(node.args) > 1):
            offenders.append(ast.unparse(node))

    assert not offenders, "geometry in generated code:\n" + "\n".join(offenders)
