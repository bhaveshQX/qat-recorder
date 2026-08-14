# -*- coding: utf-8 -*-
"""
Clicks that land on Qt's own widgets rather than the application's.

A Qt widget is built from other widgets: a tree view owns a viewport, two
scrollbars and a container for each. A person never clicks one on purpose --
they click *through* it. Recording them as targets produced

    mouse_click({"objectName": "qt_scrollarea_vcontainer", "parent": {...}})

which failed on replay, because Qt only creates that container while a scrollbar
is needed and only shows it while one is shown.
"""

import pytest

from qat_recorder.backend import FakeNode
from qat_recorder.capture import (
    CaptureSession, is_chrome, is_internal, owner_of,
)
from qat_recorder.events import Locator
from qat_recorder.ir import ActionKind
from tests.fixtures import WIDGET, build_tree
from tests.test_capture import click_pair


@pytest.mark.parametrize("name, expected", [
    ("qt_scrollarea_viewport", True),
    ("_q_something", True),
    ("treeView", False),
    ("", False),
])
def test_qt_names_its_own_widgets(name, expected):
    assert is_internal(name) is expected


@pytest.mark.parametrize("cls, name", [
    ("QWidget", "qt_scrollarea_vcontainer"),
    ("QWidget", "qt_scrollarea_hcontainer"),
    ("QScrollBar", ""),
    ("QToolButton", "qt_toolbar_ext_button"),
    ("QSizeGrip", ""),
])
def test_scrollbars_and_furniture_are_chrome(cls, name):
    assert is_chrome(cls, name) is True


@pytest.mark.parametrize("cls, name", [
    ("QWidget", "qt_scrollarea_viewport"),      # a surface, not chrome
    ("QPushButton", "loginButton"),
    ("QTreeView", "treeView"),
])
def test_real_controls_are_not_chrome(cls, name):
    assert is_chrome(cls, name) is False


def test_the_owner_is_the_nearest_application_widget():
    locator = Locator(
        cls="QWidget", object_name="qt_scrollarea_viewport",
        path=(("QWidget", "qt_scrollarea_hcontainer"),
              ("QTreeView", "treeView"),
              ("QMainWindow", "mainWindow")))
    owner = owner_of(locator)
    assert owner is not None
    assert owner.object_name == "treeView"
    # And the promoted locator keeps the rest of the chain, so it can still be
    # scoped if the name alone is ambiguous.
    assert owner.path == (("QMainWindow", "mainWindow"),)


def test_an_internal_widget_with_no_owner_has_none():
    assert owner_of(Locator(cls="QWidget", object_name="qt_x")) is None


# --- through a session ------------------------------------------------------

@pytest.fixture()
def tree_session():
    """A tree view built the way Qt builds one, internals and all."""
    backend, nodes = build_tree()
    view = nodes["root"].add(FakeNode(["QTreeView"] + WIDGET,
                                      {"objectName": "treeView"}))
    view.add(FakeNode(WIDGET, {"objectName": "qt_scrollarea_viewport"}))
    view.add(FakeNode(WIDGET, {"objectName": "qt_scrollarea_vcontainer"}))
    return CaptureSession(backend, app_name="sample"), nodes


def test_a_click_through_a_viewport_belongs_to_the_view(tree_session):
    capture, _ = tree_session
    capture.feed_all(click_pair(
        1000, "QWidget", "qt_scrollarea_viewport",
        path=[("QTreeView", "treeView"), ("QWidget", "rootWidget")]))
    recording = capture.finish()

    click = recording.actions[-1]
    assert click.kind is ActionKind.CLICK
    assert click.target.definition == {"objectName": "treeView"}


def test_such_a_click_admits_what_it_does_not_know(tree_session):
    """It clicks the view, not the row that was under the pointer."""
    capture, _ = tree_session
    capture.feed_all(click_pair(
        1000, "QWidget", "qt_scrollarea_viewport",
        path=[("QTreeView", "treeView"), ("QWidget", "rootWidget")]))
    recording = capture.finish()

    assert "not the item under the pointer" in recording.actions[-1].note


def test_a_click_on_a_scrollbar_is_not_a_step(tree_session):
    capture, _ = tree_session
    capture.feed_all(click_pair(
        1000, "QWidget", "qt_scrollarea_vcontainer",
        path=[("QTreeView", "treeView"), ("QWidget", "rootWidget")]))
    recording = capture.finish()

    assert [a.kind for a in recording.actions] == [ActionKind.LAUNCH]
    assert capture.unresolved == 2
    assert "scrolling is not a step" in capture.failures[0]


def test_an_ordinary_click_is_untouched(tree_session):
    capture, _ = tree_session
    capture.feed_all(click_pair(1000, "QPushButton", "loginButton"))
    recording = capture.finish()

    click = recording.actions[-1]
    assert click.target.definition == {"objectName": "loginButton"}
    assert click.note == ""
