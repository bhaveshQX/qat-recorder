# -*- coding: utf-8 -*-
"""Tests for gaps: events that could not be recorded, and filling them.

The recorder has always been able to say *that* something was lost and *why*.
It could not say *where*, and the operator finds out at the end of a session
they spent watching the application rather than the panel -- by which time the
end of the recording is the one place the missing step certainly does not go.

These tests pin the two halves of the answer: a drop carries the position it
happened at, and filling one puts a step exactly there.
"""

import json

import pytest

from qat_recorder.capture import CaptureSession
from qat_recorder.emit import emit_gherkin, emit_python, emit_steps
from qat_recorder.emit.python import DROP_MARKER, emit_object_map
from qat_recorder.ir import Action, ActionKind, Drop, Recording
from qat_recorder.player import Player
from qat_recorder.ui.controller import ControllerError, State
from tests.fixtures import build_tree
from tests.test_capture import click_pair
from tests.test_ui_controller import controller          # noqa: F401  (fixture)

#: Nothing in the application answers to this, which is the point.
NOWHERE = ("QNotARealClass", "nothingLikeThis")


def _session_with_a_gap():
    """One recorded click, one that could not be, then another recorded one."""
    backend, _ = build_tree()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed_all(click_pair(100, "QPushButton", "loginButton"))
    capture.feed_all(click_pair(200, *NOWHERE))
    capture.feed_all(click_pair(300, "QPushButton", "loginButton"))
    return capture, capture.finish()


def _marker_lines(script):
    return [line for line in script.splitlines() if DROP_MARKER in line]


def _body(script):
    return script[script.index("def test_recorded_session"):]


# --- a gap is not a step ---------------------------------------------------

def test_a_dropped_event_does_not_become_an_action():
    """The invariant the whole design rests on.

    An action is something the generated test does. A drop is the opposite --
    something the operator did that the test will not do -- and putting one in
    `actions` sends it to the player, the object map, the Gherkin and the
    override block, none of which have anything true to say about it.
    """
    _, recording = _session_with_a_gap()
    assert [action.kind for action in recording.actions] == [
        ActionKind.LAUNCH, ActionKind.CLICK, ActionKind.CLICK]
    assert len(recording.drops) == 1


def test_a_gap_knows_where_it_happened():
    _, recording = _session_with_a_gap()
    drop, = recording.drops
    # LAUNCH and the first click had been recorded when the event was lost.
    assert drop.after == 2
    assert drop.kind == "mouse_press"
    assert "nothingLikeThis" in drop.label


def test_a_press_and_its_release_are_one_gap():
    """One interaction the operator cannot see the result of is one question.

    A click arrives twice whether or not it can be recorded, and asking twice
    about the same button is how a session with four lost clicks looks like a
    session with eight.
    """
    _, recording = _session_with_a_gap()
    assert len(recording.drops) == 1


def test_the_count_of_lost_events_is_unchanged():
    """unresolved.txt still counts events, not questions.

    Folding the press and the release into one gap is a decision about what to
    ask the operator, not a claim that fewer events were lost.
    """
    capture, _ = _session_with_a_gap()
    assert capture.unresolved == 2
    assert len(capture.failures) == 2


# --- where it shows up -----------------------------------------------------

def test_the_script_shows_the_gap_between_the_steps_it_fell_between():
    _, recording = _session_with_a_gap()
    body = _body(emit_python(recording)).splitlines()
    steps = [index for index, line in enumerate(body) if "mouse_click" in line]
    marker = [index for index, line in enumerate(body) if DROP_MARKER in line]
    assert len(marker) == 1
    assert steps[0] < marker[0] < steps[1]


def test_the_marker_carries_enough_to_offer_a_fix():
    _, recording = _session_with_a_gap()
    line, = _marker_lines(emit_python(recording))
    payload = json.loads(line.split(DROP_MARKER, 1)[1])
    assert payload["index"] == 0            # which gap, for filling it
    assert payload["kind"] == "mouse_press"
    assert payload["seen"]["class"] == NOWHERE[0]
    assert payload["reason"]


def test_a_script_with_an_open_gap_says_so_at_the_end():
    """It passes without the step, so it has to say that out loud."""
    _, recording = _session_with_a_gap()
    script = emit_python(recording)
    assert "could not be recorded, and this script does not do them" in script


def test_a_gap_is_never_offered_as_a_locator():
    """The object could not be identified. That is why it is a gap.

    Writing it into the override block would put a definition nobody validated
    in front of the next person to read the test, which is the failure this
    project has spent its whole history removing.
    """
    _, recording = _session_with_a_gap()
    script = emit_python(recording)
    assert "QNOTAREALCLASS" not in script
    assert "nothingLikeThis" not in emit_object_map(recording)
    assert "nothingLikeThis" not in emit_gherkin(recording)
    assert "nothingLikeThis" not in emit_steps(recording)


def test_a_gap_does_not_stop_the_recording_replaying():
    """The player walks actions. A gap is not one, so there is nothing to hit."""
    played = []

    class FakeQat:
        def start_application(self, name):
            return "ctx"

        def mouse_click(self, target, **kwargs):
            played.append(target)

    _, recording = _session_with_a_gap()
    assert Player(FakeQat()).play(recording) == 3
    assert len(played) == 2


def test_gaps_survive_recording_json():
    _, recording = _session_with_a_gap()
    again = Recording.loads(recording.dumps())
    assert [(drop.after, drop.kind) for drop in again.drops] == [(2, "mouse_press")]


def test_a_recording_written_before_gaps_existed_still_loads():
    """No `drops` key at all, which is exactly right: nothing was looking."""
    old = json.loads(Recording(app="sample").dumps())
    old.pop("drops")
    assert Recording.from_dict(old).drops == []


# --- filling one -----------------------------------------------------------

def _fill(recording, index, code):
    return recording.repair(
        index, Action(ActionKind.CUSTOM_CODE, args={"code": code}))


def test_filling_a_gap_puts_the_step_where_the_event_was():
    _, recording = _session_with_a_gap()
    _fill(recording, 0, "qat.mouse_click({'text': 'Cancel'})")
    assert [action.kind for action in recording.actions] == [
        ActionKind.LAUNCH, ActionKind.CLICK, ActionKind.CUSTOM_CODE,
        ActionKind.CLICK]
    assert recording.validate() == []


def test_a_filled_gap_leaves_no_marker_and_no_warning():
    _, recording = _session_with_a_gap()
    _fill(recording, 0, "qat.mouse_click({'text': 'Cancel'})")
    script = emit_python(recording)
    assert _marker_lines(script) == []
    assert "could not be recorded" not in script
    assert "qat.mouse_click({'text': 'Cancel'})" in script


def test_the_gap_is_kept_after_it_is_filled():
    """The recording still says what was lost. It just no longer asks."""
    _, recording = _session_with_a_gap()
    _fill(recording, 0, "pass")
    assert len(recording.drops) == 1
    assert recording.drops[0].repaired is True
    assert recording.open_drops() == []


def test_filling_one_gap_moves_the_ones_after_it_along():
    """A drop's position is an index into `actions`, so inserting shifts it."""
    recording = Recording(app="sample")
    recording.add(Action(ActionKind.LAUNCH))
    recording.add_drop(Drop(reason="first", after=1))
    recording.add(Action(ActionKind.CLICK, target=None))
    recording.add_drop(Drop(reason="second", after=2))

    _fill(recording, 0, "pass")
    assert [drop.after for drop in recording.drops] == [1, 3]


def test_a_gap_cannot_be_filled_twice():
    _, recording = _session_with_a_gap()
    _fill(recording, 0, "pass")
    with pytest.raises(ValueError):
        _fill(recording, 0, "pass")


def test_a_step_that_carries_its_own_code_needs_no_target():
    """It is the call. There is nothing for a target to identify.

    Without this, one inserted step made the whole recording refuse to emit --
    so the button that inserted it could not produce a test.
    """
    recording = Recording(app="sample")
    recording.add(Action(ActionKind.LAUNCH))
    recording.add(Action(ActionKind.CUSTOM_CODE, args={"code": "pass"}))
    assert recording.validate() == []
    assert "    pass" in emit_python(recording)


# --- through the controller ------------------------------------------------

def _record_a_gap(controller):                              # noqa: F811
    controller.start()
    controller._test["receiver"].push(*click_pair(100, "QPushButton", "loginButton"))
    controller.poll()
    controller._test["receiver"].push(*click_pair(200, *NOWHERE))
    controller.poll()
    return controller.recording


def test_the_controller_reports_open_gaps(controller):      # noqa: F811
    _record_a_gap(controller)
    assert controller.summary()["gaps"] == 1


def test_repairing_through_the_controller_reaches_every_artifact(controller):  # noqa: F811
    """The reason a repair goes to the recording and not to the script pane.

    `save_as` regenerates all of them before it verifies, so a fix that lived
    only in the editor would be discarded by the act of keeping it.
    """
    _record_a_gap(controller)
    controller.repair_drop(0, "qat.mouse_click({'text': 'Cancel'})")
    controller.stop()

    files = controller.generated_files()
    assert "qat.mouse_click({'text': 'Cancel'})" in files["test_recorded.py"]
    assert DROP_MARKER not in files["test_recorded.py"]
    assert '"repaired": true' in files["recording.json"]
    assert controller.summary()["gaps"] == 0


def test_a_gap_can_be_filled_after_the_recording_has_stopped(controller):  # noqa: F811
    """Which is when it happens: the operator was watching the application."""
    _record_a_gap(controller)
    controller.stop()
    assert controller.state is State.STOPPED
    controller.repair_drop(0, "pass")
    assert controller.recording.open_drops() == []


def test_filling_a_gap_that_is_not_there_is_refused(controller):  # noqa: F811
    _record_a_gap(controller)
    with pytest.raises(ControllerError):
        controller.repair_drop(7, "pass")


def test_an_empty_repair_is_refused(controller):            # noqa: F811
    _record_a_gap(controller)
    with pytest.raises(ControllerError):
        controller.repair_drop(0, "   ")
