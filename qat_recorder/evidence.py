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
MAX_CANDIDATES = 25

#: Properties worth showing a human or a model. Anything that might name the
#: control, and the geometry -- not to hit-test with, but because "the one in
#: the top left" is how people describe things.
INTERESTING = ("objectName", "text", "title", "accessibleName", "accessibleDescription",
               "toolTip", "placeholderText", "windowTitle", "checked", "enabled",
               "visible", "x", "y", "width", "height")

#: Classes whose text is usually what labels something else.
LABEL_CLASSES = ("QLabel", "QCheckBox", "QRadioButton", "QGroupBox", "QPushButton")


def _readable(backend, node) -> dict:
    """Whatever of INTERESTING this object will admit to. Never raises."""
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


def _labels_near(backend, properties: Mapping[str, Any], limit: int = 4) -> list:
    """Text close to this object, nearest first.

    Not a locator and not pretending to be one: it is how a person recognises a
    control that has no name. The check box beside "Enable DHT" is *the DHT one*
    to everybody except the object tree.
    """
    x, y = properties.get("x"), properties.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return []

    found = []
    for class_name in LABEL_CLASSES:
        try:
            for other in backend.find_all({"type": class_name}):
                text = _readable(backend, other)
                words = text.get("text") or text.get("title") or ""
                ox, oy = text.get("x"), text.get("y")
                if not words or not isinstance(ox, (int, float)) \
                        or not isinstance(oy, (int, float)):
                    continue
                distance = abs(ox - x) + abs(oy - y)     # near enough to rank by
                found.append((distance, str(words), class_name))
        except Exception:                                    # noqa: BLE001
            continue
    found.sort(key=lambda item: item[0])
    return [{"text": words, "type": class_name, "distance": round(distance, 1)}
            for distance, words, class_name in found[:limit]]


def gather(backend, resolver, locator, reason: str = "",
           kind: str = "") -> dict:
    """The evidence pack for one lost event.

    `candidates` are the objects of the same class the application actually has,
    each with a resolved Target where one exists. The one the filter says was
    hit is marked; it is usually right, and the point of listing the rest is the
    times it is not.
    """
    from qat_recorder.capture import find_by_locator     # noqa: PLC0415

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
    if kind == "close_window":
        proposed = {}
        if class_name:
            proposed["type"] = class_name
        if locator.object_name:
            proposed["objectName"] = locator.object_name
        if proposed:
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
        properties = _readable(backend, node)
        entry = {
            "id": ordinal,
            "type": class_name,
            "properties": properties,
            "labels_near": _labels_near(backend, properties),
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
