# -*- coding: utf-8 -*-
"""
Clicking down a dialog's sidebar.

The Preferences dialog has a list of pages down the left. Clicking each one
opened each page, and the recording contained one step: the click that opened
the dialog. Everything inside it had been dropped, because the previous change
made "cannot address the row" mean "record nothing" -- honest, and useless.

A QListWidget knows its own `currentRow`, which is an ordinary Qt property. That
is enough to find the row, and the row knows its text, which is what a person
clicked and what survives the list being reordered.
"""

import pytest

from qat_recorder.backend import FakeNode
from qat_recorder.capture import CaptureSession
from qat_recorder.emit import emit_python
from qat_recorder.events import Locator, RawEvent
from qat_recorder.ir import ActionKind
from tests.fixtures import WIDGET, build_tree

PAGES = ("Behaviour", "Downloads", "Connection", "Speed", "BitTorrent")


def sidebar(with_geometry=False, with_current_row=True):
    """A Preferences sidebar: rows Qat can list, no geometry unless asked."""
    backend, nodes = build_tree()
    props = {"objectName": "tabSelection"}
    if with_current_row:
        props["currentRow"] = 0
    view = nodes["root"].add(FakeNode(["QListWidget"] + WIDGET, props))
    for index, text in enumerate(PAGES):
        row = {"row": index, "text": text}
        if with_geometry:
            row.update({"x": 0, "y": index * 20, "width": 120, "height": 20})
        view.add(FakeNode(WIDGET, row))
    return backend, nodes, view


def click(t, kind="mouse_press", x=10, y=45):
    return RawEvent(
        kind=kind, t=t, button=1, x=x, y=y,
        target=Locator(cls="QWidget", object_name="qt_scrollarea_viewport",
                       path=(("QListWidget", "tabSelection"),
                             ("QWidget", "rootWidget"))))


def test_clicking_a_page_in_the_sidebar_is_recorded(monkeypatch):
    """The bug: this produced nothing at all."""
    backend, _, view = sidebar()
    capture = CaptureSession(backend, app_name="sample")

    capture.feed(click(1000))
    view.props["currentRow"] = 2            # the widget handles the press
    capture.feed(click(1040, kind="mouse_release"))
    capture.feed(click(3000))
    view.props["currentRow"] = 3
    capture.feed(click(3040, kind="mouse_release"))
    recording = capture.finish()

    clicks = [a for a in recording.actions if a.kind is ActionKind.CLICK]
    assert len(clicks) == 2
    assert clicks[0].target.definition == {
        "container": {"objectName": "tabSelection"}, "row": 2}
    assert clicks[0].target.item_text == "Connection"


def test_each_page_becomes_its_own_step(monkeypatch):
    """Five clicks down the sidebar are five steps, not one."""
    backend, _, view = sidebar()
    capture = CaptureSession(backend, app_name="sample")

    for index in range(len(PAGES)):
        capture.feed(click(1000 + index * 1000))
        view.props["currentRow"] = index    # the widget handles the press
        capture.feed(click(1040 + index * 1000, kind="mouse_release"))
    recording = capture.finish()

    clicked = [a.target.item_text for a in recording.actions
               if a.kind is ActionKind.CLICK]
    assert clicked == list(PAGES)


def test_the_step_clicks_the_page_by_name():
    backend, _, view = sidebar()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(click(1000))
    view.props["currentRow"] = 2
    capture.feed(click(1040, kind="mouse_release"))

    source = emit_python(capture.finish())
    assert "click_row(TABSELECTION, 'Connection', recorded=2)" in source
    compile(source, "generated.py", "exec")


def test_geometry_is_used_when_it_is_there():
    """Route 2 still wins over route 3: it identifies the row that was clicked
    rather than the row that ended up selected."""
    backend, _, _ = sidebar(with_geometry=True)
    capture = CaptureSession(backend, app_name="sample")
    capture.feed(click(1000, y=45))         # inside row 2's band
    capture.feed(click(1040, kind="mouse_release", y=45))
    recording = capture.finish()

    click_action = recording.actions[-1]
    assert click_action.target.definition["row"] == 2
    assert click_action.target.item_text == "Connection"


def test_a_view_that_offers_nothing_says_what_it_does_offer():
    """So the next one that defeats all three routes does not cost a round trip
    to diagnose."""
    backend, nodes, _ = sidebar(with_current_row=False)
    opaque = nodes["root"].add(FakeNode(["QTreeView"] + WIDGET,
                                        {"objectName": "opaqueView"}))
    opaque.add(FakeNode(WIDGET, {"row": 0, "colour": "red"}))

    capture = CaptureSession(backend, app_name="sample")
    capture.feed(RawEvent(
        kind="mouse_press", t=1000, button=1, x=5, y=5,
        target=Locator(cls="QWidget", object_name="qt_scrollarea_viewport",
                       path=(("QTreeView", "opaqueView"),
                             ("QWidget", "rootWidget")))))
    capture.feed(RawEvent(kind="mouse_press", t=5000, button=1,
                          target=Locator(cls="QPushButton",
                                         object_name="loginButton")))

    reason = capture.failures[0]
    assert "the view exposes" in reason
    assert "objectName" in reason
    assert "row 0 exposes" in reason
    assert "colour" in reason
