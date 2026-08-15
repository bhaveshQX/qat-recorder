# -*- coding: utf-8 -*-
"""
Reading properties that Qat hands back as methods.

In Qt the names that matter most are both a property and a getter: `text`,
`value`, `currentRow`, `currentIndex`, `currentText`. Qat resolves those to a
callable, and the backend used to drop anything callable on the floor -- so a
list never reported which row was selected, an item never reported its label,
and a spin box never reported its value.

That is three separate mysteries from one line, and it cost several rounds of
debugging aimed at views, at filters and at item addressing, none of which were
the problem.
"""

import pytest

from qat_recorder.backend import QatBackend


class Method:
    """What Qat returns when a name resolves to a Qt method."""

    def __init__(self, value, calls=None):
        self.value = value
        self.calls = calls if calls is not None else []

    def __call__(self):
        self.calls.append(True)
        return self.value


class Explosive:
    """A method that cannot be called."""

    def __call__(self):
        raise RuntimeError("cannot invoke this remotely")


class FakeQtObject:
    """Stands in for a Qat QtObject, method-valued attributes and all."""

    def __init__(self, definition=None, **attributes):
        self._definition = dict(definition or {})
        self._attributes = attributes

    def get_definition(self):
        return dict(self._definition)

    def __getattr__(self, name):
        try:
            return self.__dict__["_attributes"][name]
        except KeyError:
            raise AttributeError(name)


@pytest.fixture()
def backend():
    return QatBackend(qat_module=object())


def test_a_property_returned_as_a_method_is_still_read(backend):
    node = FakeQtObject(currentRow=Method(4))
    assert backend.properties(node, keys=("currentRow",)) == {"currentRow": 4}


def test_a_plain_value_is_left_alone(backend):
    node = FakeQtObject(currentRow=4)
    assert backend.properties(node, keys=("currentRow",)) == {"currentRow": 4}


def test_only_the_names_asked_for_are_called(backend):
    """Every one of them is a getter that takes no arguments and changes
    nothing; nothing else is invoked."""
    calls = []
    node = FakeQtObject(currentRow=Method(4, calls),
                        deleteLater=Method(None, calls))
    backend.properties(node, keys=("currentRow",))
    assert len(calls) == 1


def test_a_method_that_cannot_be_called_is_skipped(backend):
    node = FakeQtObject(text=Explosive(), objectName="tabSelection")
    read = backend.properties(node, keys=("text", "objectName"))
    assert read == {"objectName": "tabSelection"}


def test_the_cached_definition_still_wins(backend):
    """No round trip for something already known."""
    node = FakeQtObject({"objectName": "tabSelection"},
                        objectName=Explosive())
    assert backend.properties(node, keys=("objectName",))["objectName"] == \
        "tabSelection"


def test_the_selection_of_a_list_can_now_be_read(backend):
    """The failure this came from: a sidebar whose clicks were dropped because
    its `currentRow` looked like a method and was thrown away."""
    from qat_recorder.items import selection_property

    node = FakeQtObject(currentRow=Method(3))
    properties = backend.properties(node, keys=("currentRow", "currentIndex"))
    assert selection_property(properties) == "currentRow"


def test_an_items_label_can_now_be_read(backend):
    from qat_recorder.items import item_text_of

    node = FakeQtObject(text=Method("Connection"))
    assert item_text_of(backend, node) == "Connection"


def test_a_spin_boxes_value_can_now_be_read(backend):
    node = FakeQtObject(value=Method(8077))
    assert backend.properties(node, keys=("value",)) == {"value": 8077}
