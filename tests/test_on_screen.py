# -*- coding: utf-8 -*-
"""
A button that exists more than once, only one of them on screen.

An application that keeps its other pages alive has the same button several
times over. A replay finds objects through Qat's `wait_for_object`, which only
counts one that is visible and enabled -- so when the recorder has to fall back
to what the event filter reported, it has to count the same way, or it drops
the one click that mattered.
"""

from __future__ import annotations

from qat_recorder.backend import FakeBackend, FakeNode
from qat_recorder.capture import CaptureSession
from qat_recorder.ir import ActionKind
from tests.test_capture import click_pair

BUTTON = ["RoundButton", "QQuickItem", "QObject"]
ITEM = ["QQuickItem", "QObject"]


def pages(*shown):
    """A QML view with one page per flag, each with its own Load Case button."""
    window = FakeNode(["QQuickView", "QObject"], {"objectName": "view"})
    for visible in shown:
        page = window.add(FakeNode(ITEM, {"objectName": "", "visible": visible}))
        layout = page.add(FakeNode(ITEM, {"objectName": "buttonLayout",
                                          "visible": visible}))
        layout.add(FakeNode(BUTTON, {
            "objectName": "loadCaseButton", "text": "Load Case",
            "visible": visible, "enabled": True}))
    return FakeBackend([window])


def record_load_case(backend):
    capture = CaptureSession(backend, app_name="mako")
    capture.feed_all(click_pair(1_000, "RoundButton", "loadCaseButton",
                                text="Load Case",
                                path=[("QQuickItem", "buttonLayout"),
                                      ("QQuickItem", "")]))
    recording = capture.finish()
    return [action for action in recording.actions
            if action.kind is ActionKind.CLICK], recording


def test_the_one_on_screen_is_recorded_not_dropped():
    """Observed in mako_shoulder: "Load Case" twice, one on screen, and the
    click dropped as "could not be found through Qat while it was on screen"."""
    clicks, recording = record_load_case(pages(True, False))
    assert len(clicks) == 1, recording.drops
    assert clicks[0].target.definition["objectName"] == "loadCaseButton"


def test_two_on_screen_at_once_is_still_not_guessed():
    """Two identical buttons both usable is real ambiguity; picking one is the
    guess that made scripts die on "Multiple objects found"."""
    clicks, _ = record_load_case(pages(True, True))
    assert clicks == []
