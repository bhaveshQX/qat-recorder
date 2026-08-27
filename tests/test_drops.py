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

from qat_recorder.capture import CaptureSession, dropped_report
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


# --- filling one by pointing at it -----------------------------------------

def _point_at(controller, cls, object_name):                # noqa: F811
    """What the operator does: click the control in the application."""
    from tests.test_capture import event
    controller._test["receiver"].push(
        event("mouse_press", 400, cls, object_name, button=1),
        event("mouse_release", 440, cls, object_name, button=1))
    controller.poll()


def test_pointing_at_a_control_fills_the_gap_with_a_real_step(controller):  # noqa: F811
    """The strongest fix, and the one that needs no code.

    The pick goes through the same resolver a recorded step goes through, so
    what lands in the gap carries locators that were checked against the running
    application -- not a definition assembled from what the filter reported
    about an object it could not find.
    """
    _record_a_gap(controller)
    controller.arm_repair(0)
    assert controller.state is State.PAUSED, (
        "arming pauses, so walking back to the control records nothing")
    controller.pick_now()
    assert controller.state is State.PICKING

    _point_at(controller, "QPushButton", "loginButton")

    # Still paused: the operator navigated to get here, and resuming from this
    # screen would record the next steps from the wrong place.
    assert controller.state is State.PAUSED
    filled = controller.recording.actions[-1]
    assert filled.kind is ActionKind.CLICK
    assert filled.target.definition["objectName"] == "loginButton"
    assert filled.target.robustness.rank <= 1          # as good as a recorded step
    assert controller.recording.open_drops() == []


def test_the_pointing_click_is_not_recorded_as_a_step_of_its_own(controller):  # noqa: F811
    _record_a_gap(controller)
    before = len(controller.recording.actions)
    controller.arm_repair(0)
    controller.pick_now()
    _point_at(controller, "QPushButton", "loginButton")
    # Exactly one action added: the repair. Not the click that chose it.
    assert len(controller.recording.actions) == before + 1


def test_the_step_matches_the_event_that_was_lost(controller):  # noqa: F811
    """A lost double-click asks for a double-click, not a click."""
    from tests.test_capture import event
    controller.start()
    controller._test["receiver"].push(
        event("mouse_double", 200, *NOWHERE, button=1))
    controller.poll()
    assert len(controller.recording.drops) == 1

    controller.arm_repair(0)
    controller.pick_now()
    _point_at(controller, "QPushButton", "loginButton")
    assert controller.recording.actions[-1].kind is ActionKind.DOUBLE_CLICK


def test_pointing_needs_the_application_to_be_running(controller):  # noqa: F811
    _record_a_gap(controller)
    controller.stop()
    with pytest.raises(ControllerError):
        controller.arm_repair(0)


def test_pointing_at_a_gap_that_is_not_there_is_refused(controller):  # noqa: F811
    _record_a_gap(controller)
    with pytest.raises(ControllerError):
        controller.arm_repair(7)


def test_a_checkpoint_pick_is_still_a_checkpoint(controller):  # noqa: F811
    """Arming a repair must not leave the next checkpoint filling gaps."""
    _record_a_gap(controller)
    controller.arm_repair(0)
    controller.cancel_repair()
    controller.resume()                    # arming paused; come back to it

    controller.arm_checkpoint()
    _point_at(controller, "QPushButton", "loginButton")
    assert controller._test["picked"], "the checkpoint modal was never offered"
    assert controller.recording.open_drops() != []


# --- a fix that was never checked is not a fix -----------------------------

def test_a_gap_on_an_ambiguous_object_offers_no_one_click_fix():
    """The failure this whole check exists to prevent.

    Six check boxes in a preferences dialog, none of them named, all of them
    QCheckBox. A definition built from what the filter reported looks like a
    locator and identifies all six, so the generated script died on its first
    run with "Multiple objects found that match this definition". The recorder
    asks the application at the moment of the drop, and declines to offer a fix
    it cannot stand behind.
    """
    from tests.test_capture import event

    backend, _ = build_tree()
    capture = CaptureSession(backend, app_name="sample")
    # Two identical unnamed "Apply" buttons -- the same shape as the check boxes.
    capture.feed_all(click_pair(200, "QPushButton", text="Apply"))
    recording = capture.finish()

    drop, = recording.drops
    assert drop.matched == 2
    assert drop.suggestion == {}, (
        "the resolver is asked first and can address a sibling by index, but "
        "only when the filter says which sibling it was. Here it did not, so "
        "there is nothing to offer and the count is what the panel shows.")


def test_a_gap_on_one_findable_object_carries_a_checked_fix():
    """The other half: where it is unambiguous, the fix is real.

    Resolved through the resolver, so it carries every way of addressing the
    object -- not the single property the filter happened to report.
    """
    from tests.test_capture import event

    backend, nodes = build_tree()
    capture = CaptureSession(backend, app_name="sample")
    # Nothing was dropped here, so make the same check directly.
    matched, suggestion = capture._check_now(
        event("mouse_press", 200, "QPushButton", "loginButton"),
        {"class": "QPushButton", "objectName": "loginButton"})
    assert matched == 1
    assert suggestion["definition"]["objectName"] == "loginButton"
    assert suggestion["robustness"] != "unresolved"
    assert suggestion["alternatives"], "the checked fix carries the runners-up too"


def test_filling_from_an_unchecked_gap_is_refused(controller):   # noqa: F811
    """It has to fail here, in the panel, and not later in a test run."""
    from tests.test_capture import event

    controller.start()
    controller._test["receiver"].push(*click_pair(200, "QPushButton", text="Apply"))
    controller.poll()
    assert controller.recording.drops[0].matched == 2

    with pytest.raises(ControllerError) as raised:
        controller.repair_suggestion(0)
    assert "matched 2" in str(raised.value)
    assert controller.recording.open_drops(), "the gap is still open"


def test_filling_from_a_checked_gap_inserts_a_real_step(controller):  # noqa: F811
    _record_a_gap(controller)
    drop = controller.recording.drops[0]
    # Stand in for the case where the reported definition did identify one
    # object when the event was lost.
    backend = controller.session.backend
    drop.matched = 1
    drop.suggestion = controller.session.resolver.resolve(
        backend.find_all({"objectName": "loginButton"})[0]).to_dict()

    controller.repair_suggestion(0)
    filled = controller.recording.actions[-1]
    assert filled.kind is ActionKind.CLICK
    assert filled.target.definition["objectName"] == "loginButton"
    assert controller.recording.open_drops() == []


def test_the_resolver_is_asked_before_the_reported_properties():
    """Fourteen unnamed check boxes have no distinguishing property and are
    still addressable as "the third one inside this group".

    The resolver's last rung is a positional index. Asking only what the
    filter's properties matched never reached it, so a whole dialog's worth of
    gaps offered no fix at all.
    """
    from tests.test_capture import event

    backend, nodes = build_tree()
    capture = CaptureSession(backend, app_name="sample")
    # The filter says which of the two identical buttons was hit.
    matched, suggestion = capture._check_now(
        event("mouse_press", 200, "QPushButton", text="Apply", index=1,
              path=(("QGroupBox", "duplicateGroup"),)),
        {"class": "QPushButton", "text": "Apply"})

    assert suggestion, "the resolver can place this and was not asked"
    assert suggestion["robustness"] == "fragile", "and it says what that is worth"
    assert suggestion["index"] == 1


# --- what is a gap, and what is merely not a step --------------------------

def _feed(events, tree=None):
    backend, _ = tree if tree else build_tree()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed_all(events)
    return capture, capture.finish()


def test_scrolling_is_not_a_gap():
    """The bug that made the panel unusable.

    A wheel event fires every few milliseconds while a list is scrolled, and
    every one is dropped on purpose -- there is no step to record and nothing
    for anyone to fix. Each became a gap: an interruption in the script asking
    the operator to repair a decision the recorder got right, a screenshot, and
    a lookup against the running application, several times a second.
    """
    from tests.test_capture import event

    from tests.test_no_geometry import sliders

    capture, recording = _feed(
        [event("wheel", 1000 + n * 40, "QListWidget", "torrentList", dy=-120)
         for n in range(25)],
        tree=sliders())

    assert recording.drops == [], "scrolling asked 25 questions nobody can answer"
    assert capture.unresolved == 25, "and it is still counted and explained"
    assert "scrolling is navigation" in dropped_report(capture.failures)


def test_a_drag_with_nothing_to_record_is_not_a_gap():
    from tests.test_capture import event

    capture, recording = _feed([
        event("mouse_release", 1000, "QWidget", "canvas", button=1),
    ])
    assert [drop.reason for drop in recording.drops] == [] or all(
        "drag" not in drop.reason for drop in recording.drops)


def test_a_click_on_a_scrollbar_is_not_a_gap():
    capture, recording = _feed(click_pair(1000, "QScrollBar", "qt_scrollarea_vcontainer"))
    assert recording.drops == []
    assert capture.unresolved > 0


def test_an_event_that_should_have_been_a_step_is_still_a_gap():
    """The other half: the distinction has to cut, not just exclude."""
    _, recording = _feed(click_pair(200, *NOWHERE))
    assert len(recording.drops) == 1


def test_checking_a_gap_does_not_disturb_the_next_one():
    """_resolve records why it failed in a field the folder reads afterwards.

    Asking it again, after the fact, to see whether a fix could be offered
    overwrote that -- so the next event's reason could be borrowed from an
    enquiry about the previous one.
    """
    from tests.test_capture import event

    backend, _ = build_tree()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed_all(click_pair(100, *NOWHERE))
    first = capture.failures[0]
    capture.feed_all(click_pair(400, "QNotEither", "alsoNothing"))
    assert capture.failures[0] == first
    assert "alsoNothing" in capture.failures[-1]


def test_two_clicks_on_the_same_control_are_two_gaps():
    """Folding is about a press and its release, not about timing.

    Merging anything that landed within half a second of the last gap swallowed
    genuine repeat clicks -- three attempts at the same unnameable control
    became one, and the operator was never asked about the other two.
    """
    from tests.test_capture import event

    backend, _ = build_tree()
    capture = CaptureSession(backend, app_name="sample")
    at = 1000
    for _ in range(3):
        capture.feed_all(click_pair(at, *NOWHERE))
        at += 300                          # a person clicking three times
    recording = capture.finish()

    assert len(recording.drops) == 3
    assert all(drop.kind == "mouse_press" for drop in recording.drops), (
        "each gap is the press; its release is folded into it")


def test_a_realistic_session_asks_only_about_what_matters():
    """Eight steps, a minute of scrolling, three unnameable clicks.

    Before drops were classified this produced sixty-six gaps, sixty-six
    interruptions in the generated script and a screenshot for each.
    """
    from tests.test_capture import event
    from tests.test_no_geometry import sliders

    backend, _ = sliders()
    capture = CaptureSession(backend, app_name="sample")
    at = 1000
    for _ in range(8):
        capture.feed_all(click_pair(at, "QPushButton", "loginButton")); at += 200
    for _ in range(60):
        capture.feed(event("wheel", at, "QListWidget", "torrentList", dy=-120))
        at += 30
    for _ in range(3):
        capture.feed_all(click_pair(at, *NOWHERE)); at += 300
    recording = capture.finish()

    assert capture.unresolved == 66, "everything lost is still counted"
    assert len(recording.drops) == 3, "but only three of them are questions"
    assert emit_python(recording).count(DROP_MARKER) == 3


# --- the evidence a repair is chosen from ----------------------------------

def test_a_gap_records_what_the_application_knew():
    """The prerequisite for any repair at all, human or otherwise.

    Asked for rather than taken while folding: enumerating every object of the
    class and resolving each one cost a hundred and seventy-nine round trips on
    a tree of nine widgets, and the poll loop holds the session lock.
    """
    capture, recording = _feed(click_pair(200, "QPushButton", text="Apply"))
    drop = recording.drops[0]
    assert drop.evidence == {}, "gathered in the poll loop after all"
    pack = capture.evidence_for(drop.locator_or_none(), drop.reason, drop.kind)

    assert pack["class"] == "QPushButton"
    assert pack["reason"]
    assert len(pack["candidates"]) >= 4, "the other objects of that class"

    for candidate in pack["candidates"]:
        assert "id" in candidate and "properties" in candidate
        # Every candidate that can be addressed carries the Target the resolver
        # produced against the running application -- not a definition for
        # anybody to assemble later.
        if candidate["robustness"] != "unresolved":
            assert candidate["target"]["definition"]


def test_choosing_a_candidate_inserts_the_target_the_recorder_resolved(controller):  # noqa: F811
    """The contract that keeps a chooser honest, whoever it is.

    A person clicking a candidate and a model answering with an id arrive at the
    same place, and what is inserted is what the recorder resolved. A wrong
    choice is the wrong control; it cannot be a control that does not exist.
    """
    from tests.test_capture import event

    controller.start()
    controller._test["receiver"].push(*click_pair(200, "QPushButton", text="Apply"))
    controller.poll()
    drop = controller.recording.drops[0]
    drop.evidence = controller.session.evidence_for(
        drop.locator_or_none(), drop.reason, drop.kind)

    addressable = [one for one in drop.evidence["candidates"] if one["target"]]
    assert addressable, "nothing in this application could be addressed at all"
    chosen = addressable[0]

    controller.repair_choose(0, chosen["id"])
    filled = controller.recording.actions[-1]
    assert filled.kind is ActionKind.CLICK
    assert filled.target.definition == chosen["target"]["definition"]
    assert controller.recording.open_drops() == []


def test_an_id_that_is_not_in_the_evidence_is_refused(controller):  # noqa: F811
    """A model naming a candidate that does not exist is the one failure this
    design has to refuse outright."""
    controller.start()
    controller._test["receiver"].push(*click_pair(200, "QPushButton", text="Apply"))
    controller.poll()

    controller.recording.drops[0].evidence = controller.session.evidence_for(
        controller.recording.drops[0].locator_or_none(), "", "")
    with pytest.raises(ControllerError) as raised:
        controller.repair_choose(0, 999)
    assert "not one" in str(raised.value)
    assert controller.recording.open_drops(), "the gap is still open"


def test_a_gap_remembers_how_to_be_asked_about_later():
    """The evidence is gathered on demand, so the drop has to carry the way back
    to the object -- its class, its text, which sibling it was, and what
    contained it."""
    _, recording = _feed(click_pair(200, "QPushButton", text="Apply"))
    again = Recording.loads(recording.dumps())
    drop = again.drops[0]

    assert drop.seen["class"] == "QPushButton"
    locator = drop.locator_or_none()
    assert locator.cls == "QPushButton"
    assert locator.index == drop.sibling_index


# --- a window that is gone by definition -----------------------------------

def test_a_closed_window_keeps_the_name_it_was_closed_under(controller):  # noqa: F811
    """The one case where nothing can be checked, because checking is the
    question.

    A window dismissed from its title bar is gone by the time anything can be
    asked about it. Every candidate list is empty and every honest answer is
    "cannot identify it" -- which left a gap nobody could ever fill, for
    something the filter had named on the way out.
    """
    from tests.test_capture import event

    controller.start()
    controller._test["receiver"].push(
        event("close_window", 500, "TorrentCreatorDialog", "TorrentCreatorDialog",
              path=(("QMainWindow", "MainWindow"),)))
    controller.poll()

    drop, = controller.recording.drops
    drop.evidence = controller.session.evidence_for(
        drop.locator_or_none(), drop.reason, drop.kind)
    assert drop.evidence["candidates"] == [], "the window really is gone"
    assert drop.evidence["closed_proposal"] == {
        "type": "TorrentCreatorDialog", "objectName": "TorrentCreatorDialog",
        "container": {"objectName": "MainWindow"}}

    controller.repair_closed(0)
    filled = controller.recording.actions[-1]
    assert filled.kind is ActionKind.CLOSE_WINDOW
    assert filled.target.definition["objectName"] == "TorrentCreatorDialog"
    # Graded honestly: it was never checked, and the script has to say so.
    assert filled.target.robustness.value == "unresolved"
    assert any("could not be checked" in warning
               for warning in filled.target.warnings)


def test_closing_by_name_is_refused_when_no_name_was_reported(controller):  # noqa: F811
    from tests.test_capture import event

    controller.start()
    controller._test["receiver"].push(event("close_window", 500, "", ""))
    controller.poll()
    if not controller.recording.drops:
        pytest.skip("an unnamed close produced no gap in this tree")
    with pytest.raises(ControllerError):
        controller.repair_closed(0)


def test_the_marker_tells_the_panel_a_window_can_be_closed_by_name(controller):  # noqa: F811
    from tests.test_capture import event

    controller.start()
    controller._test["receiver"].push(
        event("close_window", 500, "TorrentCreatorDialog", "TorrentCreatorDialog"))
    controller.poll()
    drop = controller.recording.drops[0]
    drop.evidence = controller.session.evidence_for(
        drop.locator_or_none(), drop.reason, drop.kind)

    line, = _marker_lines(emit_python(controller.recording))
    payload = json.loads(line.split(DROP_MARKER, 1)[1])
    assert payload["closed_as"]["objectName"] == "TorrentCreatorDialog"


# --- the text around a control ---------------------------------------------

def test_nearby_text_says_where_it_sits_not_merely_that_it_is_close():
    """What names an unnamed control is the label beside it, and "beside" is a
    direction. A label to the left in the same row names it; something forty
    pixels away diagonally is a coincidence."""
    from qat_recorder.evidence import _relationship

    control = (100, 50, 20, 20)
    assert _relationship(control, (10, 50, 80, 20))[0] == "to the left, same row"
    assert _relationship(control, (100, 10, 60, 20))[0] == "above, same column"
    assert _relationship(control, (140, 50, 60, 20))[0] == "to the right, same row"
    assert _relationship(control, (100, 90, 60, 20))[0] == "below, same column"
    assert _relationship(control, (900, 900, 40, 20))[0] == "nearby"


def test_aligned_text_outranks_merely_close_text():
    """A label in the same row belongs to the control. One nearer but unaligned
    does not, and offering it first is how a model is misled."""
    from qat_recorder.evidence import _DIRECTION_RANK

    assert _DIRECTION_RANK["to the left, same row"] < _DIRECTION_RANK["nearby"]
    assert _DIRECTION_RANK["above, same column"] < _DIRECTION_RANK["nearby"]


def test_folding_a_dropped_event_does_not_interrogate_the_application():
    """The reason the panel stalled after the first gap.

    Enumerating every object of the class and resolving each one costs hundreds
    of round trips to the application, and folding happens inside the poll loop,
    which holds the session lock every request waits on. One gap took the
    recorder, the panel and its socket down together for as long as it ran.
    """
    backend, _ = build_tree()
    trips = {"n": 0}
    for name in ("find_all", "properties", "parent"):
        original = getattr(backend, name)

        def counted(func):
            def inner(*args, **kwargs):
                trips["n"] += 1
                return func(*args, **kwargs)
            return inner

        setattr(backend, name, counted(original))

    capture = CaptureSession(backend, app_name="sample")
    capture.feed_all(click_pair(200, "QPushButton", text="Apply"))
    assert capture.finish().drops, "nothing was dropped, so this proves nothing"
    assert trips["n"] < 20, (
        f"folding one dropped event cost {trips['n']} round trips to the "
        "application, inside the lock the whole panel waits on")


def test_what_the_filter_reported_is_offered_when_nothing_can_be_found():
    """A dialog that has closed takes its buttons with it.

    Every candidate list is then empty and every honest answer is "I cannot see
    it" -- which is a refusal, not a repair, and is what the model kept saying.
    The filter named the control on the way past, and the ancestor chain says
    which dialog it was in.
    """
    from qat_recorder.evidence import gather
    from qat_recorder.events import Locator
    from qat_recorder.naming import NameResolver

    backend, _ = build_tree()
    pack = gather(backend, NameResolver(backend),
                  Locator(cls="QPushButton", text="Cancel",
                          path=(("QDialogButtonBox", ""),
                                ("TorrentCreatorDialog", "TorrentCreatorDialog"))),
                  "could not be found through Qat while it was on screen")

    proposed = pack["reported_proposal"]
    assert proposed["type"] == "QPushButton"
    assert proposed["text"] == "Cancel"
    # Scoped. "The Cancel button" matches every dialog in the application.
    assert proposed["container"] == {"type": "QDialogButtonBox"}
