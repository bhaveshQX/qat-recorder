# -*- coding: utf-8 -*-
"""Tests for the Action IR."""

import json

import pytest

from qat_recorder.ir import (
    SCHEMA_VERSION, Action, ActionKind, Recording, Robustness, Target,
    is_secret, secret_ref,
)


def make_target(name="loginButton", robustness=Robustness.STRONG, **kwargs):
    return Target(
        definition={"objectName": name},
        strategy="objectName",
        robustness=robustness,
        label=name,
        **kwargs,
    )


def test_round_trip_preserves_everything():
    recording = Recording(app="sample", meta={"qt": "6.5"})
    recording.add(Action(ActionKind.LAUNCH, args={"app": "sample"}, t=0.0))
    recording.add(Action(ActionKind.CLICK, target=make_target(), t=1.25))
    recording.add(Action(
        ActionKind.TYPE,
        target=make_target("passwordField", Robustness.STRONG),
        args={"text": secret_ref("passwordField")},
        t=2.5,
    ))

    restored = Recording.loads(recording.dumps())

    assert restored.app == recording.app
    assert restored.meta == recording.meta
    assert len(restored.actions) == 3
    assert restored.actions[1].target.definition == {"objectName": "loginButton"}
    assert restored.actions[1].target.robustness is Robustness.STRONG
    assert restored.to_dict() == recording.to_dict()


def test_index_survives_serialisation():
    target = make_target(robustness=Robustness.FRAGILE, index=1)
    recording = Recording(app="sample")
    recording.add(Action(ActionKind.CLICK, target=target))

    restored = Recording.loads(recording.dumps())
    assert restored.actions[0].target.index == 1


def test_unknown_schema_is_rejected():
    payload = json.dumps({"schema": 99, "app": "x", "actions": []})
    with pytest.raises(ValueError, match="unsupported recording schema"):
        Recording.loads(payload)


def test_current_schema_is_accepted():
    payload = json.dumps({"schema": SCHEMA_VERSION, "app": "x", "actions": []})
    assert Recording.loads(payload).app == "x"


def test_validate_flags_missing_target():
    recording = Recording(app="sample")
    recording.add(Action(ActionKind.CLICK, target=None))
    problems = recording.validate()
    assert len(problems) == 1
    assert "has no target" in problems[0]


def test_validate_flags_empty_definition():
    recording = Recording(app="sample")
    recording.add(Action(ActionKind.CLICK, target=Target(definition={})))
    assert any("empty definition" in p for p in recording.validate())


def test_launch_and_screenshot_need_no_target():
    recording = Recording(app="sample")
    recording.add(Action(ActionKind.LAUNCH, args={"app": "sample"}))
    recording.add(Action(ActionKind.SCREENSHOT, args={"path": "shot.png"}))
    assert recording.validate() == []


def test_secrets_are_marked_and_never_hold_the_literal():
    action = Action(
        ActionKind.TYPE,
        target=make_target("passwordField"),
        args={"text": secret_ref("passwordField")},
    )
    assert action.has_secret()
    assert is_secret(action.args["text"])
    assert "hunter2" not in json.dumps(action.to_dict())

    recording = Recording(app="sample")
    recording.add(action)
    assert len(recording.secrets()) == 1


def test_weakest_targets_are_ranked_worst_first():
    recording = Recording(app="sample")
    recording.add(Action(ActionKind.CLICK, target=make_target("a", Robustness.STRONG)))
    recording.add(Action(ActionKind.CLICK, target=make_target("b", Robustness.WEAK)))
    recording.add(Action(ActionKind.CLICK, target=make_target("c", Robustness.FRAGILE)))
    recording.add(Action(ActionKind.CLICK,
                         target=make_target("d", Robustness.UNRESOLVED)))

    worst = recording.weakest_targets()
    assert [t.target.label for t in worst] == ["d", "c", "b"]


def test_strong_targets_are_not_reported_as_weak():
    recording = Recording(app="sample")
    recording.add(Action(ActionKind.CLICK, target=make_target("a", Robustness.STRONG)))
    recording.add(Action(ActionKind.CLICK,
                         target=make_target("b", Robustness.MODERATE)))
    assert recording.weakest_targets() == []


def test_robustness_ordering_is_total():
    levels = [
        Robustness.STRONG, Robustness.MODERATE, Robustness.WEAK,
        Robustness.FRAGILE, Robustness.UNRESOLVED,
    ]
    ranks = [level.rank for level in levels]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(levels)
