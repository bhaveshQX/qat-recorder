# -*- coding: utf-8 -*-
"""
The Action IR — the load-bearing abstraction of the recorder.

Recording produces a `Recording`; every code generator consumes one. Keeping the
capture side and the emit side separated by this structure is what allows the
capture backend to be swapped, object names to be re-resolved, and output to be
re-emitted as pytest or Gherkin, all without re-recording.

Everything here is pure data. No Qat import, no I/O beyond JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

SCHEMA_VERSION = 1

SECRET_KEY = "$secret"


class Robustness(str, Enum):
    """How much a generated object definition can be trusted to survive UI change."""

    STRONG = "strong"        # objectName — developer-controlled, stable
    MODERATE = "moderate"    # structural: type + container/parent chain
    WEAK = "weak"            # depends on visible text; breaks under translation
    FRAGILE = "fragile"      # positional index; breaks if siblings are reordered
    UNRESOLVED = "unresolved"  # no unique definition could be produced

    @property
    def rank(self) -> int:
        return _ROBUSTNESS_ORDER[self]


_ROBUSTNESS_ORDER = {
    Robustness.STRONG: 0,
    Robustness.MODERATE: 1,
    Robustness.WEAK: 2,
    Robustness.FRAGILE: 3,
    Robustness.UNRESOLVED: 4,
}


class ActionKind(str, Enum):
    """Semantic actions. Deliberately above raw events: a recorded script should
    say `check(rememberBox)`, not `mouse_click at (12, 7)`."""

    LAUNCH = "launch"
    CLOSE = "close"
    #: One window dismissed, not the application. Recorded when a window is
    #: closed from its title bar, which no click can express because the
    #: decoration belongs to the window manager rather than to Qt.
    CLOSE_WINDOW = "close_window"
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    CONTEXT_CLICK = "context_click"
    TYPE = "type"
    KEY = "key"
    SHORTCUT = "shortcut"
    SET_CHECKED = "set_checked"
    SELECT = "select"
    DRAG = "drag"
    WHEEL = "wheel"
    VERIFY_PROPERTY = "verify_property"
    WAIT_MISSING = "wait_missing"
    SCREENSHOT = "screenshot"
    CUSTOM_CODE = "custom_code"


def secret_ref(name: str) -> dict:
    """A placeholder standing in for a value that must never be written to disk.

    Emitted as a parameter lookup rather than a literal, so recorded scripts can
    be committed without leaking credentials.
    """
    return {SECRET_KEY: name}


def is_secret(value: Any) -> bool:
    return isinstance(value, Mapping) and SECRET_KEY in value


@dataclass(frozen=True)
class Target:
    """A resolved reference to an object in the application under test."""

    definition: Mapping[str, Any]
    strategy: str = "unknown"
    robustness: Robustness = Robustness.UNRESOLVED
    warnings: Sequence[str] = field(default_factory=tuple)
    label: str = ""
    #: Set only when `definition` alone is ambiguous and the object could be
    #: distinguished no other way. Qat definitions have no index selector, so this
    #: cannot live inside `definition`; emitters render it as
    #: `find_all_objects(definition)[index]`.
    index: Optional[int] = None
    #: For a row of a list, tree or table: the text it showed when it was
    #: clicked. Qat addresses items by row number, which breaks the moment a row
    #: is inserted above; generated code looks the text up first and uses the
    #: recorded row only as a fallback.
    item_text: str = ""
    #: Other definitions that also identified this object uniquely when it was
    #: recorded, best first. A step that carries only one selector fails the day
    #: that selector changes; one carrying several tries the next.
    alternatives: Sequence[Mapping[str, Any]] = field(default_factory=tuple)

    @property
    def candidates(self) -> list:
        """Every definition to try at replay, best first."""
        found = [dict(self.definition)]
        for other in self.alternatives:
            if dict(other) not in found:
                found.append(dict(other))
        return found

    def to_dict(self) -> dict:
        return {
            "definition": dict(self.definition),
            "strategy": self.strategy,
            "robustness": self.robustness.value,
            "warnings": list(self.warnings),
            "label": self.label,
            "index": self.index,
            "item_text": self.item_text,
            "alternatives": [dict(other) for other in self.alternatives],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Target":
        return cls(
            definition=dict(data["definition"]),
            strategy=data.get("strategy", "unknown"),
            robustness=Robustness(data.get("robustness", "unresolved")),
            warnings=tuple(data.get("warnings", ())),
            label=data.get("label", ""),
            index=data.get("index"),
            item_text=data.get("item_text", ""),
            alternatives=tuple(dict(other)
                               for other in data.get("alternatives", ())),
        )


@dataclass
class Action:
    """One semantic step in a recording."""

    kind: ActionKind
    target: Optional[Target] = None
    args: dict = field(default_factory=dict)
    t: float = 0.0
    note: str = ""

    def has_secret(self) -> bool:
        return any(is_secret(value) for value in self.args.values())

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "target": self.target.to_dict() if self.target else None,
            "args": self.args,
            "t": round(self.t, 4),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Action":
        target = data.get("target")
        return cls(
            kind=ActionKind(data["kind"]),
            target=Target.from_dict(target) if target else None,
            args=dict(data.get("args", {})),
            t=float(data.get("t", 0.0)),
            note=data.get("note", ""),
        )


@dataclass
class Drop:
    """An event that could not be turned into a step, and where that happened.

    Deliberately not an Action. An action is something the generated test will
    do; this is the exact opposite -- something the operator did that the test
    will *not* do -- and the two must not share a list. Keeping drops out of
    `actions` is what stops a thing nobody could identify from reaching the
    player, the object map, the Gherkin and the override block, none of which
    have anything true to say about it.

    What it carries instead is a position. `after` is the number of actions that
    had been recorded when the event was dropped, so the gap can be shown where
    it happened rather than counted at the end -- which is all the operator was
    ever given, minutes after the click, from the other side of a VM.
    """

    reason: str
    #: The raw event kind the filter sent: mouse_press, key_press, close_window.
    kind: str = ""
    #: What describe() said the event was delivered to. For a human to read.
    label: str = ""
    #: What the filter reported about the object -- class, objectName, text.
    #: NOT a Qat definition: nothing here was validated against the application,
    #: and calling it a definition is how an unusable locator ends up in a test.
    seen: dict = field(default_factory=dict)
    #: Index into `actions`: this many steps had been recorded before the drop.
    after: int = 0
    t: float = 0.0
    #: How many objects the reported definition matched when the event was lost.
    #: 0 or many is the usual answer -- it is generally *why* it was lost -- and
    #: it is the difference between a fix that can be offered and one that
    #: cannot. -1 means nobody looked.
    matched: int = -1
    #: A Target, resolved against the running application at the moment of the
    #: drop, when the reported definition turned out to identify exactly one
    #: object. Empty otherwise. This is what makes a one-click fix honest after
    #: the session has ended and the application is gone: it was checked when it
    #: could be checked.
    suggestion: dict = field(default_factory=dict)
    #: Everything known about the object while the application was still there:
    #: what the filter reported, which sibling it was, every object of the same
    #: class with a resolved Target where one exists, and the text next to each.
    #: Gathered once, at the moment of the drop, because none of it can be got
    #: back afterwards -- see evidence.py.
    evidence: dict = field(default_factory=dict)
    #: File name of the still taken when the event was lost. The reason says why
    #: the recorder failed; this says what the operator was looking at, which is
    #: the part nobody can reconstruct afterwards.
    shot: str = ""
    #: Set once the operator has filled the gap. A repaired drop is history: it
    #: is kept so the recording still says what was lost, and emits nothing.
    repaired: bool = False

    def to_dict(self) -> dict:
        return {
            "reason": self.reason,
            "kind": self.kind,
            "label": self.label,
            "seen": dict(self.seen),
            "after": self.after,
            "t": round(self.t, 4),
            "matched": self.matched,
            "suggestion": dict(self.suggestion),
            "evidence": dict(self.evidence),
            "shot": self.shot,
            "repaired": self.repaired,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Drop":
        return cls(
            reason=data.get("reason", ""),
            kind=data.get("kind", ""),
            label=data.get("label", ""),
            seen=dict(data.get("seen", {})),
            after=int(data.get("after", 0)),
            t=float(data.get("t", 0.0)),
            matched=int(data.get("matched", -1)),
            suggestion=dict(data.get("suggestion", {})),
            evidence=dict(data.get("evidence", {})),
            shot=data.get("shot", ""),
            repaired=bool(data.get("repaired", False)),
        )


@dataclass
class Recording:
    """A complete capture session."""

    app: str
    actions: list = field(default_factory=list)
    #: Events that could not be recorded, each holding the position it happened
    #: at. Parallel to `actions`, never inside it -- see Drop.
    drops: list = field(default_factory=list)
    started_at: str = ""
    meta: dict = field(default_factory=dict)
    schema: int = SCHEMA_VERSION

    def __post_init__(self):
        if not self.started_at:
            self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # -- mutation -----------------------------------------------------------

    def add(self, action: Action) -> Action:
        self.actions.append(action)
        return action

    def add_drop(self, drop: Drop) -> Drop:
        """Record an event that could not become a step, where it happened."""
        self.drops.append(drop)
        return drop

    def insert_at(self, index: int, action: Action) -> Action:
        """Put a step at `index`, and move every drop after it along.

        A drop's position is an index into `actions`, so inserting one shifts
        the gaps that come after it. Doing that here, in the one place that can
        see both lists, is what stops a repaired script from showing its
        remaining gaps a step too early.
        """
        index = max(0, min(index, len(self.actions)))
        self.actions.insert(index, action)
        for drop in self.drops:
            if drop.after >= index:
                drop.after += 1
        return action

    def repair(self, position: int, action: Action) -> Optional[Drop]:
        """Fill the gap at `position` with a step, and mark it repaired.

        `position` indexes `drops`. The step goes exactly where the dropped
        event was, so the test does what the operator did, in the order they
        did it.
        """
        if not 0 <= position < len(self.drops):
            raise IndexError(f"no drop at {position}")
        drop = self.drops[position]
        if drop.repaired:
            raise ValueError(f"the gap at {position} has already been filled")
        at = drop.after
        self.insert_at(at, action)
        drop.after = at
        drop.repaired = True
        return drop

    def open_drops(self) -> list:
        """The gaps nobody has filled yet."""
        return [drop for drop in self.drops if not drop.repaired]

    # -- analysis -----------------------------------------------------------

    def weakest_targets(self, threshold: Robustness = Robustness.WEAK) -> list:
        """Targets at or below `threshold`, worst first.

        This is what makes recorded scripts reviewable: the operator sees which
        steps are held together with string before committing them.
        """
        found = [
            action for action in self.actions
            if action.target is not None
            and action.target.robustness.rank >= threshold.rank
        ]
        return sorted(found, key=lambda a: -a.target.robustness.rank)

    def secrets(self) -> list:
        return [action for action in self.actions if action.has_secret()]

    def validate(self) -> list:
        """Return a list of problems. Empty means the recording is coherent."""
        problems = []
        if self.schema != SCHEMA_VERSION:
            problems.append(
                f"schema {self.schema} != supported {SCHEMA_VERSION}")
        for index, action in enumerate(self.actions):
            if not isinstance(action.kind, ActionKind):
                problems.append(f"action {index}: unknown kind {action.kind!r}")
            # CUSTOM_CODE carries the call itself, so there is nothing for a
            # target to identify -- the same reason launch, close and screenshot
            # are exempt.
            needs_target = action.kind not in (
                ActionKind.LAUNCH, ActionKind.CLOSE, ActionKind.SCREENSHOT,
                ActionKind.CUSTOM_CODE)
            if needs_target and action.target is None:
                problems.append(f"action {index}: {action.kind.value} has no target")
            if action.target is not None and not action.target.definition:
                problems.append(f"action {index}: target has an empty definition")
        return problems

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "app": self.app,
            "started_at": self.started_at,
            "meta": self.meta,
            "actions": [action.to_dict() for action in self.actions],
            "drops": [drop.to_dict() for drop in self.drops],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Recording":
        schema = int(data.get("schema", 0))
        if schema != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported recording schema {schema}; this build reads {SCHEMA_VERSION}")
        return cls(
            app=data["app"],
            actions=[Action.from_dict(item) for item in data.get("actions", [])],
            # Absent from every recording written before drops were located, and
            # an empty list is exactly right for those: nothing was known to be
            # missing because nothing was looking.
            drops=[Drop.from_dict(item) for item in data.get("drops", [])],
            started_at=data.get("started_at", ""),
            meta=dict(data.get("meta", {})),
            schema=schema,
        )

    def dumps(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def loads(cls, text: str) -> "Recording":
        return cls.from_dict(json.loads(text))
