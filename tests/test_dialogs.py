# -*- coding: utf-8 -*-
"""
Native dialogs, and combo boxes that do not depend on the C++ build.

Both come from the same real session: a file picker that froze the recording,
and a combo popup recorded as `{"type": "QComboBoxListView"}` because the
installed filter predated item support.
"""

from pathlib import Path

import pytest

from qat_recorder.capture import CaptureSession, NOT_ANSWERING, explain
from qat_recorder.emit import emit_python
from qat_recorder.events import Locator, RawEvent
from qat_recorder.ir import ActionKind
from tests.fixtures import build_tree
from tests.test_capture import event

WRAPPER = Path(__file__).resolve().parents[1] / "qat_recorder" / "resources" / "wrapper.sh"


# --- native dialogs ---------------------------------------------------------

def test_the_launcher_asks_qt_for_its_own_dialogs():
    text = WRAPPER.read_text(encoding="utf-8")
    assert 'export QT_QPA_PLATFORMTHEME=""' in text
    # Opt-out, because it changes how the dialog looks.
    assert 'QATREC_NATIVE_DIALOGS' in text


def test_replay_launches_under_the_same_condition():
    """Otherwise the recording is made against one dialog and replayed against
    another."""
    backend, _ = build_tree()
    capture = CaptureSession(backend, app_name="sample", app_path="/bin/sample")
    capture.feed(event("mouse_press", 1000, "QPushButton", "loginButton",
                       button=1))
    capture.feed(event("mouse_release", 1040, "QPushButton", "loginButton",
                       button=1))

    source = emit_python(capture.finish())
    assert "os.environ['QT_QPA_PLATFORMTHEME'] = ''" in source
    assert "QATREC_NATIVE_DIALOGS" in source
    compile(source, "generated.py", "exec")


@pytest.mark.parametrize("message", [
    "Error sending command - trying to reconnect: timed out",
    "Cannot send command: application is not running",
    "Cannot send command: application has been disconnected",
])
def test_a_frozen_application_is_explained_not_just_reported(message):
    explained = explain(RuntimeError(message))
    assert "native dialog" in explained
    assert message in explained          # the original is kept, not swallowed


def test_an_ordinary_error_is_left_alone():
    assert explain(ValueError("no such object")) == "no such object"


def test_a_frozen_recording_says_why(monkeypatch):
    """The whole point: 20 dropped events with a reason beats 20 without."""
    backend, _ = build_tree()
    capture = CaptureSession(backend, app_name="sample")

    def frozen(_definition):
        raise RuntimeError("Error sending command - trying to reconnect: timed out")

    monkeypatch.setattr(backend, "find_all", frozen)
    # Two, because a group is only folded once the next event proves it is
    # complete -- the second press is what flushes the first.
    capture.feed_all([
        event("mouse_press", 1000, "QPushButton", "loginButton", button=1),
        event("mouse_press", 3000, "QPushButton", "loginButton", button=1),
    ])

    assert capture.unresolved >= 1
    assert "native dialog" in capture.failures[0]


# --- combo boxes without the new filter -------------------------------------

def combo_click(t, **extra):
    """A click in a combo popup as an OLD filter reports it: no item data."""
    return RawEvent(
        kind=extra.pop("kind", "mouse_press"), t=t,
        target=Locator(
            cls="QComboBoxListView", object_name="",
            path=(("QComboBoxPrivateContainer", ""),
                  ("QComboBox", "envSelector"),
                  ("QWidget", "rootWidget"))),
        button=1)


def test_a_combo_choice_is_recorded_even_without_item_data():
    """`{"type": "QComboBoxListView"}` does not exist unless the popup is open.

    The row that was clicked comes from the event filter, and a filter built
    before that support existed does not send it -- so the value is read from
    the combo box afterwards instead. The case that kept breaking replays does
    not get to depend on which build of the filter is installed.
    """
    backend, nodes = build_tree()
    capture = CaptureSession(backend, app_name="sample")

    capture.feed(combo_click(1000))
    capture.feed(combo_click(1040, kind="mouse_release"))
    nodes["env"].props["currentText"] = "Production"
    capture.feed_all([event("mouse_press", 3000, "QPushButton", "loginButton",
                            button=1),
                      event("mouse_release", 3040, "QPushButton", "loginButton",
                            button=1)])
    recording = capture.finish()

    select = [a for a in recording.actions if a.kind is ActionKind.SELECT]
    assert len(select) == 1
    assert select[0].target.definition == {"objectName": "envSelector"}
    assert select[0].args == {"property": "currentText", "value": "Production"}


def test_no_step_ever_targets_a_combo_popup():
    backend, nodes = build_tree()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(combo_click(1000))
    capture.feed(combo_click(1040, kind="mouse_release"))
    nodes["env"].props["currentText"] = "Production"

    source = emit_python(capture.finish())
    assert "QComboBoxListView" not in source
    assert "qat.wait_for_object(ENVSELECTOR).currentText = 'Production'" in source
    compile(source, "generated.py", "exec")
