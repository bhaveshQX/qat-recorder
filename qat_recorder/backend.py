# -*- coding: utf-8 -*-
"""
The seam between the recorder and the application under test.

Everything the naming resolver needs is expressed as five operations. `QatBackend`
implements them against a live application; `FakeBackend` implements them against
an in-memory tree so the resolver can be tested without launching anything.

IMPORTANT — conformance risk
----------------------------
`FakeBackend` reimplements Qat's object-matching semantics from its documented
behaviour: `type` is inheritance-aware, `container` searches descendants
recursively, `parent` matches direct children only, and any other key is an exact
property match. If the real server deviates, the resolver's unit tests would pass
while production silently misbehaves.

`tests/test_conformance.py` pins this down and is marked `live` — it must be run
against a real application before Phase 2 is considered done.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence


class NodeRef(Protocol):
    """Opaque handle to one object in the application under test."""


class Backend(Protocol):
    """The five operations the resolver needs."""

    def top_windows(self) -> Sequence[Any]: ...

    def children(self, node: Any) -> Sequence[Any]: ...

    def parent(self, node: Any) -> Optional[Any]: ...

    def properties(self, node: Any, keys=None) -> Mapping[str, Any]: ...

    def find_all(self, definition: Mapping[str, Any]) -> Sequence[Any]: ...

    def identity(self, node: Any) -> Any:
        """A hashable value that is equal for two handles to the same object."""


# ---------------------------------------------------------------------------
# Live backend
# ---------------------------------------------------------------------------

class QatBackend:
    """Backend talking to a running application through the `qat` package."""

    #: Properties worth reading for naming. Reading every property of every object
    #: is a round trip per property per object, which is far too slow for a walk.
    INTERESTING = (
        "objectName", "id", "text", "title", "windowTitle", "name",
        "accessibleName", "placeholderText", "echoMode", "checkable",
        "enabled", "visible",
    )

    #: Read only when something asks for them by name. Each property is a round
    #: trip, and an audit reads every object in the application -- putting these
    #: in INTERESTING would make a 500-object scan half as fast again to answer
    #: a question it never asks.
    GEOMETRY = ("x", "y", "width", "height")

    #: What a view says about its own selection. `currentRow` on a list,
    #: `currentIndex` on a tab widget or a combo box: real Qt properties, both
    #: readable and writable, which makes them the most durable way to record
    #: "they chose this one" -- no geometry, no scrolling, no row arithmetic.
    SELECTION = ("currentRow", "currentIndex", "currentText")

    def __init__(self, qat_module=None):
        if qat_module is None:
            import qat as qat_module  # noqa: PLC0415
        self.qat = qat_module

    def top_windows(self):
        return list(self.qat.list_top_windows())

    def children(self, node):
        try:
            result = node.children
        except AttributeError:
            return []
        return list(result) if isinstance(result, list) else []

    def parent(self, node):
        """The parent object, or None at the top of the tree.

        Qat represents "no parent" as a *null QtObject* rather than None — its
        definition is None and every attribute access on it fails. Returning that
        object as if it were a real one breaks identity comparison and produces
        nonsense container anchors, so it is normalised to None here.
        """
        try:
            result = node.parent
        except AttributeError:
            return None
        if result is None:
            return None
        try:
            if result.is_null() or result.get_definition() is None:
                return None
        except Exception:                                   # noqa: BLE001
            return None
        return result

    def properties(self, node, keys=None):
        """Best-effort snapshot of an object's properties.

        Uses the object's cached definition where possible and only falls back to
        remote reads for the keys asked for, to keep tree walks affordable.
        `keys` names them explicitly; by default the ones naming needs.
        """
        props = {}
        try:
            props.update(dict(node.get_definition()))
        except Exception:                                   # noqa: BLE001
            pass
        for key in (self.INTERESTING if keys is None else tuple(keys)):
            if key in props:
                continue
            try:
                value = getattr(node, key)
            except Exception:                               # noqa: BLE001
                continue
            if callable(value):
                # Qat hands back a callable when the name resolves to a Qt
                # method, and in Qt the names that matter most are both: `text`,
                # `value`, `currentRow`, `currentIndex`, `currentText` are each
                # a property and a getter. Skipping them -- which this did --
                # meant a list never reported which row was selected, an item
                # never reported its label, and a spin box never reported its
                # value. Three separate mysteries, one line.
                #
                # Only the names asked for are called, and every one of them is
                # a getter that takes no arguments and changes nothing.
                try:
                    value = value()
                except Exception:                           # noqa: BLE001
                    continue
            if not callable(value):
                props[key] = value
        return props

    def all_properties(self, node) -> dict:
        """Every property this object has, asked for in one round trip.

        Qat can list an object's properties, which is worth far more than
        guessing their names: a virtual item wrapper publishes whatever the view
        and its model choose to publish, and no list written here could keep up
        with every Qt class and every application's subclasses. Ask, do not
        assume.
        """
        try:
            listed = node.list_properties()
        except Exception:                                    # noqa: BLE001
            return {}
        found = {}
        for entry in listed or ():
            try:
                name, value = entry
            except (TypeError, ValueError):
                continue
            found[str(name)] = value
        return found

    def call(self, node, method: str, *args):
        """Invoke a Qt method on an object, or None if it cannot be called.

        Qat exposes every meta-method of an object, which is the only way to
        reach things Qt does not publish as properties. An item's label is the
        case that matters here: `data(0)` is Qt::DisplayRole, and it answers for
        views whose items expose no `text` property at all.
        """
        try:
            bound = getattr(node, method)
        except Exception:                                    # noqa: BLE001
            return None
        if not callable(bound):
            return None
        try:
            return bound(*args)
        except Exception:                                    # noqa: BLE001
            return None

    def find_all(self, definition):
        """Objects matching a definition, or an empty list.

        Qat *raises* LookupError when nothing matches rather than returning an
        empty list. Every caller here treats "no match" as a normal answer --
        the resolver tries candidate definitions precisely to discover which
        ones fail -- so the exception is translated into the empty result the
        port promises. Left unhandled, one click on a widget that was later
        hidden aborted an entire recording and discarded every event captured
        with it.

        One asymmetry is worth knowing, because it decides when recording has to
        happen. `find_all_objects` matches objects whether or not they are on
        screen, but `mouse_click` and `type_in` go through `wait_for_object`,
        which adds `visible: true` and `enabled: true` to the definition *and to
        every nested container*. So a definition validated here can still fail on
        replay if the thing it names is not showing at that point in the script.
        Being invisible is survivable; being destroyed is not, which is why
        events are resolved while the session is still running.
        """
        try:
            return list(self.qat.find_all_objects(dict(definition)))
        except LookupError:
            return []

    def identity(self, node):
        """Stable identity for a remote object.

        Qat's own equality uses `cache_uid`, so that is the only sound basis. An
        earlier version fell back to `id()` when it was unavailable, which never
        compares equal between two handles to the same object — the resolver then
        rejected valid definitions and degraded to positional indices without
        anyone noticing. Failing loudly is better.
        """
        definition = {}
        try:
            definition = node.get_definition() or {}
        except Exception:                                   # noqa: BLE001
            definition = {}

        uid = definition.get("cache_uid")
        if uid is None:
            try:
                uid = node.cache_uid
            except Exception as error:                      # noqa: BLE001
                raise RuntimeError(
                    "cannot establish object identity: no cache_uid available "
                    f"(definition={definition!r})") from error
        return ("uid", str(uid))


# ---------------------------------------------------------------------------
# In-memory backend for tests
# ---------------------------------------------------------------------------

class FakeNode:
    """One object in a synthetic tree."""

    __slots__ = ("types", "props", "children_list", "parent_node")

    def __init__(self, type_chain, props=None):
        if isinstance(type_chain, str):
            type_chain = [type_chain]
        self.types = list(type_chain)
        self.props = dict(props or {})
        self.children_list = []
        self.parent_node = None

    @property
    def type(self) -> str:
        return self.types[0]

    def add(self, child: "FakeNode") -> "FakeNode":
        child.parent_node = self
        self.children_list.append(child)
        return child

    def __repr__(self):
        name = self.props.get("objectName") or self.props.get("text") or "<unnamed>"
        return f"<{self.type} {name}>"


class FakeBackend:
    """Backend over a synthetic tree, mirroring Qat's documented match semantics."""

    def __init__(self, roots: Iterable[FakeNode]):
        self.roots = list(roots)

    # -- tree ---------------------------------------------------------------

    def top_windows(self):
        return list(self.roots)

    def children(self, node):
        return list(node.children_list)

    def parent(self, node):
        return node.parent_node

    def properties(self, node, keys=None):
        if keys is None:
            return dict(node.props)
        return {key: node.props[key] for key in keys if key in node.props}

    def identity(self, node):
        return id(node)

    def all_properties(self, node) -> dict:
        return dict(node.props)

    def call(self, node, method: str, *args):
        """`data(role)` is looked up in a per-node `data` mapping, mirroring how
        a real item answers for Qt::DisplayRole."""
        if method == "data" and args:
            values = node.props.get("data")
            if isinstance(values, dict):
                return values.get(args[0])
        return None

    # -- matching -----------------------------------------------------------

    def walk(self):
        """Pre-order depth-first, siblings in declaration order.

        Order is part of the contract, not an implementation detail: positional
        fallbacks index into `find_all` results, so a traversal that reversed
        siblings would produce indices that silently disagree with the real
        application. Kept in document order to match Qt's child ordering.
        """
        stack = list(reversed(self.roots))
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children_list))

    def _ancestors(self, node):
        current = node.parent_node
        while current is not None:
            yield current
            current = current.parent_node

    def _matches(self, node, definition: Mapping[str, Any]) -> bool:
        for key, value in definition.items():
            if key == "type":
                if value not in node.types:      # inheritance-aware
                    return False
            elif key == "container":
                if not any(self._matches(anc, value) for anc in self._ancestors(node)):
                    return False
            elif key == "parent":
                if node.parent_node is None or not self._matches(node.parent_node, value):
                    return False
            elif key in ("visible", "enabled"):
                # As in Qt: on and usable unless something turned it off.
                if node.props.get(key, True) != value:
                    return False
            else:
                if node.props.get(key) != value:
                    return False
        return True

    def find_all(self, definition):
        if isinstance(definition, str):          # Qat's bare-string shorthand
            definition = {"objectName": definition}
        return [node for node in self.walk() if self._matches(node, definition)]
