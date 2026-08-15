# -*- coding: utf-8 -*-
"""
Folding raw events into semantic actions.

A recorded script should read `click(loginButton)` and `type_in(usernameField,
"alice")`, not `mouse_press at (51, 51)`. This module does that folding, and
resolves each event's structural locator into a *validated* Qat definition using
the Phase 1 resolver.

Three things here are less obvious than they look.

**Events arrive more than once.** Qt delivers a click to the window and then to
the widget, so the same physical interaction produces several records at
different levels of the hierarchy. They are grouped by timestamp and the most
specific target wins.

**Typed text is never taken from the keystrokes.** The native filter does not
transmit characters at all. Instead a run of key presses marks the field as
edited, and the resulting value is read back from the widget through Qat when the
run ends. That is more correct than replaying keystrokes — it survives
backspaces, selection replacement, paste and IME composition — and it means the
characters only ever exist in one place, where `echoMode` is known and password
fields can be redacted before anything reaches disk.

**A press and a release are one click.** Recording both would produce scripts that
are twice as long and no more faithful.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from qat_recorder.events import Locator, RawEvent
from qat_recorder.ir import Action, ActionKind, Recording, Robustness, Target, secret_ref
from qat_recorder.items import (
    combo_owner, discover_row, is_view, item_definition, item_text_of,
    selection_property,
)
from qat_recorder.menus import NOT_FOUND, resolve_menu_item, strip_mnemonic
from qat_recorder.naming import NameResolver, is_editable, is_secret_field

# Qt::Key values for keys that do not produce text.
KEY_NAMES = {
    0x01000000: "Escape",
    0x01000001: "Tab",
    0x01000002: "Backtab",
    0x01000003: "Backspace",
    0x01000004: "Return",
    0x01000005: "Enter",
    0x01000006: "Insert",
    0x01000007: "Delete",
    0x01000010: "Home",
    0x01000011: "End",
    0x01000012: "Left",
    0x01000013: "Up",
    0x01000014: "Right",
    0x01000015: "Down",
    0x01000016: "PageUp",
    0x01000017: "PageDown",
}
for _i in range(12):
    KEY_NAMES[0x01000030 + _i] = f"F{_i + 1}"

#: Pressing Shift alone is not an action.
MODIFIER_KEYS = frozenset({0x01000020, 0x01000021, 0x01000022, 0x01000023})

MOD_SHIFT = 0x02000000
MOD_CONTROL = 0x04000000
MOD_ALT = 0x08000000
MOD_META = 0x10000000

#: Modifiers that turn a keystroke into a shortcut rather than typing.
COMMAND_MODIFIERS = MOD_CONTROL | MOD_ALT | MOD_META

BUTTON_NAMES = {1: "left", 2: "right", 4: "middle"}

#: Menus are operated by pressing on the bar and releasing on an item, which
#: looks exactly like a drag and is nothing like one.
MENU_HINTS = ("Menu", "Action")


def is_menu_interaction(press_class: str, release_class: str) -> bool:
    """Whether a press/release across two widgets is a menu being used."""
    return any(hint in (press_class or "") or hint in (release_class or "")
               for hint in MENU_HINTS)


def is_menu_bar(class_name: str) -> bool:
    """Whether clicking an item of this opens a menu rather than doing something."""
    return (class_name or "").strip().endswith("MenuBar")


def describe(locator: Locator) -> str:
    """A short human description of what an event was delivered to.

    Used in the report of what could not be recorded. "21 unresolved" with no
    explanation is a mystery rather than a diagnosis, and it is the operator --
    who knows what they clicked -- who can tell which missing step mattered.
    """
    if locator.menu_item:
        return (f"menu item {strip_mnemonic(locator.menu_item)!r} in "
                f"{locator.object_name or locator.cls or 'a menu'}")
    name = locator.object_name or locator.text or locator.title or "<unnamed>"
    where = locator.nearest_named_ancestor()
    return (f"{locator.cls or '?'} {name!r}"
            + (f" in {where}" if where else ""))


# ---------------------------------------------------------------------------
# Qt's own widgets
#
# A Qt widget is built from other widgets. A tree view owns a viewport, two
# scrollbars and a container for each; a tab widget owns a stacked widget; a
# spin box owns a line edit. Qt names them itself, with a `qt_` prefix, and a
# person never clicks one on purpose -- they click *through* it, at the control
# that owns it.
#
# Recording them as targets is how a real session produced
# `mouse_click({"objectName": "qt_scrollarea_vcontainer", ...})`, which failed on
# replay because Qt only creates that container while a scrollbar is needed and
# only shows it while one is shown. The audit has classified these as internal
# since Phase 1; capture simply never used what the audit knew.
#
# They divide in two, and the halves want opposite treatment:
#
#   surface   the widget you click *through* -- a viewport, a stacked page, the
#             line edit inside a spin box. The event belongs to the control that
#             owns it, so the target is promoted to that owner.
#   chrome    scrollbars, their containers, overflow buttons. Operating one
#             scrolls; it is not a step in a test, and the widget itself comes
#             and goes with the content. Dropped.
# ---------------------------------------------------------------------------

INTERNAL_PREFIXES = ("qt_", "_q_")

CHROME_CLASSES = frozenset({"QScrollBar", "QSizeGrip", "QSplitterHandle"})

CHROME_MARKERS = ("vcontainer", "hcontainer", "scrollbar", "_ext_button")


def is_internal(object_name: str) -> bool:
    """Whether Qt named this widget rather than the application."""
    return (object_name or "").strip().startswith(INTERNAL_PREFIXES)


def is_chrome(class_name: str, object_name: str) -> bool:
    """Whether operating this scrolls or resizes rather than doing something."""
    if (class_name or "").strip() in CHROME_CLASSES:
        return True
    name = (object_name or "").strip().lower()
    return is_internal(name) and any(mark in name for mark in CHROME_MARKERS)


def owner_of(locator: Locator) -> Optional[Locator]:
    """The nearest ancestor that belongs to the application, not to Qt.

    Built from the ancestor chain the native filter already reports, so it costs
    no round trip and cannot disagree with what the event actually hit.
    """
    for index, (cls, object_name) in enumerate(locator.path):
        if is_internal(object_name) or is_chrome(cls, object_name):
            continue
        return Locator(cls=cls, object_name=object_name,
                       path=locator.path[index + 1:])
    return None


NOT_ANSWERING = (
    "the application stopped answering Qat. Almost always a native dialog: a "
    "GTK or desktop-portal file picker is not a Qt widget, it is modal, and it "
    "runs its own event loop, so nothing can be recorded until it closes. The "
    "launcher asks Qt for its own dialogs instead (QT_QPA_PLATFORMTHEME); if "
    "this appears, that application is opening one some other way")


def explain(error: Exception) -> str:
    """Turn an exception raised during folding into something actionable."""
    text = str(error) or error.__class__.__name__
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered or \
            "not running" in lowered or "disconnected" in lowered:
        return f"{NOT_ANSWERING} [{text}]"
    return text


#: Why an event could not be turned into a step.
NOT_FINDABLE = ("could not be found through Qat while it was on screen -- "
                "usually hidden, on another tab, or already destroyed")

CHROME = ("a scrollbar or window furniture; scrolling is not a step, and the "
          "widget only exists while the content needs it")

SCROLLING = ("scrolling is navigation, not a step: replaying a scroll of so many "
             "degrees depends on how much content happens to be there, and the "
             "step after it finds its target by name anyway")

DRAGGING = ("a drag of so many pixels across this widget has no durable "
            "meaning; what it produced -- a value, a selection -- is what a "
            "test should assert, and this widget exposes no value to read")

NO_ITEM = ("the row could not be addressed through its view; clicking the view "
           "instead would click whichever row happened to be there")

NO_COMBO = ("chosen from a combo box whose own name could not be resolved, so "
            "there is nothing durable to set the value on")

ITEM_WARNING = ("a row of a view: found by its text on replay, falling back to "
                "the recorded position")

ITEM_POSITIONAL = ("a row of a view that shows no text anywhere Qat can read "
                   "it, so this step can only be its position: it will click "
                   "the wrong row if the list is ever reordered")


# ---------------------------------------------------------------------------
# Widgets whose value a wheel or a drag changes
#
# Scrolling over a list scrolls it. Scrolling over a spin box changes a number,
# and dragging a slider moves it -- those are real changes, and the honest way
# to record one is the same trick that records typing: do not replay the input,
# read what it produced and set that. A recorded step then says "set this to
# 40" instead of "turn the wheel three notches here", which is what the person
# meant and is the only version that survives different content, a different
# window size, or a different day.
# ---------------------------------------------------------------------------

VALUE_PROPERTIES = (
    ("QComboBox", "currentText"),
    ("ComboBox", "currentText"),
    ("QFontComboBox", "currentText"),
    ("QSpinBox", "value"),
    ("QDoubleSpinBox", "value"),
    ("SpinBox", "value"),
    ("QSlider", "value"),
    ("QDial", "value"),
    ("QProgressBar", "value"),
    ("QAbstractSlider", "value"),
    ("Slider", "value"),
    ("QDateTimeEdit", "dateTime"),
    ("QDateEdit", "date"),
    ("QTimeEdit", "time"),
)


def value_property(class_name: str) -> str:
    """The property a wheel or drag changes on this widget, if any."""
    name = (class_name or "").strip()
    for candidate, prop in VALUE_PROPERTIES:
        if name == candidate or name.endswith(candidate):
            return prop
    return ""

NO_OWNER = ("one of Qt's own internal widgets, with no application widget "
            "above it to attribute the click to")
NOT_UNIQUE = ("no definition identifies it uniquely, not even by position; "
              "it needs an objectName in the application")


def dropped_report(failures: Iterable[str]) -> str:
    """The contents of unresolved.txt.

    Written wherever a recording is saved -- locally, from the panel, or fetched
    back from an agent -- because the count on its own is a mystery, and a
    dropped step is something the operator did that the test will not do.
    """
    counted = Counter(failures)
    if not counted:
        return "Every event was recorded.\n"
    return ("Events that could not be turned into steps.\n\n"
            "Each of these is something you did that the generated test will "
            "not do. If one of them mattered -- closing a dialog, for instance "
            "-- the replay diverges from your session at that point.\n\n"
            + "\n".join(f"{count:>4} x  {reason}"
                        for reason, count in counted.most_common()) + "\n")


# `is_editable` lives in naming.py, because the resolver needs the same
# knowledge for a different reason: this module uses it to decide whether typed
# characters end up in `text`, and the resolver uses it to decide whether `text`
# may be used to identify the object at all. One definition, two consequences.


def modifier_names(modifiers: int) -> list:
    names = []
    if modifiers & MOD_CONTROL:
        names.append("Ctrl")
    if modifiers & MOD_ALT:
        names.append("Alt")
    if modifiers & MOD_META:
        names.append("Meta")
    if modifiers & MOD_SHIFT:
        names.append("Shift")
    return names


def key_name(key: int) -> str:
    if key in KEY_NAMES:
        return KEY_NAMES[key]
    if 0x20 <= key < 0x7F:
        return chr(key)
    return f"0x{key:08x}"


def is_text_key(key: int) -> bool:
    """True for keys that contribute characters to a field."""
    return key not in MODIFIER_KEYS and key < 0x01000000


def find_by_locator(backend, locator: Locator):
    """Resolve a structural locator to a live object.

    Escalates the same way the name resolver does, but starting from what the
    event filter observed rather than from a node we already hold.
    """
    if locator.object_name:
        matches = backend.find_all({"objectName": locator.object_name})
        if len(matches) == 1:
            return matches[0]
        if matches and locator.cls:
            typed = backend.find_all(
                {"objectName": locator.object_name, "type": locator.cls})
            if len(typed) == 1:
                return typed[0]

    if not locator.cls:
        return None

    base = {"type": locator.cls}
    anchor = locator.nearest_named_ancestor()

    for prop, value in (("text", locator.text), ("title", locator.title)):
        if not value:
            continue
        candidate = dict(base)
        candidate[prop] = value
        matches = backend.find_all(candidate)
        if len(matches) == 1:
            return matches[0]
        if anchor:
            scoped = dict(candidate)
            scoped["container"] = {"objectName": anchor}
            scoped_matches = backend.find_all(scoped)
            if len(scoped_matches) == 1:
                return scoped_matches[0]
            if 0 <= locator.index < len(scoped_matches):
                return scoped_matches[locator.index]

    candidate = dict(base)
    if anchor:
        candidate["container"] = {"objectName": anchor}
    matches = backend.find_all(candidate)
    if len(matches) == 1:
        return matches[0]
    if 0 <= locator.index < len(matches):
        return matches[locator.index]
    return None


@dataclass
class _Pending:
    """A press waiting for its release."""
    event: RawEvent
    target: Target
    node: Any
    #: Anything the resulting step should admit about itself, decided when the
    #: press was resolved rather than when the release arrives.
    note: str = ""


class CaptureSession:
    """Turns a stream of RawEvents into a Recording."""

    def __init__(self, backend, app_name: str = "app",
                 resolver: Optional[NameResolver] = None,
                 group_ms: int = 8, click_ms: int = 1200,
                 app_path: str = ""):
        self.backend = backend
        self.resolver = resolver or NameResolver(backend)
        self.recording = Recording(app=app_name)
        # The executable, kept so generated tests can register the application
        # themselves. `qat.start_application()` takes a registered NAME, not a
        # path, so a generated test that only knows the name fails with
        # "Application '...' is not defined in configuration file".
        if app_path:
            self.recording.meta["app_path"] = str(app_path)
        self.group_ms = group_ms
        self.click_ms = click_ms

        self._t0: Optional[int] = None
        self._group: list = []
        self._pending: Optional[_Pending] = None
        #: When a menu item was last clicked, and which one, so the release that
        #: Qt redirects to the popup can be told apart from a real second click.
        self._menu_press_t: Optional[int] = None
        self._menu_definition: Optional[dict] = None
        #: The same, for a row of a view: the press is the step, its release is
        #: not a second one.
        self._item_press_t: Optional[int] = None
        #: Views already reported as having unreadable row labels, so a session
        #: that clicks forty rows of one table says so once.
        self._unlabelled_views: set = set()
        self._typing_node: Any = None
        self._typing_target: Optional[Target] = None
        self._typing_t: int = 0
        #: A wheel or drag changing a widget's value, read when the run ends.
        self._value_node: Any = None
        self._value_target: Optional[Target] = None
        self._value_property: str = ""
        self._value_t: int = 0
        #: Whether that pending value is a view's selection, which becomes a
        #: click on a row rather than a property assignment.
        self._value_is_selection: bool = False
        self._resolved_cache: dict = {}
        self._last_reason: str = ""
        self._last_note: str = ""
        self.unresolved = 0
        #: Human-readable reasons individual events were dropped, so the count
        #: is explicable rather than mysterious.
        self.failures: list = []

        self.recording.add(Action(ActionKind.LAUNCH, args={"app": app_name}, t=0.0))

    # -- public ------------------------------------------------------------

    def feed(self, event: RawEvent) -> None:
        if self._t0 is None:
            self._t0 = event.t
        if self._group and not self._same_interaction(self._group[0], event):
            self._flush_group()
        self._group.append(event)

    def feed_all(self, events: Iterable[RawEvent]) -> None:
        """Fold a batch of events, surviving individual failures.

        A recording is minutes of a person's time. One event that cannot be
        resolved -- because the object was destroyed, or is on a tab that is no
        longer shown -- must cost that one step, not the whole session. An
        earlier version let the exception escape and discarded 124 captured
        events along with it.
        """
        for event in events:
            try:
                self.feed(event)
            except Exception as error:                       # noqa: BLE001
                self.unresolved += 1
                self.failures.append(
                    f"{event.kind} on {describe(event.target)}: "
                    f"{explain(error)}")

    def flush_stale(self, now_ms: Optional[int] = None,
                    max_age_ms: int = 150) -> bool:
        """Emit a buffered interaction once no more of it can arrive.

        Grouping waits for the *next* event to know an interaction is complete,
        which is fine for batch processing but means a live panel shows nothing
        until the operator does something else. Calling this on a timer closes
        the group on age instead. Returns True if anything was flushed.

        Typing runs are deliberately not flushed here: the value is read from the
        widget when the run ends, and ending it early would capture a half-typed
        string.
        """
        if not self._group:
            return False
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        if now_ms - self._group[-1].t < max_age_ms:
            return False
        self._flush_group()
        return True

    def finish(self) -> Recording:
        self._flush_group()
        self._flush_input()
        self._pending = None
        return self.recording

    def resolve_locator(self, locator: Locator):
        """Resolve a locator to (node, Target), or None. Cached.

        Public because the control panel needs it to turn a checkpoint click
        into a target without re-implementing the escalation.
        """
        return self._resolve(locator)

    def properties_of(self, node) -> dict:
        """Best-effort property snapshot; never raises."""
        return self._properties_of(node)

    def add_verification(self, node, property_name: str, expected: Any,
                         when: Optional[int] = None) -> Action:
        """Operator-inserted checkpoint."""
        target = self._target_for_node(node)
        return self.recording.add(Action(
            ActionKind.VERIFY_PROPERTY,
            target=target,
            args={"property": property_name, "expected": expected},
            t=self._elapsed(when) if when else self._last_time(),
        ))

    # -- grouping ----------------------------------------------------------

    def _same_interaction(self, first: RawEvent, other: RawEvent) -> bool:
        return (first.kind == other.kind
                and first.button == other.button
                and abs(other.t - first.t) <= self.group_ms)

    def _flush_group(self) -> None:
        if not self._group:
            return
        group, self._group = self._group, []
        # Qt delivers the same interaction to the window and then to the widget.
        # Keep the most specific recipient.
        best = max(group, key=lambda event: event.target.specificity())
        self._handle(best)

    # -- handling ----------------------------------------------------------

    def _handle(self, event: RawEvent) -> None:
        if event.kind in ("mouse_press", "mouse_release", "mouse_double"):
            self._handle_mouse(event)
        elif event.kind == "key_press":
            self._handle_key(event)
        elif event.kind == "shortcut":
            self._flush_input()
            resolved = self._resolve(event.target)
            if resolved is None:
                self._drop(event)
                return
            node, target = resolved
            # Qt delivers QEvent::Shortcut to whatever owns the shortcut, which
            # is usually a QAction -- and a QAction is not a widget, so replaying
            # against it fails with "Associated widget was not found".
            #
            # Retarget to the NEAREST named ancestor, taken from the path the
            # native filter already reports. Walking to the outermost ancestor
            # instead lands on a QWidgetWindow, which is a QWindow rather than a
            # QWidget and fails in exactly the same way.
            self.recording.add(Action(
                ActionKind.SHORTCUT,
                target=self._owning_widget_target(event.target, node, target),
                args={"keys": event.keys}, t=self._elapsed(event.t)))
        elif event.kind == "wheel":
            resolved = self._resolve(event.target)
            if resolved is None:
                self._flush_input()
                self._drop(event)
                return
            node, target = resolved
            if not self._begin_value_change(event, node, target):
                # Scrolling something scrollable. Navigation, not a step.
                self._flush_input()
                self._drop(event, SCROLLING)
        # key_release and focus_in carry no action of their own.

    def _handle_mouse(self, event: RawEvent) -> None:
        # Menus first: the object a menu click is delivered to is never the
        # thing the person clicked.
        if self._handle_menu(event):
            return
        if self._handle_item(event):
            return

        resolved = self._resolve(event.target)
        if resolved is None:
            self._drop(event)
            return
        node, target = resolved

        if event.kind == "mouse_double":
            self._flush_input()
            self._pending = None
            self.recording.add(Action(
                ActionKind.DOUBLE_CLICK, target=target,
                args=self._button_args(event), t=self._elapsed(event.t),
                note=self._last_note))
            return

        if event.kind == "mouse_press":
            self._flush_input()
            self._pending = _Pending(event, target, node, self._last_note)
            return

        # mouse_release: pair it with the press if they belong together
        pending = self._pending
        self._pending = None
        if (pending is not None
                and pending.target.definition == target.definition
                and event.t - pending.event.t <= self.click_ms):
            kind = (ActionKind.CONTEXT_CLICK if event.button == 2
                    else ActionKind.CLICK)
            self.recording.add(Action(
                kind, target=target, args=self._button_args(pending.event),
                t=self._elapsed(pending.event.t), note=pending.note))
            return

        # Press and release on different widgets. Usually a drag -- but a menu
        # is the common exception, and getting it wrong makes the recording
        # unreplayable.
        if pending is not None and is_menu_interaction(
                pending.event.target.cls, event.target.cls):
            # Opening a menu is press-on-the-bar, move, release-on-the-item.
            # That is two clicks, not a drag: replaying a drag across a menu bar
            # opens nothing, so the item is never visible and the next step
            # fails with "Unable to find object". Observed on a real
            # application.
            self.recording.add(Action(
                ActionKind.CLICK, target=pending.target,
                args=self._button_args(pending.event),
                t=self._elapsed(pending.event.t),
                note="opens the menu"))
            self.recording.add(Action(
                ActionKind.CLICK, target=target,
                args=self._button_args(event), t=self._elapsed(event.t)))
            return

        # A release with no matching press: most likely a drag. Recorded as the
        # value it produced where the widget has one -- dragging a slider sets a
        # number, and "set it to 40" replays where "drag 63 pixels right" does
        # not. Everywhere else there is nothing durable to record.
        if pending is not None:
            if self._begin_value_change(pending.event, pending.node,
                                        pending.target):
                self._flush_value()
                return
            self._drop(pending.event, DRAGGING)

    # -- items in views ----------------------------------------------------

    def _handle_item(self, event: RawEvent) -> bool:
        """Fold a click inside a list, tree or table into a click on the row.

        Returns True when the event has been dealt with here.

        A combo box is handled differently and deliberately: choosing from one
        is recorded as the value chosen, not as two clicks on a popup that only
        exists while it is open.
        """
        if event.kind == "mouse_release":
            # The press already produced the step; its release is not a second
            # one. Anything else pairs normally.
            if self._item_press_t is None or \
                    event.t - self._item_press_t > self.click_ms:
                return False
            # If the press left a selection to read, read it now rather than at
            # the next action. The widget has had the press by now, so its
            # `currentRow` is the row just clicked -- waiting any longer risks
            # reading the row of the *next* click instead of this one.
            self._flush_value()
            return True
        if event.kind not in ("mouse_press", "mouse_double"):
            return False

        # Combo boxes first, and deliberately before the `is_item` gate. Which
        # row of the popup was clicked comes from the event filter, and a filter
        # built before that existed does not send it -- but a combo popup is
        # recognisable from the class chain alone, and its value can be read
        # afterwards. This is the case that has broken replays repeatedly, so it
        # does not get to depend on which build of the filter is installed.
        combo = combo_owner(event.target)
        if combo:
            self._item_press_t = None
            return self._handle_combo(event, combo)

        found_view = (self._view_target(event)
                      if self._looks_like_a_view(event) else None)
        if found_view is None:
            return False              # not a view; ordinary handling
        node, view = found_view

        # Three routes to what was clicked, best first. A click on a view is
        # never recorded as a click on the view: that selects whichever row
        # happens to be in the middle, which is how a click on a tab list opened
        # the wrong tab and made the next step fail on a widget that was never
        # shown. If none of the three works, the step is dropped and said so.
        row, column, text = (event.target.item_row, event.target.item_column,
                             event.target.item_text)
        if row < 0:
            # 2. Ask Qat, which wraps every item in a virtual widget with real
            #    geometry -- it has to, or it could not click one.
            found = discover_row(self.backend, view.definition,
                                 event.x, event.y)
            if found is not None:
                row, column, text = found[0], 0, found[1]

        if row < 0:
            # 3. Ask the view what ended up selected, and record that. Not a
            #    click, so it comes last, but it survives anything.
            if self._record_selection(event, node, view):
                return True
            self._drop(event, self._why_no_item(node, view))
            return True

        self._item_press_t = None
        definition = item_definition(view.definition, row, column)
        matches = self.backend.find_all(definition)
        if len(matches) != 1:
            # Qat cannot address this item, so the honest step is none at all;
            # clicking the view instead would click a different row.
            self._drop(event, NO_ITEM)
            return True
        if not text:
            # The filter said which row but not what it says. Ask Qat, which can
            # reach the model even where no property exposes the label.
            text = item_text_of(self.backend, matches[0])
        if not text:
            self._note_unlabelled_row(definition, matches[0])

        target = self._item_target(definition, text, row)
        self._flush_input()
        self._pending = None
        self._item_press_t = event.t
        kind = (ActionKind.DOUBLE_CLICK if event.kind == "mouse_double"
                else ActionKind.CONTEXT_CLICK if event.button == 2
                else ActionKind.CLICK)
        self.recording.add(Action(
            kind, target=target, args=self._button_args(event),
            t=self._elapsed(event.t)))
        return True

    def _handle_combo(self, event: RawEvent, combo_name: str) -> bool:
        """Choosing from a combo box: record the value, not the two clicks."""
        matches = self.backend.find_all({"objectName": combo_name})
        if len(matches) != 1:
            self._drop(event, NO_COMBO)
            return True
        node = matches[0]
        target = self.resolver.resolve(node)
        if target.robustness is Robustness.UNRESOLVED:
            self._drop(event, NO_COMBO)
            return True

        self._flush_input()
        self._pending = None
        self._item_press_t = event.t
        # The click that opened the popup is already in the recording, and
        # setting the value does that job as well. Left in, replay would open
        # the popup and leave it open over the following step.
        self._drop_click_on(target)

        if event.target.item_text:
            self.recording.add(Action(
                ActionKind.SELECT, target=target,
                args={"property": "currentText",
                      "value": event.target.item_text},
                t=self._elapsed(event.t)))
            return True

        # The filter did not say which row was clicked, so read the answer from
        # the combo box instead -- once the popup has closed and the choice has
        # taken effect, which is what deferring to the next flush achieves.
        self._begin_value_change(event, node, target, prop="currentText")
        return True

    def _drop_click_on(self, target: Target) -> None:
        """Remove a just-recorded click on this object, if that is the last step."""
        actions = self.recording.actions
        if not actions:
            return
        last = actions[-1]
        if (last.kind is ActionKind.CLICK and last.target is not None
                and last.target.definition == target.definition):
            actions.pop()

    def _why_no_item(self, node, view: Target) -> str:
        """Say what this view actually offered, not merely that it offered too
        little.

        A dropped step that says "could not address the row" sends someone back
        to the tool's author with a screenshot. One that says which properties
        the view and its rows do expose answers the question in the report
        itself -- Qt is large, applications subclass everything in it, and the
        next view that defeats all three routes should not cost a round trip to
        find out why.
        """
        details = []
        try:
            on_view = sorted(self.backend.properties(node).keys())
            details.append(f"the view exposes {', '.join(on_view) or 'nothing'}")
        except Exception:                                    # noqa: BLE001
            details.append("the view's properties could not be read")

        try:
            rows = self.backend.find_all(item_definition(view.definition, 0))
            if not rows:
                details.append("Qat reports no rows in it at all")
            else:
                on_row = sorted(self.backend.properties(rows[0]).keys())
                details.append(
                    f"row 0 exposes {', '.join(on_row) or 'nothing'}")
        except Exception as error:                           # noqa: BLE001
            details.append(f"its rows could not be listed ({error})")

        return f"{NO_ITEM}. For whoever fixes this: {'; '.join(details)}"

    def _record_selection(self, event: RawEvent, node, view: Target) -> bool:
        """Record what the view says is selected, rather than a click.

        Read when the run ends, like typing and like a combo box: a click
        selects, and the selection is only settled once it has happened.
        """
        try:
            properties = self.backend.properties(
                node, keys=("currentRow", "currentIndex"))
        except TypeError:            # a backend that predates the keys argument
            properties = self.backend.properties(node)
        except Exception:                                    # noqa: BLE001
            return False

        prop = selection_property(properties)
        if not prop:
            return False
        # Each click on a list is its own step. Unlike a wheel, which arrives as
        # a burst and means one change, five clicks down a sidebar are five
        # pages -- so anything still pending is finished before this one starts.
        self._flush_input()
        self._item_press_t = event.t
        self._pending = None
        if self._begin_value_change(event, node, view, prop=prop):
            # Once the click has settled, this row number becomes a row, and
            # that row's text becomes the durable way to click it again.
            self._value_is_selection = True
        return True

    def _looks_like_a_view(self, event: RawEvent) -> bool:
        """Whether this click may have landed on a row of something.

        Generous on purpose. Asking Qat for row 0 of a widget that has no rows
        costs one lookup that finds nothing, and the alternative -- being strict
        and missing an application's own view subclass -- costs a step that
        clicks the middle of a list.
        """
        if event.target.is_item:
            return True
        if is_view(event.target.cls):
            return True
        # A click through a viewport: the promoted owner is the view.
        return (is_internal(event.target.object_name)
                and any(is_view(cls) for cls, _ in event.target.path))

    def _view_target(self, event: RawEvent):
        """(node, Target) for the view holding the item that was clicked."""
        if event.target.item_view:
            matches = self.backend.find_all(
                {"objectName": event.target.item_view})
            if len(matches) == 1:
                candidate = self.resolver.resolve(matches[0])
                if candidate.robustness is not Robustness.UNRESOLVED:
                    return matches[0], candidate
        # No name of its own: fall back to whatever the ordinary resolution of
        # this event produces, which promotes a viewport to its view already.
        return self._resolve(event.target)

    # -- menus -------------------------------------------------------------

    def _handle_menu(self, event: RawEvent) -> bool:
        """Fold a click on a menu into a click on the item that was chosen.

        Returns True when the event has been dealt with here.

        Using a menu produces more raw events than it appears to. Pressing on
        the bar opens the menu, and Qt immediately redirects the release to the
        popup that has just appeared -- a release on a different widget, at a
        position that is not even inside it, which is neither a drag nor a
        second click. Only the press names an item, so the press is what gets
        recorded and the release belonging to it is dropped.

        Unless the release names a *different* item: that is the press-drag-
        release way of using a menu, and then the release is a choice in its own
        right.
        """
        if event.kind == "mouse_release":
            pressed_at = self._menu_press_t
            self._menu_press_t = None
            if pressed_at is None or event.t - pressed_at > self.click_ms:
                return False
            target = self._menu_item_target(event)
            if target is not None and target.definition != self._menu_definition:
                self._add_menu_click(event, target)
            return True

        # A press or a double click. Any of them ends a menu interaction.
        self._menu_press_t = None
        if not event.target.menu_item:
            return False

        target = self._menu_item_target(event)
        if target is None:
            # Deliberately dropped rather than recorded against the menu. A
            # click on the menu itself lands in the centre of a bar the width of
            # the window and opens nothing; it has been watched failing on a
            # real application twice. A recording that is short by one step and
            # says so beats one that looks complete and does not replay.
            self._drop(event, NOT_FOUND)
            return True

        # Qt sends press, release, double-click, release for a double click. The
        # double-click event is the second half of a choice already recorded --
        # emitting it again would click the same menu title twice, which closes
        # the menu the first click opened.
        if (event.kind == "mouse_double"
                and target.definition == self._menu_definition):
            self._menu_press_t = event.t
            return True

        self._flush_input()
        self._pending = None
        self._menu_press_t = event.t
        self._menu_definition = target.definition
        self._add_menu_click(event, target)
        return True

    def _menu_item_target(self, event: RawEvent) -> Optional[Target]:
        """The chosen item of the menu this event was delivered to."""
        if not event.target.menu_item:
            return None
        resolved = self._resolve(event.target)          # the menu itself
        if resolved is None:
            return None
        _, menu = resolved
        return resolve_menu_item(self.backend, menu.definition,
                                 event.target.menu_item,
                                 event.target.menu_item_name)

    def _add_menu_click(self, event: RawEvent, target: Target) -> None:
        kind = (ActionKind.CONTEXT_CLICK if event.button == 2
                else ActionKind.CLICK)
        note = "opens the menu" if is_menu_bar(event.target.cls) else ""
        self.recording.add(Action(
            kind, target=target, args=self._button_args(event),
            t=self._elapsed(event.t), note=note))

    def _handle_key(self, event: RawEvent) -> None:
        if event.key in MODIFIER_KEYS:
            return

        resolved = self._resolve(event.target)
        if resolved is None:
            self._drop(event)
            return
        node, target = resolved

        if event.modifiers & COMMAND_MODIFIERS:
            self._flush_input()
            combination = "+".join(modifier_names(event.modifiers)
                                   + [key_name(event.key)])
            # Qt dispatches shortcuts at window scope, not to the focused widget.
            # Recording Ctrl+S against whichever field happened to have focus is
            # both wrong and fragile: on replay Qat sends the event to that
            # widget, nothing accepts it, and the step fails with "No widget
            # accepted this event". Attribute it to the window instead.
            self.recording.add(Action(
                ActionKind.SHORTCUT, target=self._window_target(node, target),
                args={"keys": combination}, t=self._elapsed(event.t)))
            return

        if is_text_key(event.key):
            properties = self._properties_of(node)
            if not is_editable(event.target.cls, properties):
                # Characters landing on something that is not a text field are
                # navigation or activation, not typing. Recording them as typing
                # would read the widget's caption back as user input.
                self._flush_input()
                self.recording.add(Action(
                    ActionKind.KEY, target=target,
                    args={"key": key_name(event.key)}, t=self._elapsed(event.t)))
                return

            # Do not record the character. Note which field is being edited and
            # read its value back when the run ends.
            if (self._typing_target is not None
                    and self._typing_target.definition != target.definition):
                self._flush_input()
            if self._typing_target is None:
                self._typing_target = target
                self._typing_node = node
                self._typing_t = event.t
            return

        self._flush_input()
        self.recording.add(Action(
            ActionKind.KEY, target=target,
            args={"key": key_name(event.key)}, t=self._elapsed(event.t)))

    def _begin_value_change(self, event: RawEvent, node, target: Target,
                            prop: str = "") -> bool:
        """Note that a widget's value is being changed, if it has one.

        Returns False for widgets a wheel or drag merely scrolls. `prop` names
        the property outright for callers that already know it -- choosing from
        a combo box, where the widget under the pointer was the popup.

        Like typing, the value is not read here. A wheel arrives as a burst of
        events and a drag as a stream; reading after each one would record the
        journey rather than the destination. The value is read when the run
        ends, which is when it means something.
        """
        prop = prop or value_property(event.target.cls)
        if not prop:
            return False
        if (self._value_target is not None
                and self._value_target.definition != target.definition):
            self._flush_value()
        if self._value_target is None:
            self._value_target = target
            self._value_node = node
            self._value_property = prop
            self._value_t = event.t
        return True

    def _flush_value(self) -> None:
        """Emit the value a wheel or drag left behind."""
        if self._value_target is None:
            return
        target, node = self._value_target, self._value_node
        prop, when = self._value_property, self._value_t
        self._value_target = None
        self._value_node = None

        selection, self._value_is_selection = self._value_is_selection, False

        try:
            value = self.backend.properties(node, keys=(prop,)).get(prop)
        except TypeError:            # a backend that predates the keys argument
            value = self._properties_of(node).get(prop)
        except Exception:                                    # noqa: BLE001
            value = None

        if value is None:
            # The property vanished with the widget, or was never readable.
            # Nothing durable to say, so say nothing rather than guess.
            self.unresolved += 1
            self.failures.append(
                f"value change on {target.label or 'a widget'}: its {prop} "
                "could not be read afterwards")
            return

        if selection:
            # A row number is a means, not an end. Turn it into a click on the
            # row itself, identified by its text, which is what the person
            # actually did and what survives the list being reordered.
            item = self._item_from_row(target, value, when)
            if item is not None:
                return

        self.recording.add(Action(
            ActionKind.SELECT, target=target,
            args={"property": prop, "value": value},
            t=self._elapsed(when)))

    def _item_from_row(self, view: Target, row, when: int):
        """Turn "row 2 is selected" into a click on the row that says X."""
        try:
            index = int(row)
        except (TypeError, ValueError):
            return None
        if index < 0:
            return None

        definition = item_definition(view.definition, index)
        matches = self.backend.find_all(definition)
        if len(matches) != 1:
            return None

        text = item_text_of(self.backend, matches[0])
        if not text:
            self._note_unlabelled_row(definition, matches[0])
        return self.recording.add(Action(
            ActionKind.CLICK,
            target=self._item_target(definition, text, index),
            t=self._elapsed(when)))

    def _note_unlabelled_row(self, definition: dict, node) -> None:
        """Say what a row without a label does publish.

        Recorded once per view. A step that is positional because its row shows
        no text is a real limitation, but "no text" is a conclusion, not
        evidence -- and the evidence is one round trip away.
        """
        container = str(definition.get("container"))
        if container in self._unlabelled_views:
            return
        self._unlabelled_views.add(container)

        listed = getattr(self.backend, "all_properties", None)
        try:
            names = sorted((listed(node) if callable(listed) else {}).keys())
        except Exception:                                    # noqa: BLE001
            names = []
        self.failures.append(
            f"rows of {container} show no text that can be read, so steps on "
            f"them are positional. The row publishes: "
            f"{', '.join(names) or 'nothing at all'}")

    def _item_target(self, definition: dict, text: str, row: int) -> Target:
        """A row, graded by whether it can be found again by anything but luck.

        A row with a label is found by that label on replay and survives the
        list being reordered. A row without one is its position and nothing
        else -- which is fragile, and has to say so. Describing it as
        "found by its text" when there is no text was a warning that lied.
        """
        if text:
            return Target(
                definition=definition, strategy="item",
                robustness=Robustness.MODERATE, warnings=(ITEM_WARNING,),
                label=text, item_text=text)
        return Target(
            definition=definition, strategy="item+index",
            robustness=Robustness.FRAGILE, warnings=(ITEM_POSITIONAL,),
            label=f"row {row}", item_text="")

    def _flush_input(self) -> None:
        """End any run of typing or value changes that is in progress."""
        self._flush_typing()
        self._flush_value()

    def _flush_typing(self) -> None:
        if self._typing_target is None:
            return
        target, node, when = self._typing_target, self._typing_node, self._typing_t
        self._typing_target = None
        self._typing_node = None

        properties = self._properties_of(node)

        if is_secret_field(properties):
            value: Any = secret_ref(target.label or "secret")
            note = "value redacted: field has a non-normal echoMode"
        else:
            value = properties.get("text", "")
            note = ""

        self.recording.add(Action(
            ActionKind.TYPE, target=target, args={"text": value},
            t=self._elapsed(when), note=note))

    # -- helpers -----------------------------------------------------------

    def _owning_widget_target(self, locator: Locator, node,
                              fallback: Target) -> Target:
        """The nearest named ancestor of a shortcut's receiver.

        Uses the ancestor chain the native filter reports rather than walking the
        tree, so it picks the closest sensible widget instead of the outermost
        object.
        """
        name = locator.nearest_named_ancestor()
        if name:
            matches = self.backend.find_all({"objectName": name})
            if len(matches) == 1:
                candidate = self.resolver.resolve(matches[0])
                if candidate.robustness is not Robustness.UNRESOLVED:
                    return candidate
        return self._window_target(node, fallback)

    def _window_target(self, node, fallback: Target) -> Target:
        """The outermost ancestor that can still be named durably.

        Used for shortcuts, which belong to the window rather than to whatever
        had keyboard focus when they were pressed.
        """
        best = fallback
        current = node
        for _ in range(12):
            parent = self.backend.parent(current)
            if parent is None:
                break
            candidate = self.resolver.resolve(parent)
            if candidate.robustness is not Robustness.UNRESOLVED:
                best = candidate
            current = parent
        return best

    def _properties_of(self, node) -> dict:
        try:
            return dict(self.backend.properties(node))
        except Exception:                                    # noqa: BLE001
            return {}

    def _button_args(self, event: RawEvent) -> dict:
        args = {}
        button = BUTTON_NAMES.get(event.button)
        if button and button != "left":
            args["button"] = button
        modifiers = modifier_names(event.modifiers)
        if modifiers:
            args["modifiers"] = "+".join(modifiers)
        # No coordinates, ever. A step that clicks a position rather than a
        # named thing replays correctly exactly once -- on the machine, screen
        # and window size it was recorded on -- and then quietly clicks
        # whatever moved into that spot.
        return args

    def _resolve(self, locator: Locator):
        """Resolve a locator, remembering why in `self._last_reason` if it fails.

        Also remembers, in `self._last_note`, anything the resulting step should
        say about itself -- a click attributed to a widget rather than to the
        thing inside it needs to admit that.
        """
        key = (locator.cls, locator.object_name, locator.text,
               locator.title, locator.index, locator.path)
        if key in self._resolved_cache:
            result, self._last_reason, self._last_note = self._resolved_cache[key]
            return result

        result, reason, note = self._resolve_uncached(locator)
        self._resolved_cache[key] = (result, reason, note)
        self._last_reason, self._last_note = reason, note
        return result

    def _resolve_uncached(self, locator: Locator):
        if is_chrome(locator.cls, locator.object_name):
            return None, CHROME, ""

        note = ""
        if is_internal(locator.object_name):
            # Clicked through one of Qt's own widgets. The event belongs to the
            # control that owns it.
            owner = owner_of(locator)
            if owner is None:
                return None, NO_OWNER, ""
            note = ("clicks the widget, not the item under the pointer: the "
                    "click landed on Qt's own "
                    f"{locator.object_name}")
            locator = owner

        node = find_by_locator(self.backend, locator)
        if node is None:
            return None, NOT_FINDABLE, ""
        target = self.resolver.resolve(node)
        if target.robustness is Robustness.UNRESOLVED:
            return None, NOT_UNIQUE, ""
        return (node, target), "", note

    def _drop(self, event: RawEvent, reason: str = "") -> None:
        """Count an event that could not be recorded, and say why."""
        self.unresolved += 1
        self.failures.append(
            f"{event.kind} on {describe(event.target)}: "
            f"{reason or self._last_reason or 'unresolved'}")

    def _target_for_node(self, node) -> Target:
        return self.resolver.resolve(node)

    def _elapsed(self, when: int) -> float:
        if self._t0 is None:
            return 0.0
        return max(0.0, (when - self._t0) / 1000.0)

    def _last_time(self) -> float:
        return self.recording.actions[-1].t if self.recording.actions else 0.0
