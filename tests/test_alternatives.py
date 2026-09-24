# -*- coding: utf-8 -*-
"""
No step depends on one selector.

The resolver used to return at the first candidate that uniquely identified an
object and throw the rest away, so every step had exactly one way to find its
target. When that one way stopped working -- a renamed objectName, a translated
label, a dialog not yet drawn -- the step failed, even though the same object
was still findable three other ways.

Every candidate that identified the object is kept now, all of them validated
against the running application while recording, and the generated test tries
them in order and waits for one to become usable.
"""

import ast

import pytest

from qat_recorder.backend import FakeNode
from qat_recorder.capture import CaptureSession
from qat_recorder.emit import emit_python
from qat_recorder.ir import Robustness
from qat_recorder.naming import NameResolver
from tests.fixtures import BUTTON, build_tree
from tests.test_capture import click_pair


def test_a_named_button_keeps_every_way_it_could_be_found():
    backend, nodes = build_tree()
    target = NameResolver(backend).resolve(nodes["login"])

    assert target.definition == {"objectName": "loginButton"}
    assert target.alternatives                 # and it is not alone
    assert {"objectName": "loginButton", "type": "QPushButton"} \
        in target.alternatives
    # Every one of them was checked against the tree, so they all work.
    for candidate in target.candidates:
        assert backend.find_all(candidate) == [nodes["login"]]


def test_the_best_one_is_still_first():
    backend, nodes = build_tree()
    target = NameResolver(backend).resolve(nodes["login"])
    assert target.candidates[0] == {"objectName": "loginButton"}
    assert target.robustness is Robustness.STRONG


def test_the_number_kept_is_bounded():
    """Each costs a lookup while recording; three is enough to be robust."""
    backend, nodes = build_tree()
    target = NameResolver(backend, max_alternatives=2).resolve(nodes["login"])
    assert len(target.alternatives) <= 2


def test_even_a_plain_button_gets_several_ways():
    """An objectName alone, with its type, and each narrowed by a container --
    all different enough that one change is unlikely to break them all."""
    backend, nodes = build_tree()
    only = nodes["root"].add(FakeNode(BUTTON, {"objectName": "solitary"}))
    target = NameResolver(backend).resolve(only)

    assert target.candidates[0] == {"objectName": "solitary"}
    assert len(target.candidates) > 1
    for candidate in target.candidates:
        assert backend.find_all(candidate) == [only]


def test_candidates_never_repeat_themselves():
    backend, nodes = build_tree()
    target = NameResolver(backend).resolve(nodes["login"])
    rendered = [repr(sorted(one.items())) for one in target.candidates]
    assert len(rendered) == len(set(rendered))


def test_they_survive_being_written_out_and_read_back():
    from qat_recorder.ir import Target

    backend, nodes = build_tree()
    target = NameResolver(backend).resolve(nodes["login"])
    again = Target.from_dict(target.to_dict())
    assert again.candidates == target.candidates


# --- what the generated test does with them ---------------------------------

def generated():
    backend, nodes = build_tree()
    capture = CaptureSession(backend, app_name="sample")
    capture.feed_all(click_pair(1000, "QPushButton", "loginButton"))
    return emit_python(capture.finish())


def test_the_constant_holds_them_all():
    source = generated()
    assert "LOGINBUTTON = override('LOGINBUTTON', [{'objectName': " in source


def test_every_step_goes_through_the_chooser():
    """`find` picks whichever candidate the application currently answers to."""
    source = generated()
    assert "qat.mouse_click(find(LOGINBUTTON))" in source
    assert "def find(candidates, timeout_ms=None):" in source
    compile(source, "generated.py", "exec")


def test_the_chooser_waits_rather_than_failing_at_once():
    """A control that is not there yet and one that cannot be found are the
    same thing to a script, and waiting is the cure for both."""
    source = generated()
    chooser = source.split("def find(candidates")[1].split("\ndef ")[0]
    assert "deadline" in chooser
    assert "time.sleep" in chooser
    assert "wait_for_object(" in chooser     # usable, not merely present


def test_the_failure_names_everything_it_tried():
    source = generated()
    chooser = source.split("def find(candidates")[1].split("\ndef ")[0]
    assert "none of these found a usable object" in chooser
    assert "candidates" in chooser


def test_the_failure_says_what_the_application_has_for_each():
    """Listing the definitions that did not work says how hard we looked. It
    does not say *why*, and the two reasons want opposite fixes: an object that
    is not there means the application changed, an object that is there and not
    usable means the step ran too early -- or that the wait cannot be expressed
    for it at all, which is the case for a window. `wait_for_object` asks Qat
    for one that is `visible` and `enabled`, and a QWindow has no `enabled`
    property to match on, so a definition scoped by one can never be satisfied
    however plainly it is on screen. Recording never notices: it validates with
    `find_all_objects`, which adds neither."""
    import sys
    import types

    class Window:
        """On screen, and with no `enabled` property -- like every QWindow."""
        visible = True

        def __getattr__(self, name):
            raise AttributeError(name)

    stub = types.ModuleType("qat")
    stub.wait_for_object = lambda definition, timeout=None: (_ for _ in ()).throw(
        LookupError("Unable to find object"))
    stub.find_all_objects = lambda definition: (
        [Window()] if definition == {"objectName": "loginButton"} else [])

    original = sys.modules.get("qat")
    sys.modules["qat"] = stub
    try:
        generated_module = {"__file__": __file__}
        exec(compile(generated(), "generated.py", "exec"), generated_module)
        with pytest.raises(AssertionError) as failure:
            generated_module["find"](
                [{"objectName": "loginButton"}, {"objectName": "gone"}],
                timeout_ms=0)
    finally:
        if original is None:
            del sys.modules["qat"]
        else:
            sys.modules["qat"] = original

    message = str(failure.value)
    assert "exactly one object has this" in message
    assert "visible=True" in message
    assert "no enabled property" in message
    assert "{'objectName': 'gone'} -- no object in the application has this" \
        in message


def test_no_step_is_left_with_a_bare_definition():
    """Every use of a constant goes through `find`, or the alternatives are
    decoration."""
    source = generated()
    tree = ast.parse(source)
    body = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
                and node.name == "test_recorded_session")

    # Constants reached through a chooser -- find(X), or click_row(X, ...),
    # which chooses the view itself before looking for rows in it.
    chosen = set()
    for node in ast.walk(body):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id in ("find", "click_row", "row") and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name):
                chosen.add(id(first))

    allowed = {"TIMEOUT_MS", "APP_NAME", "APP_PATH"}
    for node in ast.walk(body):
        if isinstance(node, ast.Name) and node.id.isupper() \
                and node.id not in allowed and id(node) not in chosen:
            raise AssertionError(f"{node.id} is used without a chooser")
