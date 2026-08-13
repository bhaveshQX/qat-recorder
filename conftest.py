# -*- coding: utf-8 -*-
"""Pytest configuration for the QAT Recorder test suite."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run tests that require launching a real Qt application through Qat",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: requires a running application under test (needs --live)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="needs --live and a working Qat injection")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
