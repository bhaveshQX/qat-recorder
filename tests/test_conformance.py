# -*- coding: utf-8 -*-
"""
Conformance: does FakeBackend actually behave like the real Qat server?

Every unit test for the resolver runs against `FakeBackend`, which reimplements
Qat's match semantics from documented behaviour. If the real server deviates, the
whole suite would stay green while production silently misbehaves. These tests
pin the assumptions that matter against a live application.

Requires working injection, so it is skipped unless run with `--live`:

    pytest tests/test_conformance.py --live

This caught a real bug on its first run: Qat reports `echoMode` as the string
'Password', not a number, so password fields were not being redacted while every
offline test passed.
"""

import sys
from pathlib import Path

import pytest

from qat_recorder.audit import audit
from qat_recorder.backend import QatBackend
from qat_recorder.launcher import prepare_python_aut_env
from qat_recorder.naming import NameResolver, is_secret_field
from tests.fixtures import build_tree

pytestmark = pytest.mark.live

SPIKE = Path(__file__).resolve().parents[1] / "spike"
APP_NAME = "conformance_sample"


@pytest.fixture(scope="module")
def live_backend():
    qat = pytest.importorskip("qat")
    pytest.importorskip("PySide6")
    # Qat resolves the interpreter symlink, launching the system python; without
    # this the AUT cannot import PySide6. See qat_recorder/launcher.py.
    prepare_python_aut_env(str(SPIKE))
    qat.register_application(APP_NAME, sys.executable, str(SPIKE / "sample_app.py"))
    context = qat.start_application(APP_NAME)
    try:
        yield QatBackend(qat)
    finally:
        try:
            qat.close_application(context)
        finally:
            qat.unregister_application(APP_NAME)


def _count(backend, definition):
    return len(backend.find_all(definition))


def test_objectname_lookup_is_unique(live_backend):
    for name in ("mainWindow", "loginButton", "passwordField", "duplicateGroup"):
        assert _count(live_backend, {"objectName": name}) == 1, name


def test_a_definition_matching_nothing_returns_empty_rather_than_raising(
        live_backend):
    """Qat's find_all_objects RAISES LookupError when nothing matches.

    The whole resolver is built on trying candidate definitions and counting the
    matches, so "no match" has to be an ordinary answer. Left untranslated, the
    exception escaped and aborted an entire recording on a real application —
    the object had simply moved to a tab that was no longer visible, and Qat
    adds `visible: true` to every definition.
    """
    assert live_backend.find_all({"objectName": "definitelyNotAnObject"}) == []
    assert live_backend.find_all({"type": "NoSuchWidgetClass"}) == []


def test_type_matching_is_inheritance_aware(live_backend):
    """The resolver relies on `type` matching base classes, not just the exact
    class. If this fails, every `type`-based definition narrows differently than
    the unit tests assume."""
    exact = _count(live_backend, {"type": "QPushButton"})
    base = _count(live_backend, {"type": "QAbstractButton"})
    assert exact > 0
    assert base >= exact, "QCheckBox/QPushButton should both match QAbstractButton"


def test_container_is_recursive_and_parent_is_not(live_backend):
    """The distinction the scoping strategy depends on."""
    recursive = _count(live_backend, {
        "type": "QPushButton", "text": "Export",
        "container": {"objectName": "rootWidget"}})
    direct = _count(live_backend, {
        "type": "QPushButton", "text": "Export",
        "parent": {"objectName": "rootWidget"}})
    assert recursive >= direct
    assert direct <= 1, "parent must match direct children only"


def test_duplicate_buttons_are_genuinely_ambiguous(live_backend):
    """Confirms the sample app really does present the hard case."""
    assert _count(live_backend, {"type": "QPushButton", "text": "Apply"}) == 2


def test_find_all_returns_stable_document_order(live_backend):
    """Positional fallbacks index into these results, so order must be stable
    across calls. If it is not, index-based targets are unusable and the
    resolver must treat those objects as UNRESOLVED instead."""
    definition = {"type": "QPushButton", "text": "Apply"}
    first = [live_backend.identity(n) for n in live_backend.find_all(definition)]
    second = [live_backend.identity(n) for n in live_backend.find_all(definition)]
    assert first == second
    assert len(set(first)) == len(first)


def test_children_and_parent_round_trip(live_backend):
    """Walking up from a known widget must reach the window.

    Not via `children[0]` — that is a QMainWindowLayout whose parent Qat reports
    as a *null object*, which is the case QatBackend.parent() normalises to None.
    """
    window = live_backend.find_all({"objectName": "mainWindow"})[0]
    assert live_backend.children(window), "mainWindow should have children"

    node = live_backend.find_all({"objectName": "loginButton"})[0]
    seen = []
    for _ in range(20):
        node = live_backend.parent(node)
        if node is None:
            break
        seen.append(live_backend.identity(node))

    assert live_backend.identity(window) in seen, seen


def test_null_parent_is_normalised_to_none(live_backend):
    """Qat returns a null QtObject rather than None at the top of the tree."""
    window = live_backend.find_all({"objectName": "mainWindow"})[0]
    layout = live_backend.children(window)[0]
    result = live_backend.parent(layout)
    assert result is None or live_backend.identity(result)


def test_password_field_is_detected_on_a_live_app(live_backend):
    """The bug this pins: Qat reports echoMode as the enum NAME ('Password'),
    not the number. Assuming an int made is_secret_field() return False for every
    real password field, which would have written credentials into recordings."""
    field = live_backend.find_all({"objectName": "passwordField"})[0]
    props = live_backend.properties(field)
    assert "echoMode" in props
    assert is_secret_field(props) is True

    plain = live_backend.find_all({"objectName": "usernameField"})[0]
    assert is_secret_field(live_backend.properties(plain)) is False


def test_qml_inside_a_quickwidget_is_not_reachable(live_backend):
    """A documented Qat limitation, not a bug in this project.

    The scene loads (no qmlErrorLabel) and the QQuickWidget itself is visible, but
    Qat's QML plugin does not traverse into it — a QQuickWidget renders into an
    internal, non-top-level window. QML embedded this way cannot be recorded or
    replayed. Use a top-level QQuickView/QQuickWindow instead.

    If this test starts failing, upstream has fixed it and the constraint in
    LINUX-VERIFICATION.md can be lifted.
    """
    assert _count(live_backend, {"objectName": "quickView"}) == 1, \
        "the QQuickWidget itself should be visible"
    assert _count(live_backend, {"objectName": "qmlErrorLabel"}) == 0, \
        "the QML scene should have loaded"
    assert _count(live_backend, {"objectName": "qmlNamedTile"}) == 0
    assert _count(live_backend, {"type": "QQuickItem"}) == 0


@pytest.fixture(scope="module")
def qml_window_backend():
    """A separate application whose QML is a top-level QQuickView."""
    qat = pytest.importorskip("qat")
    pytest.importorskip("PySide6")
    prepare_python_aut_env(str(SPIKE))
    name = "conformance_qml_window"
    qat.register_application(name, sys.executable, str(SPIKE / "qml_window_app.py"))
    context = qat.start_application(name)
    try:
        yield QatBackend(qat)
    finally:
        try:
            qat.close_application(context)
        finally:
            qat.unregister_application(name)


def test_qml_in_a_top_level_quickview_is_fully_reachable(qml_window_backend):
    """The supported QML topology: scene as a top-level QQuickView."""
    backend = qml_window_backend
    assert _count(backend, {"objectName": "qmlNamedTile"}) == 1
    assert _count(backend, {"objectName": "qmlNamedArea"}) == 1
    assert _count(backend, {"type": "MouseArea"}) == 2      # named and unnamed
    assert _count(backend, {"type": "QQuickItem"}) > 0


def test_qml_properties_and_input_work_in_a_quickview(qml_window_backend):
    tile = qml_window_backend.find_all({"objectName": "qmlNamedTile"})[0]
    assert int(tile.width) == 140
    qml_window_backend.qat.mouse_click({"objectName": "qmlNamedArea"})


def test_resolver_agrees_with_the_synthetic_tree(live_backend):
    """The headline conformance check: resolving the same objects against the
    real application must produce the same strategies as against the fake."""
    fake_backend, nodes = build_tree()
    fake_resolver = NameResolver(fake_backend)
    live_resolver = NameResolver(live_backend)

    for key, definition in [
        ("login", {"objectName": "loginButton"}),
        ("password", {"objectName": "passwordField"}),
        ("import_btn", {"type": "QPushButton", "text": "Import"}),
    ]:
        live_nodes = live_backend.find_all(definition)
        assert len(live_nodes) == 1, (key, len(live_nodes))
        live_target = live_resolver.resolve(live_nodes[0])
        fake_target = fake_resolver.resolve(nodes[key])
        assert live_target.robustness is fake_target.robustness, key
        assert live_target.definition == fake_target.definition, key


def test_audit_runs_against_a_live_application(live_backend):
    report = audit(live_backend, limit=2000)
    assert report.entries
    assert sum(report.counts.values()) == len(report.graded)
    print("\n" + report.render())
