# -*- coding: utf-8 -*-
"""Tests for the object-name resolver."""

import pytest

from qat_recorder.ir import Robustness
from qat_recorder.naming import (
    I18N_WARNING, INDEX_WARNING, NameResolver, is_secret_field, summarise,
)
from tests.fixtures import build_tree


@pytest.fixture()
def tree():
    backend, nodes = build_tree()
    return backend, nodes, NameResolver(backend)


# --- the ladder ------------------------------------------------------------

def test_named_object_uses_bare_objectname(tree):
    _, nodes, resolver = tree
    target = resolver.resolve(nodes["login"])
    assert target.definition == {"objectName": "loginButton"}
    assert target.robustness is Robustness.STRONG
    assert target.warnings == ()
    assert target.index is None


def test_shortest_working_definition_wins(tree):
    """objectName alone identifies it, so type must not be added."""
    _, nodes, resolver = tree
    target = resolver.resolve(nodes["status"])
    assert target.definition == {"objectName": "statusLabel"}


def test_unnamed_but_unique_text_falls_to_text_and_is_flagged(tree):
    _, nodes, resolver = tree
    target = resolver.resolve(nodes["import_btn"])
    assert target.definition == {"type": "QPushButton", "text": "Import"}
    assert target.robustness is Robustness.WEAK
    assert I18N_WARNING in target.warnings


def test_duplicate_text_across_containers_is_scoped(tree):
    """'Export' is ambiguous globally; each becomes unique once scoped."""
    backend, nodes, resolver = tree

    inner = resolver.resolve(nodes["export_inner"])
    outer = resolver.resolve(nodes["export_outer"])

    assert inner.definition != outer.definition
    for target in (inner, outer):
        assert target.index is None
        matches = backend.find_all(target.definition)
        assert len(matches) == 1

    assert backend.find_all(inner.definition)[0] is nodes["export_inner"]
    assert backend.find_all(outer.definition)[0] is nodes["export_outer"]


def test_outer_duplicate_needs_parent_not_container(tree):
    """`container` is recursive and would match both Exports; `parent` is not.

    This pins the distinction that makes scoping work at all.
    """
    backend, nodes, resolver = tree
    target = resolver.resolve(nodes["export_outer"])

    assert "parent" in target.definition, target.definition
    # The recursive form really would be ambiguous:
    recursive = {"type": "QPushButton", "text": "Export",
                 "container": {"objectName": "rootWidget"}}
    assert len(backend.find_all(recursive)) == 2


def test_identical_siblings_fall_back_to_index(tree):
    """Two unnamed identical buttons in one container cannot be told apart by
    properties. The resolver must say so rather than emit a lying definition."""
    backend, nodes, resolver = tree

    first = resolver.resolve(nodes["apply_a"])
    second = resolver.resolve(nodes["apply_b"])

    for target in (first, second):
        assert target.robustness is Robustness.FRAGILE
        assert INDEX_WARNING in target.warnings
        assert target.index is not None

    # Indices must follow document order, not traversal happenstance.
    assert first.index == 0
    assert second.index == 1

    # The indexed definition must be scoped, so that a matching object appearing
    # elsewhere in the tree cannot shift the index out from under the recording.
    for target in (first, second):
        assert "parent" in target.definition or "container" in target.definition, (
            target.definition)

    assert backend.find_all(first.definition)[first.index] is nodes["apply_a"]
    assert backend.find_all(second.definition)[second.index] is nodes["apply_b"]


def test_find_all_returns_document_order(tree):
    """Positional fallbacks index into these results, so the order is a contract."""
    backend, nodes, _ = tree
    applies = backend.find_all({"type": "QPushButton", "text": "Apply"})
    assert applies == [nodes["apply_a"], nodes["apply_b"]]

    everything = list(backend.walk())
    assert everything[0] is nodes["window"]
    assert everything.index(nodes["creds"]) < everything.index(nodes["import_btn"])
    assert everything.index(nodes["username"]) < everything.index(nodes["password"])


# --- the invariant that matters --------------------------------------------

def test_every_definition_is_validated_before_being_returned(tree):
    """The core promise: a returned definition either identifies exactly one
    object, or carries an index, or is explicitly marked UNRESOLVED."""
    backend, _, resolver = tree

    for node in backend.walk():
        target = resolver.resolve(node)
        matches = backend.find_all(target.definition)

        if target.robustness is Robustness.UNRESOLVED:
            continue
        if target.index is not None:
            assert matches[target.index] is node
        else:
            assert len(matches) == 1, (target.definition, len(matches))
            assert matches[0] is node


def test_resolver_never_silently_returns_an_ambiguous_definition(tree):
    backend, _, resolver = tree
    for node in backend.walk():
        target = resolver.resolve(node)
        if target.index is None and target.robustness is not Robustness.UNRESOLVED:
            assert len(backend.find_all(target.definition)) == 1


# --- inheritance-aware type matching ---------------------------------------

def test_type_matching_follows_the_inheritance_chain(tree):
    backend, nodes, _ = tree
    buttons = backend.find_all({"type": "QAbstractButton"})
    assert nodes["login"] in buttons
    assert nodes["remember"] in buttons          # QCheckBox is a QAbstractButton
    assert nodes["status"] not in buttons


# --- credential handling ---------------------------------------------------

def test_password_field_is_detected(tree):
    backend, nodes, _ = tree
    assert is_secret_field(backend.properties(nodes["password"])) is True
    assert is_secret_field(backend.properties(nodes["username"])) is False


def test_field_without_echomode_is_not_secret(tree):
    backend, nodes, _ = tree
    assert is_secret_field(backend.properties(nodes["login"])) is False


@pytest.mark.parametrize("value", [
    "Password", "NoEcho", "PasswordEchoOnEdit",   # Qat's real string enums
    2, 3, "2",                                    # numeric forms, other versions
    "SomethingUnrecognised",                      # unknown -> redact anyway
])
def test_every_non_normal_echomode_counts_as_secret(value):
    """Qat returns the enum NAME, not the number. Assuming an int here once made
    is_secret_field() return False for every real password field."""
    assert is_secret_field({"echoMode": value}) is True


@pytest.mark.parametrize("value", ["Normal", 0, "0", "", None])
def test_normal_and_missing_echomode_are_not_secret(value):
    assert is_secret_field({"echoMode": value}) is False


def test_non_text_widget_is_not_secret():
    assert is_secret_field({"objectName": "button"}) is False


# --- reporting -------------------------------------------------------------

def test_summarise_counts_by_robustness(tree):
    backend, _, resolver = tree
    targets = [resolver.resolve(node) for node in backend.walk()]
    counts = summarise(targets)
    assert sum(counts.values()) == len(targets)
    assert counts["strong"] > 0
    assert counts["fragile"] >= 2          # the two Apply buttons
