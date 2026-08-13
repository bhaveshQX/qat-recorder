# -*- coding: utf-8 -*-
"""Tests for the nameability audit."""

import pytest

from qat_recorder.audit import audit, is_interactive
from qat_recorder.ir import Robustness
from tests.fixtures import build_tree


# --- interactive versus internal -------------------------------------------

@pytest.mark.parametrize("class_name", [
    "QPushButton", "QCheckBox", "QLineEdit", "QComboBox", "QAction",
    "QMenu", "QToolButton", "QTabWidget", "QTreeView", "SomeCustomWidget",
])
def test_controls_are_graded(class_name):
    assert is_interactive(class_name) is True


@pytest.mark.parametrize("class_name", [
    "QVBoxLayout", "QHBoxLayout", "QBoxLayout", "QStackedLayout",
    "QItemSelectionModel", "QStandardItemModel", "QListModel",
    "QSortFilterProxyModel", "QStyledItemDelegate", "QScrollBar",
    "QSplitterHandle", "QWidgetLineControl", "QShortcut", "QActionGroup",
    "QToolBarSeparator", "QWidget", "QFrame", "QObject",
])
def test_internal_machinery_is_not_graded(class_name):
    """Nobody clicks a layout. Grading them produced advice like
    'add objectName to 15 x QItemSelectionModel' against a real application."""
    assert is_interactive(class_name) is False


@pytest.mark.parametrize("object_name", [
    "qt_scrollarea_viewport", "qt_scrollarea_hcontainer",
    "qt_tabwidget_stackedwidget", "qt_splithandle_", "_q_qlineeditclearaction",
])
def test_qt_internal_names_are_not_graded(object_name):
    assert is_interactive("QPushButton", object_name) is False


def test_unknown_classes_are_treated_as_interactive():
    """Under-reporting a real control is worse than including a container."""
    assert is_interactive("AcmeCustomThing") is True


# --- the report ------------------------------------------------------------

def test_audit_scans_everything_but_grades_only_controls():
    backend, _ = build_tree()
    report = audit(backend)

    # Everything is scanned...
    assert len(report.entries) == len(list(backend.walk()))
    # ...but the verdict covers only what a person can operate.
    assert sum(report.counts.values()) == len(report.graded)
    assert len(report.graded) + len(report.internal) == len(report.entries)
    assert report.internal, "the fixture contains a bare QWidget container"


def test_all_flag_grades_everything():
    backend, _ = build_tree()
    report = audit(backend, include_internal=True)
    assert sum(report.counts.values()) == len(report.entries)
    assert report.graded == report.entries


def test_audit_flags_the_unnameable_objects():
    backend, nodes = build_tree()
    report = audit(backend)

    problem_labels = {entry.label for entry in report.problems()}
    assert "Apply" in problem_labels          # identical siblings
    assert "Import" in problem_labels         # text-only
    assert "loginButton" not in problem_labels


def test_audit_ranks_worst_first():
    backend, _ = build_tree()
    ranks = [entry.target.robustness.rank for entry in audit(backend).problems()]
    assert ranks == sorted(ranks, reverse=True)


def test_audit_suggests_source_level_fixes():
    backend, _ = build_tree()
    suggestions = audit(backend).suggestions()
    assert suggestions
    joined = " ".join(suggestions)
    assert "objectName" in joined
    assert "QPushButton" in joined


def test_audit_groups_duplicates_into_one_suggestion():
    backend, _ = build_tree()
    suggestions = audit(backend).suggestions()
    apply_suggestions = [s for s in suggestions if "duplicateGroup" in s]
    assert len(apply_suggestions) == 1
    assert "2 x QPushButton" in apply_suggestions[0]


def test_suggestions_group_by_real_parent_not_by_definition():
    """`Import` needs no container in its definition but still lives in
    rootWidget; it must be grouped with the other rootWidget button so the
    advice points at the file the developer has to edit."""
    backend, _ = build_tree()
    suggestions = audit(backend).suggestions()
    root_suggestions = [s for s in suggestions if "'rootWidget'" in s]
    assert len(root_suggestions) == 1
    assert "2 x QPushButton" in root_suggestions[0]
    assert not any(
        "QPushButton (currently" in s for s in suggestions), suggestions


def test_audit_identifies_secret_fields():
    backend, _ = build_tree()
    report = audit(backend)
    labels = {entry.label for entry in report.secrets()}
    assert labels == {"passwordField"}


def test_audit_builds_readable_paths():
    backend, _ = build_tree()
    report = audit(backend)
    entry = next(e for e in report.entries if e.label == "passwordField")
    assert entry.path.startswith("mainWindow / rootWidget / credentialsGroup")


def test_render_produces_a_report():
    backend, _ = build_tree()
    text = audit(backend).render()
    assert "Scanned" in text
    assert "Suggested source changes:" in text
    assert "redacted" in text


def test_clean_tree_reports_no_problems():
    from qat_recorder.backend import FakeBackend, FakeNode
    root = FakeNode(["QMainWindow", "QWidget"], {"objectName": "w"})
    root.add(FakeNode(["QPushButton", "QWidget"], {"objectName": "ok"}))
    report = audit(FakeBackend([root]))
    assert report.problems() == []
    assert report.counts[Robustness.STRONG.value] == 2
    assert report.suggestions() == []
