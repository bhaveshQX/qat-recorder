# -*- coding: utf-8 -*-
"""
Rows in lists, trees and tables — and the combo boxes built out of them.

The third and last family where the widget that receives a click is not the
thing that was clicked. Menus were the first, Qt's own internal widgets the
second, and this one matters most, because a real application's interesting
state lives in its lists.

A row is a model index, not a widget. The click is delivered to the viewport,
so a recorder that trusts the receiver produces `mouse_click(treeView)` --
which, on replay, clicks whatever row happens to be in the middle of the tree
that day. Qat solves it the way it solves menus, by wrapping the model index in
a virtual widget::

    {"container": {"objectName": "treeView"}, "row": 3, "column": 0}

Two decisions here are the whole point.

**The row index is recorded, and so is the text.** Qat addresses items by
position, which is exactly as fragile as it sounds: insert a row above and every
index below it is wrong. So the generated test looks the item up by its text and
falls back to the recorded index only when nothing matches. That turns Qat's
positional primitive into the durable "the row that says X" that a person meant.

**A combo box is not recorded as clicks at all.** Opening the popup and clicking
an item is two clicks on two transient objects, the second of which
(`QComboBoxListView`) does not exist unless the popup is showing -- a real
recording produced `mouse_click({"type": "QComboBoxListView"})` and failed on
exactly that. What the person did was choose a value, so the value is what gets
recorded.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from qat_recorder.events import Locator

#: Classes whose popup is a list view rather than a menu.
COMBO_MARKERS = ("ComboBox",)

#: Qt's internal container for a combo box popup, and the list inside it.
COMBO_POPUP_CLASSES = ("QComboBoxListView", "QComboBoxPrivateContainer")


def is_combo(class_name: str) -> bool:
    return (class_name or "").strip().endswith(COMBO_MARKERS)


def combo_owner(locator: Locator) -> Optional[str]:
    """The object name of the combo box this click belongs to, if any.

    A combo popup is a top-level window, but Qt keeps the combo box as its
    parent, so the chain the filter reports leads back to it.
    """
    if not (is_combo(locator.cls)
            or locator.cls in COMBO_POPUP_CLASSES
            or locator.item_view_class in COMBO_POPUP_CLASSES):
        # Not obviously a combo; it may still be one deeper in the chain.
        if not any(is_combo(cls) or cls in COMBO_POPUP_CLASSES
                   for cls, _ in locator.path):
            return None

    for cls, object_name in locator.path:
        if is_combo(cls) and object_name:
            return object_name
    return None


def item_definition(container: Mapping[str, Any], row: int,
                    column: int = 0) -> dict:
    """Qat's address for one item of a view."""
    definition: dict = {"container": dict(container), "row": int(row)}
    if column:
        definition["column"] = int(column)
    return definition
