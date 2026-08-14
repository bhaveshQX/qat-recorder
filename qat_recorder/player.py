# -*- coding: utf-8 -*-
"""
Replaying a Recording directly, without generating code.

Useful while debugging a capture: it isolates "did we record the right thing?"
from "does the generated code say the right thing?". The generated pytest module
is what you commit; this is what you use to find out why it does not work.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Mapping, Optional

from qat_recorder.ir import Action, ActionKind, Recording, SECRET_KEY, is_secret


class MissingSecret(RuntimeError):
    """A recorded step needs a value that was deliberately never written down."""


class Player:
    def __init__(self, qat_module=None, secrets: Optional[Mapping[str, str]] = None):
        if qat_module is None:
            import qat as qat_module  # noqa: PLC0415
        self.qat = qat_module
        self.secrets = dict(secrets or {})
        self.context = None

    # -- values ------------------------------------------------------------

    def resolve_value(self, value: Any) -> Any:
        if not is_secret(value):
            return value
        name = value[SECRET_KEY]
        if name in self.secrets:
            return self.secrets[name]
        from_env = os.environ.get("QATREC_SECRET_" + name)
        if from_env is not None:
            return from_env
        raise MissingSecret(
            f"'{name}' was redacted when recorded; supply it via the secrets "
            f"argument or QATREC_SECRET_{name}")

    def target_of(self, action: Action):
        definition = dict(action.target.definition)
        if action.target.index is None:
            return definition
        matches = self.qat.find_all_objects(definition)
        if action.target.index >= len(matches):
            raise LookupError(
                f"positional target {action.target.label!r} wanted index "
                f"{action.target.index} but only {len(matches)} object(s) match "
                f"{definition}; the UI has changed since recording")
        return matches[action.target.index]

    # -- playback ----------------------------------------------------------

    def play(self, recording: Recording,
             on_action: Optional[Callable[[Action], None]] = None) -> int:
        problems = recording.validate()
        if problems:
            raise ValueError("refusing to replay an invalid recording: "
                             + "; ".join(problems))

        played = 0
        for action in recording.actions:
            if on_action is not None:
                on_action(action)
            self.play_one(action, recording)
            played += 1
        return played

    def play_one(self, action: Action, recording: Optional[Recording] = None) -> None:
        qat = self.qat

        if action.kind is ActionKind.LAUNCH:
            name = action.args.get("app") or (recording.app if recording else None)
            self.context = qat.start_application(name)
            return
        if action.kind is ActionKind.CLOSE:
            qat.close_application(self.context)
            self.context = None
            return
        if action.kind is ActionKind.SCREENSHOT:
            qat.take_screenshot(action.args.get("path"))
            return

        target = self.target_of(action)

        if action.kind in (ActionKind.CLICK, ActionKind.SET_CHECKED):
            qat.mouse_click(target)
        elif action.kind is ActionKind.CONTEXT_CLICK:
            qat.mouse_click(target, button=getattr(qat, "Button", None).RIGHT
                            if hasattr(qat, "Button") else 2)
        elif action.kind is ActionKind.DOUBLE_CLICK:
            qat.double_click(target)
        elif action.kind is ActionKind.TYPE:
            qat.type_in(target, self.resolve_value(action.args.get("text", "")))
        elif action.kind is ActionKind.KEY:
            qat.press_key(target, action.args.get("key", ""))
        elif action.kind is ActionKind.SHORTCUT:
            qat.shortcut(target, action.args.get("keys", ""))
        elif action.kind is ActionKind.WHEEL:
            # x_degrees, not xDegrees: Qat's Python API is snake_case even where
            # the Qt property it wraps is not.
            qat.mouse_wheel(target, x_degrees=action.args.get("dx", 0),
                            y_degrees=action.args.get("dy", 0))
        elif action.kind is ActionKind.DRAG:
            qat.mouse_drag(target, dx=action.args.get("dx", 0),
                           dy=action.args.get("dy", 0))
        elif action.kind is ActionKind.WAIT_MISSING:
            qat.wait_for_object_missing(target)
        elif action.kind is ActionKind.VERIFY_PROPERTY:
            prop = action.args.get("property", "text")
            expected = self.resolve_value(action.args.get("expected"))
            actual = getattr(qat.wait_for_object(target), prop)
            if str(actual) != str(expected):
                raise AssertionError(
                    f"{action.target.label}.{prop}: expected {expected!r}, "
                    f"got {actual!r}")
        else:
            raise NotImplementedError(f"cannot replay {action.kind.value}")
