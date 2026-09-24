# -*- coding: utf-8 -*-
"""
What the generated `find()` hands to the call that uses it.

`wait_for_object` only counts an object that is visible and enabled; the calls
that act -- `mouse_click`, `type_in` -- send the definition as given and count
every object that has it. mako_shoulder keeps its other pages alive, so "Load
Case" was found by the wait and refused by the click as "Multiple objects
found". Checked against real Qat 1.8: the plain definition fails the click, the
same one with visible and enabled clicks the right button.
"""

from __future__ import annotations

import sys
import types

from qat_recorder.emit import emit_python
from qat_recorder.ir import Action, ActionKind, Recording, Target


def generated(monkeypatch, tmp_path):
    recording = Recording(app="mako")
    recording.add(Action(ActionKind.LAUNCH, args={"app": "mako"}))
    recording.add(Action(ActionKind.CLICK, target=Target(
        definition={"objectName": "loadCaseButton", "type": "RoundButton"},
        label="loadCaseButton")))
    source = emit_python(recording)

    fake = types.ModuleType("qat")
    fake.waited = []
    fake.wait_for_object = lambda definition, timeout=None: fake.waited.append(
        definition)
    monkeypatch.setitem(sys.modules, "qat", fake)
    module = {"__file__": str(tmp_path / "test_recorded.py")}
    exec(compile(source, "generated.py", "exec"), module)
    return module, fake


def test_find_returns_the_definition_as_it_matched(monkeypatch, tmp_path):
    module, fake = generated(monkeypatch, tmp_path)
    found = module["find"]([{"objectName": "loadCaseButton",
                             "type": "RoundButton"}])
    assert found == {"objectName": "loadCaseButton", "type": "RoundButton",
                     "visible": True, "enabled": True}
    # What was waited for is untouched: the wait adds those itself.
    assert fake.waited == [{"objectName": "loadCaseButton",
                            "type": "RoundButton"}]
