# -*- coding: utf-8 -*-
"""
Nameability audit — the headless half of the inspector.

Walks an application's object tree, resolves a definition for every object, and
reports which ones cannot be named durably. Run it *before* recording anything:
since you build the applications in-house, the cheapest fix for a fragile
recording is almost always adding an `objectName` in the source, not cleverness
in the recorder.

Works against any `Backend`, so it is exercised in tests against a synthetic tree
and in production against a live application.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from qat_recorder.ir import Robustness, Target
from qat_recorder.naming import NameResolver, is_secret_field

# ---------------------------------------------------------------------------
# Interactive versus internal
#
# A Qt object tree contains far more than the user interface: layouts, data
# models, item delegates, selection models, scrollbars, viewport widgets. None
# of them can be clicked, so grading them produces advice like "add objectName
# to 15 x QItemSelectionModel", which is nonsense — and worse, it buries the
# handful of real controls that genuinely need attention.
#
# Measured on a real application (qBittorrent 5.15): 502 objects, of which about
# 80 were interactive. Grading everything reported 47% fragile; grading only the
# interactive surface tells you something you can act on.
# ---------------------------------------------------------------------------

#: Classes whose names end this way are never user-interactive.
STRUCTURAL_SUFFIXES = ("Layout", "Model", "Delegate", "Validator", "Animation")

#: Specific classes that exist to arrange or decorate, not to be operated.
STRUCTURAL_CLASSES = frozenset({
    "QObject", "QWidget", "QFrame", "QScrollBar", "QSplitterHandle",
    "QWidgetLineControl", "QShortcut", "QActionGroup", "QToolBarSeparator",
    "QStackedWidget", "QScrollArea", "QSplitter", "QSizeGrip", "QSpacerItem",
    "QGraphicsScene", "QStyle", "QTimer", "QSettings", "QClipboard",
})

#: Qt names its own internal widgets with these prefixes.
STRUCTURAL_NAME_PREFIXES = ("qt_", "_q_")


def is_interactive(class_name: str, object_name: str = "") -> bool:
    """Whether a person could plausibly click, type into, or select this.

    Deliberately conservative about what it excludes: anything unrecognised is
    treated as interactive, because under-reporting a real control is worse than
    including one extra container.
    """
    name = (object_name or "").strip()
    if name.startswith(STRUCTURAL_NAME_PREFIXES):
        return False

    cls = (class_name or "").strip()
    if not cls or cls == "?":
        return False
    if cls in STRUCTURAL_CLASSES:
        return False
    if cls.endswith(STRUCTURAL_SUFFIXES):
        return False
    return True


@dataclass
class AuditEntry:
    label: str
    node_type: str
    path: str
    target: Target
    is_secret: bool = False
    #: objectName of the actual parent in the tree. Suggestions group on this
    #: rather than on whatever container the definition happened to need, so the
    #: advice points at where the source change belongs.
    container: str = ""
    #: Whether a person could operate this object. Internal machinery is
    #: scanned but not graded — see is_interactive().
    interactive: bool = True


@dataclass
class AuditReport:
    entries: list = field(default_factory=list)
    #: When True, internal objects are graded too. Off by default: see the note
    #: above is_interactive().
    include_internal: bool = False

    @property
    def graded(self) -> list:
        """The entries the verdict is based on."""
        if self.include_internal:
            return list(self.entries)
        return [entry for entry in self.entries if entry.interactive]

    @property
    def internal(self) -> list:
        return [entry for entry in self.entries if not entry.interactive]

    @property
    def counts(self) -> dict:
        counts = {level.value: 0 for level in Robustness}
        for entry in self.graded:
            counts[entry.target.robustness.value] += 1
        return counts

    def problems(self, threshold: Robustness = Robustness.WEAK) -> list:
        found = [
            entry for entry in self.graded
            if entry.target.robustness.rank >= threshold.rank
        ]
        return sorted(found, key=lambda e: -e.target.robustness.rank)

    def secrets(self) -> list:
        return [entry for entry in self.entries if entry.is_secret]

    def suggestions(self) -> list:
        """Actionable source-level fixes, most valuable first."""
        groups: dict = {}
        for entry in self.problems():
            groups.setdefault((entry.node_type, entry.container), []).append(entry)

        out = []
        for (node_type, container), items in sorted(
                groups.items(), key=lambda kv: -len(kv[1])):
            where = f" in '{container}'" if container else ""
            worst = min(items, key=lambda e: -e.target.robustness.rank)
            out.append(
                f"add objectName to {len(items)} x {node_type}{where} "
                f"(currently {worst.target.robustness.value}: "
                f"{', '.join(worst.target.warnings) or 'ambiguous'})"
            )
        return out

    def render(self, max_problems: int = 40) -> str:
        lines = []
        counts = self.counts
        graded = self.graded
        total = len(graded)
        skipped = len(self.internal)

        if self.include_internal:
            lines.append(f"Scanned {len(self.entries)} objects (all graded)")
        else:
            lines.append(
                f"Scanned {len(self.entries)} objects — grading the "
                f"{total} a person can operate")
            if skipped:
                lines.append(
                    f"  ({skipped} internal: layouts, models, scrollbars and "
                    "the like. --all to include them)")
        lines.append("")
        for level in Robustness:
            count = counts[level.value]
            if not count:
                continue
            share = 100.0 * count / total if total else 0.0
            lines.append(f"  {level.value:<11} {count:>5}  {share:5.1f}%")

        problems = self.problems()
        if problems:
            lines.append("")
            lines.append(f"Controls that will not replay durably ({len(problems)}):")
            for entry in problems[:max_problems]:
                lines.append(
                    f"  [{entry.target.robustness.value:<10}] "
                    f"{entry.node_type:<20} {entry.path or entry.label}")
                for warning in entry.target.warnings:
                    lines.append(f"               {warning}")
            if len(problems) > max_problems:
                lines.append(f"  ... and {len(problems) - max_problems} more")

        suggestions = self.suggestions()
        if suggestions:
            lines.append("")
            lines.append("Suggested source changes:")
            for suggestion in suggestions:
                lines.append(f"  - {suggestion}")

        secrets = self.secrets()
        if secrets:
            lines.append("")
            lines.append("Fields whose contents will be redacted when recorded:")
            for entry in secrets:
                lines.append(f"  - {entry.path or entry.label}")

        return "\n".join(lines)


def _path_of(backend, node, resolver) -> str:
    """Human-readable ancestry, outermost first."""
    parts = []
    current = node
    depth = 0
    while current is not None and depth < 12:
        props = backend.properties(current)
        label = (props.get("objectName") or props.get("id")
                 or props.get("text") or props.get("title"))
        node_type = getattr(current, "type", None) or props.get("type") or "?"
        parts.append(str(label) if label else f"<{node_type}>")
        current = backend.parent(current)
        depth += 1
    return " / ".join(reversed(parts))


def audit(backend, resolver: Optional[NameResolver] = None,
          limit: int = 5000, include_internal: bool = False) -> AuditReport:
    resolver = resolver or NameResolver(backend)
    report = AuditReport(include_internal=include_internal)

    if hasattr(backend, "walk"):
        nodes = backend.walk()
    else:
        nodes = _walk_live(backend, limit)

    for count, node in enumerate(nodes):
        if count >= limit:
            break
        props = backend.properties(node)
        target = resolver.resolve(node)
        parent = backend.parent(node)
        container = ""
        if parent is not None:
            parent_props = backend.properties(parent)
            container = str(parent_props.get("objectName")
                            or parent_props.get("id") or "")
        node_type = str(getattr(node, "type", None) or props.get("type") or "?")
        report.entries.append(AuditEntry(
            label=target.label,
            node_type=node_type,
            path=_path_of(backend, node, resolver),
            target=target,
            is_secret=is_secret_field(props),
            container=container,
            interactive=is_interactive(node_type, str(props.get("objectName", ""))),
        ))
    return report


def _walk_live(backend, limit: int):
    """Pre-order walk of a live application, siblings in order."""
    stack = list(reversed(list(backend.top_windows())))
    seen = set()
    count = 0
    while stack and count < limit:
        node = stack.pop()
        key = backend.identity(node)
        if key in seen:
            continue
        seen.add(key)
        count += 1
        yield node
        stack.extend(reversed(list(backend.children(node))))
