# -*- coding: utf-8 -*-
"""
Telling "the object is gone" apart from "Qat never had it".

A replay that cannot find its object says so, and says nothing about why. The
two reasons want opposite work: an application that changed is a re-record, and
an object Qat's tree never contained is not a recorder problem at all -- no
generated test can address what Qat cannot see, however plainly it is on screen
and however clearly the event filter reads its name from inside the process.

Measured on a QML application: every step of a session named an objectName the
filter had read off the QObject, and every one of them was missing from Qat.
"""

from qat_recorder.cli import build_parser
from qat_recorder.probe import report


class FakeObject:
    def __init__(self, type_name, object_name="", children=()):
        self._definition = {"type": type_name, "objectName": object_name}
        self.children = list(children)

    def get_definition(self):
        return dict(self._definition)


class FakeQat:
    """Answers the three questions the probe asks, from a fixed tree."""

    def __init__(self, windows=(), objects=()):
        self._windows = list(windows)
        self._objects = list(objects)

    def list_top_windows(self):
        return list(self._windows)

    def find_all_objects(self, definition):
        matches = []
        for obj in self._objects:
            got = obj.get_definition()
            if all(got.get(key) == value for key, value in definition.items()):
                matches.append(obj)
        return matches


def lines(qat, names=()) -> str:
    collected = []
    report(qat, names, out=collected.append)
    return "\n".join(collected)


def test_an_application_whose_qml_qat_cannot_see():
    """The mako_shoulder case. Qat has the window and nothing inside it, and
    the names the replay asked for are simply absent."""
    window = FakeObject("VIS::QuickView_C", "mainWindow")
    text = lines(FakeQat(windows=[window], objects=[window]),
                 names=["roundButton", "openCase"])

    assert "contains no QML objects at all" in text
    assert "roundButton" in text and "Qat does not have the object" in text
    # And it says where to read what has already been measured about this.
    assert "top-level QQuickView" in text


def test_an_application_qat_can_see_qml_in():
    button = FakeObject("RoundButton", "roundButton")
    window = FakeObject("QQuickWindow", "mainWindow", children=[button])
    text = lines(FakeQat(windows=[window], objects=[window, button]),
                 names=["roundButton"])

    assert "contains no QML objects" not in text
    assert "every name asked for" in text
    # The tree is printed, so an unexpected shape is visible rather than
    # summarised away.
    assert "RoundButton" in text


def test_a_name_missing_from_an_application_qat_can_otherwise_see():
    """Different finding, different advice: this one is about that object."""
    other = FakeObject("QQuickItem", "somethingElse")
    window = FakeObject("QQuickWindow", "mainWindow", children=[other])
    text = lines(FakeQat(windows=[window], objects=[window, other]),
                 names=["roundButton"])

    assert "but not ['roundButton']" in text
    assert "a screen that had not been" in text


def test_no_top_window_is_named_as_the_wrong_process():
    """Qat attached to something that draws nothing. A launch script starting
    the real binary as a child does exactly this."""
    text = lines(FakeQat())
    assert "no top-level window" in text
    assert "child" in text


def test_the_command_is_reachable_from_the_cli():
    args = build_parser().parse_args(
        ["probe", "--launch", "/opt/app.sh", "--pause", "roundButton"])
    assert args.command == "probe"
    assert args.launch == "/opt/app.sh"
    assert args.pause is True
    assert args.names == ["roundButton"]


class Labelled:
    """A control whose label lives in a child item, as a custom QML button's
    often does -- which leaves the recorder with nothing but its type."""

    def __init__(self, type_name, visible=True, text=None, children=()):
        self._definition = {"type": type_name, "visible": visible}
        if text is not None:
            self.text = text
        self.children = list(children)

    def get_definition(self):
        return dict(self._definition)


def test_types_lists_the_visible_ones_and_the_text_inside_them():
    from qat_recorder.probe import inspect_types

    shown = Labelled("DarlinMessageDialogButton",
                     children=[Labelled("Text", text="Discard")])
    hidden = Labelled("DarlinMessageDialogButton", visible=False,
                      children=[Labelled("Text", text="Hidden")])
    collected = []
    inspect_types(FakeQat(objects=[shown, hidden]),
                  ["DarlinMessageDialogButton", "ItemDelegate"],
                  out=collected.append)
    text = "\n".join(collected)
    assert "text inside it: Discard" in text
    assert "Hidden" not in text
    assert "(nothing but its type)" in text
    assert "visible ItemDelegate" in text and "none on screen" in text


def test_probe_takes_types_on_the_command_line():
    args = build_parser().parse_args(
        ["probe", "--launch", "/opt/app.sh", "--type", "ItemDelegate",
         "--type", "DarlinMessageDialogButton"])
    assert args.type == ["ItemDelegate", "DarlinMessageDialogButton"]
