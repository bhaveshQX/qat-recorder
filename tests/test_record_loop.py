# -*- coding: utf-8 -*-
"""
When events are resolved, rather than how.

An object can only be named while it exists. This is the difference between a
recording that contains the click which closed a dialog and one that does not,
and it cost a full debugging round on a real application to find.
"""

from qat_recorder.backend import FakeNode
from qat_recorder.capture import CaptureSession
from qat_recorder.cli import pump
from qat_recorder.ir import ActionKind
from tests.fixtures import BUTTON, GROUP, build_tree
from tests.test_capture import click_pair


def _with_dialog():
    """A tree with a dialog open, and a way to close it."""
    backend, nodes = build_tree()
    dialog = nodes["root"].add(FakeNode(GROUP, {"objectName": "cookiesDialog"}))
    dialog.add(FakeNode(BUTTON, {"text": "&OK"}))

    def close():
        # What Qt does when a dialog is dismissed: the contents go away.
        nodes["root"].children_list.remove(dialog)

    return backend, nodes, close


def test_a_click_in_a_dialog_survives_if_it_is_resolved_while_open():
    backend, _, close = _with_dialog()
    capture = CaptureSession(backend, app_name="sample")

    capture.feed_all(click_pair(1000, "QPushButton", text="&OK"))
    close()
    capture.feed_all(click_pair(2000, "QPushButton", "loginButton"))
    recording = capture.finish()

    clicked = [action.target.definition for action in recording.actions
               if action.kind is ActionKind.CLICK]
    assert {"type": "QPushButton", "text": "&OK"} in clicked
    assert capture.unresolved == 0


def test_the_same_click_is_lost_if_it_is_resolved_afterwards():
    """The old behaviour, kept as a test so the reason is not forgotten."""
    backend, _, close = _with_dialog()
    capture = CaptureSession(backend, app_name="sample")

    close()                                   # session over, dialog gone
    capture.feed_all(click_pair(1000, "QPushButton", text="&OK"))
    recording = capture.finish()

    assert [a.kind for a in recording.actions] == [ActionKind.LAUNCH]
    assert capture.unresolved == 2
    assert "could not be found" in capture.failures[0]


def test_the_drop_report_collapses_repeats_and_explains_itself():
    from qat_recorder.capture import dropped_report

    text = dropped_report(["click on A: gone", "click on A: gone",
                           "click on B: gone"])
    assert "2 x  click on A: gone" in text
    assert "1 x  click on B: gone" in text
    assert "the replay diverges" in text


def test_the_drop_report_says_so_when_nothing_was_dropped():
    from qat_recorder.capture import dropped_report

    assert "Every event was recorded" in dropped_report([])


class FakeReceiver:
    """Hands out one batch of events per drain, then nothing."""

    def __init__(self, batches):
        self.batches = list(batches)

    def drain(self, timeout=0.0):
        return self.batches.pop(0) if self.batches else []


class RecordingSession:
    """Notes when it was fed, against a clock the test controls."""

    def __init__(self, clock):
        self.clock = clock
        self.fed_at = []
        self.flushes = 0

    def feed_all(self, events):
        self.fed_at.extend(self.clock() for _ in events)

    def flush_stale(self):
        self.flushes += 1


def test_the_record_loop_feeds_during_the_session_not_after():
    now = [0.0]

    def clock():
        return now[0]

    def sleep(seconds):
        now[0] += seconds

    session = RecordingSession(clock)
    receiver = FakeReceiver([["a"], [], ["b", "c"]])

    captured = pump(receiver, session, seconds=1.0, interval=0.25,
                    clock=clock, sleep=sleep)

    assert captured == 3
    # Every event was fed before the session's deadline, not in a batch at 1.0.
    assert session.fed_at == [0.0, 0.5, 0.5]
    # Idle turns close a group nothing more will join, so a last click is not
    # left buffered until the session ends.
    assert session.flushes >= 1
