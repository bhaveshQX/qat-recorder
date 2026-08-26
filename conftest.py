import os
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


# A picture of the screen for every recorded step is a real feature and a real
# cost: the suite drives hundreds of steps through agent-backed sessions, and
# photographing this machine's screen for each one doubled how long it takes.
# The tests that are about stills turn it back on for themselves.
os.environ.setdefault("QATREC_NO_STEP_SHOTS", "1")
