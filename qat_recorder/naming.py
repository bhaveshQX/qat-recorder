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

# ---------------------------------------------------------------------------
# Editable inputs
#
# On a label or a button, `text` is a caption: stable, and a reasonable (if
# translation-fragile) way to identify the object. On an editable input it is
# *content* -- whatever the user typed -- and using it as identity is circular.
#
# Observed on a real application: the recorder identified a search box as
# {"type": "LineEdit", "text": "bhavesh"} because that is what had just been
# typed into it. On replay the box was empty, so the definition matched nothing:
#
#   LookupError: Unable to find object: {"text":"bhavesh","type":"LineEdit"}
#
# `placeholderText` is *not* excluded -- that one is set by the developer and
# does not change as the user types, so it is a genuinely useful identifier.
# ---------------------------------------------------------------------------

#: Exact class names whose `text` holds user-entered content.
EDITABLE_CLASSES = frozenset({
    "QLineEdit", "QTextEdit", "QPlainTextEdit", "QSpinBox", "QDoubleSpinBox",
    "QComboBox", "QAbstractSpinBox", "QKeySequenceEdit",
    "TextInput", "TextEdit", "TextField", "TextArea",
})

#: Suffixes, so application-specific subclasses are caught too. qBittorrent's
#: search box is a QLineEdit subclass simply named `LineEdit`.
EDITABLE_SUFFIXES = ("LineEdit", "TextEdit", "SpinBox", "ComboBox",
                     "TextField", "TextArea", "TextInput")


def is_editable(class_name: str, properties: Mapping[str, Any]) -> bool:
    """Whether this object's `text` is something the user types into it."""
    name = (class_name or "").strip()
    if name in EDITABLE_CLASSES or name.endswith(EDITABLE_SUFFIXES):
        return True
    # Only editable inputs expose echoMode, so it is a reliable last resort for
    # a subclass named something unexpected.
    return "echoMode" in (properties or {})

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

    def __init__(self, backend, max_scope_depth: int = 4,
                 max_alternatives: int = 3):
        self.backend = backend
        self.max_scope_depth = max_scope_depth
        #: How many runners-up to keep. Each costs one lookup while recording
        #: and buys one more way for the step to survive a change later.
        self.max_alternatives = max_alternatives

    # -- public ------------------------------------------------------------

    def resolve(self, node) -> Target:
        """The best definition for this object -- and the runners-up.

        Every candidate that uniquely identifies the object is kept, not just
        the first. A step that depends on one selector fails the day that
        selector changes; a step carrying several tries the next one, and only
        fails when the application has changed enough that none of them fits.

        They are all validated here, against the live application, so the
        alternatives are known to work rather than merely plausible.
        """
        props = self.backend.properties(node)
        label = self._label(props, node)

        best = None
        alternatives: list = []
        for definition, strategy, robustness, warnings in self._candidates(node, props):
            if not self._identifies(definition, node):
                continue
            if best is None:
                best = (definition, strategy, robustness, tuple(warnings))
                continue
            if definition in alternatives or definition == best[0]:
                continue
            alternatives.append(definition)
            if len(alternatives) >= self.max_alternatives:
                break

        if best is not None:
            definition, strategy, robustness, warnings = best
            return Target(
                definition=definition,
                strategy=strategy,
                robustness=robustness,
                warnings=warnings,
                label=label,
                alternatives=tuple(alternatives),
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

        # On an editable input, `text` is what the user typed, not what the
        # object is. Identifying a field by its contents produces a definition
        # that only matches while those contents are present -- which, on
        # replay, they are not.
        editable = is_editable(node_type or "", props)

        for key in TEXT_PROPERTIES:
            if editable and key == "text":
                continue
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
        """A human-readable name, used for generated constants and step text.

        The same exclusion as `_bases`: on an editable input, `text` is what the
        user typed, so naming a constant after it produces things like
        `BHAVESH = {...}` for a search box — accurate about nothing and confusing
        to read six months later.
        """
        node_type = _clean(props.get("type")) or self._type_of(node)
        editable = is_editable(node_type or "", props)

        for key in ("objectName", "id") + TEXT_PROPERTIES + STABLE_PROPERTIES:
            if editable and key == "text":
                continue
            value = _clean(props.get(key))
            if value:
                return value
        return node_type or "object"


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


#: A class name so generic that scoping by it says nothing.
_ANONYMOUS_CONTAINERS = frozenset({"QWidget", "QFrame", "QObject"})


def reported_target(locator) -> Optional[Target]:
    """What the application said this object was, when nothing can be asked now.

    The resolver's whole method is to propose a definition and check it against
    the running application. That is right, and it has one blind spot: an object
    that is *gone* by the time the check runs. Clicking OK dismisses the dialog
    the OK button lives in, so the press is reported, the dialog tears itself
    down, and the lookup a moment later finds nothing. Whether it finds anything
    is a matter of milliseconds, which is exactly why the same button recorded
    sometimes and disappeared other times.

    Nothing about that means the object was not there. The native filter read
    its class, its text and its whole ancestor chain *inside the application*,
    at the instant the person clicked it -- that identity is not a guess, it is
    a first-hand report. What it has never been is *checked*, and it says so:
    Robustness.REPORTED, a warning that travels into the generated script, and a
    replay a few seconds later that settles it either way.

    Returned only when the report actually distinguishes something. A bare
    `{"type": "QCheckBox"}` names every check box in the dialog, and guessing
    there is the failure this project exists to avoid -- so that returns None
    and stays a gap.
    """
    class_name = (getattr(locator, "cls", "") or "").strip()
    object_name = (getattr(locator, "object_name", "") or "").strip()
    text = (getattr(locator, "text", "") or "").strip()
    if not class_name or (not object_name and not text):
        return None

    container = None
    for step_class, step_name in (getattr(locator, "path", ()) or ()):
        if step_name:
            container = {"objectName": step_name}
            break
        if step_class and step_class not in _ANONYMOUS_CONTAINERS:
            container = {"type": step_class}
            break

    candidates = []
    if object_name:
        candidates.append({"objectName": object_name, "type": class_name})
        candidates.append({"objectName": object_name})
    if text:
        # Scoped first: "the OK button" is in every dialog the application has,
        # and the one that matters is the one in the dialog that was open.
        if container:
            candidates.append({"type": class_name, "text": text,
                               "container": dict(container)})
        candidates.append({"type": class_name, "text": text})
    if object_name and container:
        candidates.append({"objectName": object_name,
                           "container": dict(container)})

    where = ""
    if container:
        where = container.get("objectName") or container.get("type") or ""
    return Target(
        definition=candidates[0],
        alternatives=tuple(candidates[1:]),
        strategy="reported by the application as the event happened",
        robustness=Robustness.REPORTED,
        label=(object_name or text) + (f" in {where}" if where else ""),
        warnings=(
            "the object had gone by the time it could be checked -- this is "
            "what the application said it was at the moment it was used, not a "
            "definition that was verified. The replay is what proves it.",))
