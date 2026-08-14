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
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from qat_recorder.events import Locator, RawEvent
from qat_recorder.ir import Action, ActionKind, Recording, Robustness, Target, secret_ref
from qat_recorder.menus import NOT_FOUND, resolve_menu_item
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
        self._typing_node: Any = None
        self._typing_target: Optional[Target] = None
        self._typing_t: int = 0
        self._resolved_cache: dict = {}
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
                self.failures.append(f"{event.kind} on "
                                     f"{event.target.cls or '?'}: {error}")

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
        self._flush_typing()
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
            self._flush_typing()
            resolved = self._resolve(event.target)
            if resolved is None:
                self.unresolved += 1
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
            self._flush_typing()
            resolved = self._resolve(event.target)
            if resolved is not None:
                _, target = resolved
                self.recording.add(Action(
                    ActionKind.WHEEL, target=target,
                    args={"dx": event.dx, "dy": event.dy},
                    t=self._elapsed(event.t)))
        # key_release and focus_in carry no action of their own.

    def _handle_mouse(self, event: RawEvent) -> None:
        # Menus first: the object a menu click is delivered to is never the
        # thing the person clicked.
        if self._handle_menu(event):
            return

        resolved = self._resolve(event.target)
        if resolved is None:
            self.unresolved += 1
            return
        node, target = resolved

        if event.kind == "mouse_double":
            self._flush_typing()
            self._pending = None
            self.recording.add(Action(
                ActionKind.DOUBLE_CLICK, target=target,
                args=self._button_args(event), t=self._elapsed(event.t)))
            return

        if event.kind == "mouse_press":
            self._flush_typing()
            self._pending = _Pending(event, target, node)
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
                t=self._elapsed(pending.event.t)))
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

        # A release with no matching press: most likely a drag.
        if pending is not None:
            self.recording.add(Action(
                ActionKind.DRAG, target=pending.target,
                args={"dx": event.x - pending.event.x,
                      "dy": event.y - pending.event.y,
                      **self._button_args(pending.event)},
                t=self._elapsed(pending.event.t)))

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
            self.unresolved += 1
            self.failures.append(
                f"menu item {event.target.menu_item!r} in "
                f"{event.target.cls or '?'}: {NOT_FOUND}")
            return True

        # Qt sends press, release, double-click, release for a double click. The
        # double-click event is the second half of a choice already recorded --
        # emitting it again would click the same menu title twice, which closes
        # the menu the first click opened.
        if (event.kind == "mouse_double"
                and target.definition == self._menu_definition):
            self._menu_press_t = event.t
            return True

        self._flush_typing()
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
            self.unresolved += 1
            return
        node, target = resolved

        if event.modifiers & COMMAND_MODIFIERS:
            self._flush_typing()
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
                self._flush_typing()
                self.recording.add(Action(
                    ActionKind.KEY, target=target,
                    args={"key": key_name(event.key)}, t=self._elapsed(event.t)))
                return

            # Do not record the character. Note which field is being edited and
            # read its value back when the run ends.
            if (self._typing_target is not None
                    and self._typing_target.definition != target.definition):
                self._flush_typing()
            if self._typing_target is None:
                self._typing_target = target
                self._typing_node = node
                self._typing_t = event.t
            return

        self._flush_typing()
        self.recording.add(Action(
            ActionKind.KEY, target=target,
            args={"key": key_name(event.key)}, t=self._elapsed(event.t)))

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
        key = (locator.cls, locator.object_name, locator.text,
               locator.title, locator.index, locator.path)
        if key in self._resolved_cache:
            return self._resolved_cache[key]

        node = find_by_locator(self.backend, locator)
        result = None
        if node is not None:
            target = self.resolver.resolve(node)
            if target.robustness is not Robustness.UNRESOLVED:
                result = (node, target)
        self._resolved_cache[key] = result
        return result

    def _target_for_node(self, node) -> Target:
        return self.resolver.resolve(node)

    def _elapsed(self, when: int) -> float:
        if self._t0 is None:
            return 0.0
        return max(0.0, (when - self._t0) / 1000.0)

    def _last_time(self) -> float:
        return self.recording.actions[-1].t if self.recording.actions else 0.0
