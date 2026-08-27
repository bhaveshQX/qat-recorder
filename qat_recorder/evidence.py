# -*- coding: utf-8 -*-
"""
Everything known about an object at the moment its event was lost.

A gap is noticed minutes later, from a different screen, often after the
application has been closed. By then the only things that can identify the
control are the things somebody thought to write down while it was still there.
Until now that was three properties and a photograph, which is enough to say
what went wrong and not nearly enough to fix it.

So this collects the evidence while the application is alive: what the filter
reported, which of its siblings it was, what else in the application looks like
it, the text sitting next to each of those, and -- for every candidate that can
be addressed at all -- a fully resolved Target, checked against the running
application exactly as a recorded step is.

That last part is what keeps a repair honest whoever chooses it. A person
picking from this list, or a model reading it, is choosing among objects that
demonstrably exist and are demonstrably addressable. Neither is inventing a
locator, which is the only way this can go wrong.

Deliberately not geometry. `QMouseEvent::pos()` is relative to the widget that
received the event, so the click coordinates cannot place that widget among its
siblings -- the arithmetic is circular. The filter's `siblingIndex` answers the
same question directly and is already how `find_by_locator` works.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from qat_recorder.ir import Robustness

#: Enough to choose from, few enough that resolving each is not a stall. A
#: dialog with more identical controls than this has a naming problem that no
#: repair tool should be papering over.
MAX_CANDIDATES = 15

#: Properties worth showing a human or a model. Anything that might name the
#: control, and the geometry -- not to hit-test with, but because "the one in
#: the top left" is how people describe things.
INTERESTING = ("objectName", "text", "title", "accessibleName", "accessibleDescription",
               "toolTip", "placeholderText", "windowTitle", "checked", "enabled",
               "visible", "x", "y", "width", "height")

#: Classes whose text is usually what labels something else.
LABEL_CLASSES = ("QLabel", "QCheckBox", "QRadioButton", "QGroupBox", "QPushButton")


#: How many objects of one class are worth measuring for nearby text. A dialog
#: has tens of labels, not thousands, and every one costs a walk up its parents.
MAX_LABELS_PER_CLASS = 20


def _readable(backend, node, memo=None) -> dict:
    """Whatever of INTERESTING this object will admit to. Never raises.

    Memoised within one gather: measuring the text around twenty-five candidates
    asks the same labels the same questions twenty-five times over, and each
    question is a round trip to the application under test.
    """
    if memo is not None:
        cached = memo.get(id(node))
        if cached is not None:
            return cached
    out = _read_uncached(backend, node)
    if memo is not None:
        memo[id(node)] = out
    return out


def _read_uncached(backend, node) -> dict:
    try:
        properties = backend.properties(node, keys=INTERESTING)
    except TypeError:                    # a backend predating the keys argument
        try:
            properties = backend.properties(node)
        except Exception:                                    # noqa: BLE001
            return {}
    except Exception:                                        # noqa: BLE001
        return {}
    out = {}
    for key in INTERESTING:
        value = properties.get(key)
        if value not in (None, ""):
            out[key] = value
    return out


def _box(properties: Mapping[str, Any]):
    """(x, y, w, h) if this object will admit to a rectangle."""
    values = [properties.get(key) for key in ("x", "y", "width", "height")]
    if any(not isinstance(value, (int, float)) for value in values):
        return None
    return tuple(float(value) for value in values)


def _absolute(backend, node, properties: Mapping[str, Any], depth: int = 12,
              memo=None):
    """Where this object is on the screen, not where it is inside its parent.

    Qt reports a widget's position relative to whatever contains it, so two
    controls in different group boxes have coordinates that cannot be compared
    at all -- and "the label to the left of this check box" is a comparison.
    Walking up the parents and adding the offsets puts everything into one
    space, which is the only thing that makes the directions below mean
    anything.
    """
    box = _box(properties)
    if box is None:
        return None
    x, y, width, height = box
    current = node
    for _ in range(depth):
        try:
            current = backend.parent(current)
        except Exception:                                    # noqa: BLE001
            break
        if current is None:
            break
        outer = _box(_readable(backend, current, memo))
        if outer is None:
            continue
        x += outer[0]
        y += outer[1]
    return (x, y, width, height)


def _relationship(subject, other) -> tuple:
    """How `other` sits against `subject`: (direction, distance).

    The directions people actually use to describe a control that has no name.
    The aligned ones are the crossing cases -- the label in the same row of a
    settings grid, the header above a column -- and they rank first, because
    alignment is what makes text *belong* to a control rather than merely be
    near it.
    """
    sx, sy, sw, sh = subject
    ox, oy, ow, oh = other
    same_row = oy < sy + sh and sy < oy + oh
    same_column = ox < sx + sw and sx < ox + ow

    if same_row and ox + ow <= sx:
        return "to the left, same row", sx - (ox + ow)
    if same_row and ox >= sx + sw:
        return "to the right, same row", ox - (sx + sw)
    if same_column and oy + oh <= sy:
        return "above, same column", sy - (oy + oh)
    if same_column and oy >= sy + sh:
        return "below, same column", oy - (sy + sh)
    return "nearby", abs(ox - sx) + abs(oy - sy)


#: Aligned text beats merely-close text, because alignment is what makes a label
#: belong to a control. Left and above first: that is where Qt dialogs put them.
_DIRECTION_RANK = {
    "to the left, same row": 0,
    "above, same column": 1,
    "to the right, same row": 2,
    "below, same column": 3,
    "nearby": 4,
}


def _labels_near(backend, node, properties: Mapping[str, Any], limit: int = 6,
                 memo=None) -> list:
    """Text around this object: beside it, above it, across from it.

    Not a locator and not pretending to be one. It is how a person recognises a
    control that has no name -- the check box beside "Enable DHT" is *the DHT
    one* to everybody except the object tree -- and it is the most useful thing
    anybody, or anything, can be given when asked which control was meant.
    """
    subject = _absolute(backend, node, properties, memo=memo)
    if subject is None:
        return []

    found = []
    for class_name in LABEL_CLASSES:
        try:
            others = list(backend.find_all({"type": class_name}))[:MAX_LABELS_PER_CLASS]
        except Exception:                                    # noqa: BLE001
            continue
        for other in others:
            if other is node:
                continue
            text = _readable(backend, other, memo)
            words = (text.get("text") or text.get("title")
                     or text.get("accessibleName") or "")
            if not words:
                continue
            box = _absolute(backend, other, text, memo=memo)
            if box is None:
                continue
            direction, distance = _relationship(subject, box)
            found.append((_DIRECTION_RANK[direction], distance, str(words),
                          class_name, direction))

    found.sort(key=lambda item: (item[0], item[1]))
    return [{"text": words, "type": class_name, "where": direction,
             "distance": round(distance, 1)}
            for _, distance, words, class_name, direction in found[:limit]]


def gather(backend, resolver, locator, reason: str = "",
           kind: str = "") -> dict:
    """The evidence pack for one lost event.

    `candidates` are the objects of the same class the application actually has,
    each with a resolved Target where one exists. The one the filter says was
    hit is marked; it is usually right, and the point of listing the rest is the
    times it is not.
    """
    from qat_recorder.capture import find_by_locator     # noqa: PLC0415

    memo: dict = {}
    class_name = (locator.cls or "").strip()
    pack = {
        "reason": reason,
        "class": class_name,
        "objectName": locator.object_name or "",
        "text": locator.text or "",
        "sibling_index": locator.index,
        "path": [{"class": cls, "objectName": name}
                 for cls, name in (locator.path or ())],
        "clicked_at": {"x": locator and getattr(locator, "x", None)},
        "candidates": [],
        "hit": -1,
    }
    # A window closed from its title bar is gone by the time anything can be
    # asked about it -- that is what closing means -- so there are never any
    # candidates and every honest answer is "cannot identify it". But the filter
    # named it on the way out, and a dialog with its own objectName is the
    # strongest kind of locator this project has. It cannot be *checked*, which
    # is a different objection from the usual one, and the replay settles it in
    # seconds rather than leaving a gap nobody can ever fill.
    proposed = {}
    if class_name:
        proposed["type"] = class_name
    if locator.object_name:
        proposed["objectName"] = locator.object_name
    elif locator.text:
        proposed["text"] = locator.text
    # Scoped by the nearest ancestor that has a name of its own. "The Cancel
    # button" matches every dialog in the application; "the Cancel button in
    # TorrentCreatorDialog" matches one, and the ancestor chain is the one thing
    # the filter always reports.
    for step_class, step_name in (locator.path or ()):
        if step_name:
            proposed["container"] = {"objectName": step_name}
            break
        if step_class and step_class not in ("QWidget",):
            proposed["container"] = {"type": step_class}
            break
    if len(proposed) > 1:
        pack["reported_proposal"] = proposed
        # Kept under the old name too: the panel offers a closed window a
        # differently worded button, and that wording is still right.
        if kind == "close_window":
            pack["closed_proposal"] = proposed

    if not class_name:
        return pack

    try:
        matches = list(backend.find_all({"type": class_name}))
    except Exception:                                        # noqa: BLE001
        matches = []
    if not matches:
        return pack

    try:
        hit_node = find_by_locator(backend, locator)
    except Exception:                                        # noqa: BLE001
        hit_node = None

    for ordinal, node in enumerate(matches[:MAX_CANDIDATES]):
        properties = _readable(backend, node, memo)
        entry = {
            "id": ordinal,
            "type": class_name,
            "properties": properties,
            "labels_near": _labels_near(backend, node, properties, memo=memo),
            "target": None,
            "robustness": Robustness.UNRESOLVED.value,
        }
        try:
            target = resolver.resolve(node)
            if target.robustness is not Robustness.UNRESOLVED:
                entry["target"] = target.to_dict()
                entry["robustness"] = target.robustness.value
        except Exception:                                    # noqa: BLE001
            pass
        if hit_node is not None and node is hit_node:
            pack["hit"] = ordinal
        pack["candidates"].append(entry)

    pack["truncated"] = len(matches) > MAX_CANDIDATES
    return pack


def chosen_target(pack: Mapping[str, Any], candidate_id: int) -> Optional[dict]:
    """The resolved Target of one candidate, or None if it has none.

    The only way a choice becomes a step. Whoever chose -- a person clicking, a
    model answering -- picks an id out of this list, and what gets inserted is
    the Target this module resolved against the running application. Nothing
    downstream ever accepts a definition from outside.
    """
    for candidate in pack.get("candidates", ()):
        if int(candidate.get("id", -1)) == int(candidate_id):
            return candidate.get("target")
    return None
