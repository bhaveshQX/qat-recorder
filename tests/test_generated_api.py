# -*- coding: utf-8 -*-
"""
Every call this project generates must exist in Qat, with the arguments we pass.

This is the one class of bug that has bitten repeatedly, and always in the same
way: the code is written against a remembered API, the tests use a fake that
accepts anything, and the mistake surfaces minutes into a replay on a VM.

    TypeError: mouse_wheel() got an unexpected keyword argument 'xDegrees'

A fake cannot catch that -- it is only ever as accurate as the memory that wrote
it. So these tests read the *real* `qat` module and check the generated source
against `inspect.signature`. No application is launched and nothing is called;
only the shape of each call is checked, which is exactly the part a fake cannot
vouch for.

Skipped where qat is not installed, so a checkout without it still tests
everything else.
"""

import ast
import inspect

import pytest

qat = pytest.importorskip("qat", reason="signature checks need the real qat")

from qat_recorder.emit import emit_python                        # noqa: E402
from qat_recorder.ir import (                                    # noqa: E402
    Action, ActionKind, Recording, Robustness, Target,
)

#: Attributes of `qat` that are values rather than functions, and so are used
#: rather than called.
CONSTANTS = {"Button", "Modifier"}


def target(label: str = "thing") -> Target:
    return Target(definition={"objectName": label}, strategy="objectName",
                  robustness=Robustness.STRONG, label=label)


def every_kind() -> Recording:
    """A recording that exercises every action the emitter can produce."""
    recording = Recording(app="sample")
    recording.meta["app_path"] = "/opt/sample"
    recording.add(Action(ActionKind.LAUNCH, args={"app": "sample"}))
    recording.add(Action(ActionKind.CLICK, target=target("plain")))
    recording.add(Action(ActionKind.CLICK, target=target("right"),
                         args={"button": "right"}))
    recording.add(Action(ActionKind.CLICK, target=Target(
        definition={"container": {"objectName": "menuBar"}, "text": "File"},
        strategy="menu item", robustness=Robustness.MODERATE, label="File")))
    recording.add(Action(ActionKind.CONTEXT_CLICK, target=target("context")))
    recording.add(Action(ActionKind.DOUBLE_CLICK, target=target("twice")))
    recording.add(Action(ActionKind.TYPE, target=target("field"),
                         args={"text": "alice"}))
    recording.add(Action(ActionKind.KEY, target=target("field"),
                         args={"key": "Tab"}))
    recording.add(Action(ActionKind.SHORTCUT, target=target("window"),
                         args={"keys": "Ctrl+S"}))
    recording.add(Action(ActionKind.WHEEL, target=target("list"),
                         args={"dx": 0, "dy": -12}))
    recording.add(Action(ActionKind.DRAG, target=target("slider"),
                         args={"dx": 40, "dy": 0}))
    recording.add(Action(ActionKind.SET_CHECKED, target=target("box"),
                         args={"checked": True}))
    recording.add(Action(ActionKind.VERIFY_PROPERTY, target=target("label"),
                         args={"property": "text", "expected": "ready"}))
    recording.add(Action(ActionKind.WAIT_MISSING, target=target("spinner")))
    recording.add(Action(ActionKind.SCREENSHOT, args={"path": "shot.png"}))
    recording.add(Action(ActionKind.CLOSE))
    return recording


def qat_calls(source: str):
    """Every `qat.something(...)` in the generated module, with its arguments."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not isinstance(function, ast.Attribute):
            continue
        if not (isinstance(function.value, ast.Name)
                and function.value.id == "qat"):
            continue
        yield (function.attr,
               len(node.args),
               [keyword.arg for keyword in node.keywords])


@pytest.fixture(scope="module")
def generated() -> str:
    return emit_python(every_kind())


def test_the_generated_module_calls_qat_at_all(generated):
    """A guard on the guard: if the parser found nothing, the rest is vacuous."""
    names = {name for name, _, _ in qat_calls(generated)}
    assert {"mouse_click", "type_in", "mouse_wheel", "start_application"} <= names


def test_every_generated_call_exists_in_qat(generated):
    missing = sorted({name for name, _, _ in qat_calls(generated)
                      if not hasattr(qat, name)})
    assert not missing, f"qat has no {missing}"


def test_every_generated_call_accepts_the_arguments_we_pass(generated):
    """`inspect.signature.bind` decides, not our memory of the API."""
    problems = []
    for name, positional, keywords in qat_calls(generated):
        function = getattr(qat, name, None)
        if function is None or not callable(function):
            continue
        signature = inspect.signature(function)
        try:
            signature.bind(*([None] * positional),
                           **{key: None for key in keywords if key})
        except TypeError as error:
            problems.append(f"qat.{name}: {error}")
    assert not problems, "\n".join(problems)


def test_the_check_catches_the_bug_that_motivated_it():
    """A guard nobody has seen fail is a guard nobody should trust.

    `xDegrees` is what was shipped and what failed on a VM; `x_degrees` is what
    Qat actually takes.
    """
    signature = inspect.signature(qat.mouse_wheel)
    with pytest.raises(TypeError):
        signature.bind(None, xDegrees=0, yDegrees=-12)
    signature.bind(None, x_degrees=0, y_degrees=-12)      # the real spelling


def test_constants_referenced_by_generated_code_exist(generated):
    """`qat.Button.RIGHT` is used by right-click steps."""
    tree = ast.parse(generated)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        inner = node.value
        if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name) \
                and inner.value.id == "qat" and inner.attr in CONSTANTS:
            constant = getattr(qat, inner.attr)
            assert hasattr(constant, node.attr), \
                f"qat.{inner.attr} has no {node.attr}"


def test_the_player_calls_qat_the_same_way():
    """The player drives Qat directly rather than through generated code, so it
    is a second place the same mistake can live -- and did."""
    import qat_recorder.player as player_module

    source = inspect.getsource(player_module)
    problems = []
    for name, positional, keywords in qat_calls(source):
        function = getattr(qat, name, None)
        if function is None:
            problems.append(f"qat has no {name}")
            continue
        if not callable(function):
            continue
        try:
            inspect.signature(function).bind(
                *([None] * positional),
                **{key: None for key in keywords if key})
        except TypeError as error:
            problems.append(f"qat.{name}: {error}")
    assert not problems, "\n".join(problems)
