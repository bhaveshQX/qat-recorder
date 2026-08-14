# -*- coding: utf-8 -*-
"""Tests for menu-item resolution."""

import pytest

from qat_recorder.backend import FakeNode
from qat_recorder.ir import Robustness
from qat_recorder.menus import (
    candidate_labels, is_menu, resolve_menu_item, strip_mnemonic,
)
from tests.fixtures import build_tree


@pytest.mark.parametrize("raw, expected", [
    ("&Options", "Options"),
    ("Save &As...", "Save As..."),
    ("Options", "Options"),
    ("R&&D", "R&D"),                 # a doubled ampersand is a literal one
    ("", ""),
])
def test_the_mnemonic_marker_is_removed(raw, expected):
    assert strip_mnemonic(raw) == expected


def test_an_ellipsis_is_tried_both_ways():
    """"Options..." and "Options" are the same item to a person."""
    labels = candidate_labels("&Options...")
    assert labels[0] == "Options..."
    assert "Options" in labels


@pytest.mark.parametrize("class_name, expected", [
    ("QMenu", True), ("QMenuBar", True), ("MyAppMenu", True),
    ("QPushButton", False), ("QLineEdit", False), ("", False),
])
def test_menus_are_recognised(class_name, expected):
    assert is_menu(class_name) is expected


def test_an_item_is_found_by_its_menu_and_its_label():
    backend, _ = build_tree()
    target = resolve_menu_item(backend, {"objectName": "menuOptions"},
                               "&Preferences")
    assert target is not None
    assert target.definition == {
        "container": {"objectName": "menuOptions"}, "text": "Preferences"}
    assert target.label == "Preferences"
    assert target.robustness is Robustness.MODERATE


def test_the_label_dependency_is_stated_not_hidden():
    backend, _ = build_tree()
    target = resolve_menu_item(backend, {"objectName": "menuOptions"},
                               "&Preferences")
    assert any("translation" in warning for warning in target.warnings)


def test_an_item_that_does_not_exist_is_not_invented():
    backend, _ = build_tree()
    assert resolve_menu_item(backend, {"objectName": "menuOptions"},
                             "&Nothing here") is None


def test_an_ambiguous_label_is_refused():
    """Two items with the same label in one menu cannot be told apart.

    Accepting the first would produce a step that clicks whichever Qat returns
    first — reproducible until the day it is not.
    """
    backend, nodes = build_tree()
    menu = nodes["menu_options"]
    menu.add(FakeNode(["QWidget", "QObject"], {"text": "Preferences"}))
    assert resolve_menu_item(backend, {"objectName": "menuOptions"},
                             "&Preferences") is None


def test_no_container_means_no_guess():
    backend, _ = build_tree()
    assert resolve_menu_item(backend, {}, "&Preferences") is None
