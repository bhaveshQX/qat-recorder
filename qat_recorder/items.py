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

#: Anything that draws rows from a model. Matched by suffix so an application's
#: own subclass -- qBittorrent's TransferListWidget, the Preferences dialog's
#: tab list -- is covered without naming it.
VIEW_SUFFIXES = ("View", "ListWidget", "TreeWidget", "TableWidget",
                 "ItemView", "ListView", "TreeView", "TableView",
                 "ColumnView", "Selection")


def is_view(class_name: str) -> bool:
    """Whether this widget draws rows rather than owning child widgets."""
    name = (class_name or "").strip()
    return bool(name) and name.endswith(VIEW_SUFFIXES)


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


# ---------------------------------------------------------------------------
# Finding the row without the event filter's help
#
# Which row was clicked is reported by the native filter, which asks Qt. That
# works and it is fast, but it makes a semantic decision depend on whether
# somebody remembered to run `build-filter` -- and when they had not, a click
# on a tab list became a click on the middle of the tab list, which selected
# the wrong tab, and the following step failed looking for a widget on a page
# that was never shown. A step that quietly does the wrong thing is worse than
# one that fails.
#
# So there is a second route to the same answer, through Qat itself: Qat wraps
# each item in a virtual widget with real geometry -- it has to, or it could not
# click one -- so the rows can be walked and hit-tested against the point that
# was clicked. Slower, and entirely independent of which filter is installed.
#
# Qat is the source of truth here and the filter is only an accelerator. That is
# the right way round: what the recorder can name is exactly what Qat can find.
# ---------------------------------------------------------------------------

#: Property spellings for an item's geometry, most likely first. Which one a
#: version of Qat uses is not documented, so all of them are tried and the
#: recorder degrades honestly when none is present.
BOUNDS_KEYS = (("x", "y", "width", "height"),
               ("left", "top", "width", "height"))

NESTED_BOUNDS_KEYS = ("bounds", "geometry", "rect")

#: How far down a view to look. Deep enough for a tab list or a settings tree,
#: shallow enough that a hundred-thousand-row table does not stall a recording.
MAX_ROWS = 300


def bounds_of(properties: Mapping[str, Any]) -> Optional[tuple]:
    """(x, y, width, height) for an item, whatever Qat calls them."""
    for nested in NESTED_BOUNDS_KEYS:
        inner = properties.get(nested)
        if isinstance(inner, Mapping):
            found = bounds_of(inner)
            if found:
                return found

    for keys in BOUNDS_KEYS:
        values = []
        for key in keys:
            value = properties.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                values = []
                break
            values.append(float(value))
        if len(values) == 4 and values[2] > 0 and values[3] > 0:
            return tuple(values)
    return None


def contains(bounds: tuple, x: float, y: float) -> bool:
    left, top, width, height = bounds
    return left <= x < left + width and top <= y < top + height


# ---------------------------------------------------------------------------
# The third route, and usually the best one
#
# A view will say what is selected: `currentRow` on a list, `currentIndex` on a
# tab widget or a combo box. Real Qt properties, readable and writable, which
# makes them the most durable record of "they chose this one" -- no geometry, no
# scrolling, no row arithmetic, nothing that a resized window can disturb.
#
#     qat.wait_for_object(TABSELECTION).currentRow = 2
#
# It is not a click, and for a tree where clicking expands a node it would be
# the wrong thing, which is why it comes after addressing the item properly.
# But when the row cannot be determined at all, this is the difference between
# a recording that works and a step that has to be dropped.
# ---------------------------------------------------------------------------

SELECTION_PROPERTIES = ("currentRow", "currentIndex")


def selection_property(properties: Mapping[str, Any]) -> str:
    """Which property this view uses to say what is selected, if any."""
    for name in SELECTION_PROPERTIES:
        value = properties.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return name
        # Qat may hand an integer back as a string.
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return name
    return ""


def discover_row(backend, container: Mapping[str, Any], x: float, y: float,
                 limit: int = MAX_ROWS) -> Optional[tuple]:
    """Ask Qat which row of this view holds the point. (row, text) or None.

    Walks upward from row 0 and stops at the first row Qat does not have, which
    is the end of the view. Returns None when Qat exposes no geometry for its
    items -- in which case the caller must not guess.
    """
    geometry_seen = False
    for row in range(limit):
        definition = item_definition(container, row)
        try:
            matches = backend.find_all(definition)
        except Exception:                                    # noqa: BLE001
            return None
        if len(matches) != 1:
            break
        try:
            properties = dict(backend.properties(
                matches[0], keys=("x", "y", "width", "height", "text")))
        except TypeError:            # a backend that predates the keys argument
            properties = dict(backend.properties(matches[0]))
        except Exception:                                    # noqa: BLE001
            continue
        bounds = bounds_of(properties)
        if bounds is None:
            continue
        geometry_seen = True
        if contains(bounds, x, y):
            return row, str(properties.get("text", "") or "")

    if not geometry_seen:
        return None                 # no geometry at all: nothing to hit-test
    return None
