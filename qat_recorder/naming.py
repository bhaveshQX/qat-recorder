# -*- coding: utf-8 -*-
"""
Object-name resolution — the quality ceiling of the whole recorder.

A recorded script is only as durable as the object definitions in it. The resolver
escalates through candidate definitions from most to least durable and, critically,
**validates each candidate against the live application before accepting it**:
a candidate is only used if `find_all(candidate)` returns exactly the object we
are naming. Ambiguity surfaces during recording rather than weeks later in CI.

Escalation ladder, best first:

  1. objectName                      -> STRONG
  2. objectName + type               -> STRONG
  3. QML id                          -> STRONG
  4. type + stable identifying prop   -> MODERATE
  5. type + visible text              -> WEAK      (breaks under translation)
  ... each of the above, re-tried scoped by container / parent ...
  6. definition + positional index    -> FRAGILE   (breaks if siblings reorder)
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from qat_recorder.ir import Robustness, Target

#: Properties that identify an object but are subject to translation.
TEXT_PROPERTIES = ("text", "title", "windowTitle", "placeholderText")

#: Properties that identify an object and are not user-visible strings.
STABLE_PROPERTIES = ("accessibleName", "name")

I18N_WARNING = "depends on visible text; will break under translation"
INDEX_WARNING = "positional: breaks if siblings are added, removed or reordered"
UNRESOLVED_WARNING = "no unique definition found; this step will not replay reliably"


def is_secret_field(props: Mapping[str, Any]) -> bool:
    """True for password-style inputs.

    `QLineEdit::Normal` means the contents are visible; anything else (Password,
    NoEcho, PasswordEchoOnEdit) must never be written to a recording.

    Qat reports this property as the *enum name string* ('Normal', 'Password'),
    not the numeric value — verified against a live application. An earlier
    version assumed an int and returned False for every real password field,
    which would have written credentials into recordings. Both forms are handled
    because the representation is not guaranteed across Qat/Qt versions, and
    anything unrecognised is treated as secret: over-redacting is recoverable,
    leaking is not.
    """
    echo = props.get("echoMode")
    if echo is None or isinstance(echo, bool):
        return False
    if isinstance(echo, int):
        return echo != 0
    text = str(echo).strip()
    if not text:
        return False
    if text.lstrip("-").isdigit():
        return int(text) != 0
    return text.lower() != "normal"


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class NameResolver:
    """Produces the most durable definition that uniquely identifies an object."""

    def __init__(self, backend, max_scope_depth: int = 4):
        self.backend = backend
        self.max_scope_depth = max_scope_depth

    # -- public ------------------------------------------------------------

    def resolve(self, node) -> Target:
        props = self.backend.properties(node)
        label = self._label(props, node)

        for definition, strategy, robustness, warnings in self._candidates(node, props):
            if self._identifies(definition, node):
                return Target(
                    definition=definition,
                    strategy=strategy,
                    robustness=robustness,
                    warnings=tuple(warnings),
                    label=label,
                )

        # Nothing unique. Fall back to a positional reference against the most
        # specific non-unique definition we have.
        fallback = self._positional(node, props)
        if fallback is not None:
            definition, index = fallback
            return Target(
                definition=definition,
                strategy="index",
                robustness=Robustness.FRAGILE,
                warnings=(INDEX_WARNING,),
                label=label,
                index=index,
            )

        return Target(
            definition=self._bare(props, node),
            strategy="none",
            robustness=Robustness.UNRESOLVED,
            warnings=(UNRESOLVED_WARNING,),
            label=label,
        )

    # -- candidate generation ----------------------------------------------

    def _candidates(self, node, props) -> Iterable:
        """Yield (definition, strategy, robustness, warnings), best first."""
        bases = list(self._bases(props, node))

        # Unscoped first: the shortest definition that works is the best one.
        for definition, strategy, robustness, warnings in bases:
            yield definition, strategy, robustness, warnings

        # Then the same bases, narrowed by scope. Nearest scope first.
        scopes = list(self._scopes(node))
        for scope_def, scope_kind in scopes:
            for definition, strategy, robustness, warnings in bases:
                yield (
                    {**definition, scope_kind: scope_def},
                    f"{strategy}+{scope_kind}",
                    _weaken_to_moderate(robustness),
                    warnings,
                )

    def _bases(self, props, node) -> Iterable:
        node_type = _clean(props.get("type")) or self._type_of(node)

        object_name = _clean(props.get("objectName"))
        if object_name:
            yield {"objectName": object_name}, "objectName", Robustness.STRONG, ()
            if node_type:
                yield ({"objectName": object_name, "type": node_type},
                       "objectName+type", Robustness.STRONG, ())

        qml_id = _clean(props.get("id"))
        if qml_id:
            yield {"id": qml_id}, "id", Robustness.STRONG, ()

        for key in STABLE_PROPERTIES:
            value = _clean(props.get(key))
            if value and node_type:
                yield ({"type": node_type, key: value},
                       f"type+{key}", Robustness.MODERATE, ())

        for key in TEXT_PROPERTIES:
            value = _clean(props.get(key))
            if value and node_type:
                yield ({"type": node_type, key: value},
                       f"type+{key}", Robustness.WEAK, (I18N_WARNING,))

        if node_type:
            yield {"type": node_type}, "type", Robustness.MODERATE, ()

    def _scopes(self, node) -> Iterable:
        """Container/parent constraints, nearest first.

        `container` searches descendants recursively; `parent` matches direct
        children only and so is the tighter of the two.
        """
        parent = self.backend.parent(node)
        if parent is not None:
            parent_def = self._anchor_definition(parent)
            if parent_def:
                yield parent_def, "parent"

        depth = 0
        current = self.backend.parent(node)
        while current is not None and depth < self.max_scope_depth:
            anchor = self._anchor_definition(current)
            if anchor:
                yield anchor, "container"
            current = self.backend.parent(current)
            depth += 1

    def _anchor_definition(self, node) -> Optional[dict]:
        """A definition for an ancestor, usable to scope a search.

        Only anchors that are themselves unique are worth using — scoping by an
        ambiguous container just moves the ambiguity.
        """
        props = self.backend.properties(node)
        name = _clean(props.get("objectName"))
        if name:
            candidate = {"objectName": name}
            if len(self.backend.find_all(candidate)) == 1:
                return candidate
        qml_id = _clean(props.get("id"))
        if qml_id:
            candidate = {"id": qml_id}
            if len(self.backend.find_all(candidate)) == 1:
                return candidate
        return None

    def _positional(self, node, props):
        """Most specific non-unique definition plus this object's index in it.

        Ranked by fewest matches first, then by most constrained definition. The
        tie-break matters: `{type, text}` and `{type, text, parent: X}` may both
        match two objects, but indexing into the scoped one is safer — a new
        matching object elsewhere in the tree cannot shift the index.
        """
        best_score = None
        best = None
        target_id = self.backend.identity(node)

        for definition, _, _, _ in self._candidates(node, props):
            matches = self.backend.find_all(definition)
            if not matches:
                continue
            for index, match in enumerate(matches):
                if self.backend.identity(match) != target_id:
                    continue
                score = (len(matches), -len(definition))
                if best_score is None or score < best_score:
                    best_score = score
                    best = (definition, index)
                break

        return best

    # -- helpers -----------------------------------------------------------

    def _identifies(self, definition, node) -> bool:
        """The validation step: exactly one match, and it is the right object."""
        try:
            matches = self.backend.find_all(definition)
        except Exception:                                    # noqa: BLE001
            return False
        if len(matches) != 1:
            return False
        return self.backend.identity(matches[0]) == self.backend.identity(node)

    def _type_of(self, node) -> Optional[str]:
        node_type = getattr(node, "type", None)
        if isinstance(node_type, str):
            return node_type
        props = self.backend.properties(node)
        return _clean(props.get("type"))

    def _bare(self, props, node) -> dict:
        node_type = self._type_of(node)
        return {"type": node_type} if node_type else dict(props)

    def _label(self, props, node) -> str:
        for key in ("objectName", "id") + TEXT_PROPERTIES + STABLE_PROPERTIES:
            value = _clean(props.get(key))
            if value:
                return value
        return self._type_of(node) or "object"


def _weaken_to_moderate(robustness: Robustness) -> Robustness:
    """A scoped definition is never STRONG: it now depends on the tree shape too."""
    if robustness is Robustness.STRONG:
        return Robustness.MODERATE
    return robustness


def summarise(targets: Sequence[Target]) -> dict:
    """Counts by robustness — the headline number for a recording's health."""
    counts = {level.value: 0 for level in Robustness}
    for target in targets:
        counts[target.robustness.value] += 1
    return counts
