# -*- coding: utf-8 -*-
"""
Starting every session from the same place.

mako_shoulder saves the open case -- workflow step, plan, images -- in the case's
folder and restores it on the next Load Case, so each recording and replay began
where the previous one stopped. QATREC_RESET runs before every launch.
"""

from __future__ import annotations

import pytest

from qat_recorder import launch


class StartsNothing:
    """Enough of Qat to see whether it was asked to start anything."""

    def __init__(self):
        self.started = []

    def start_application(self, name):
        self.started.append(name)
        return "context"


def test_nothing_runs_when_it_is_not_set(monkeypatch):
    monkeypatch.delenv(launch.RESET_ENV, raising=False)
    qat = StartsNothing()
    assert launch.start(qat, "app", follow=False) == "context"


def test_the_reset_runs_before_the_application_starts(monkeypatch, tmp_path):
    marker = tmp_path / "reset-ran"
    monkeypatch.setenv(launch.RESET_ENV, f"touch {marker}")
    qat = StartsNothing()
    launch.start(qat, "app", follow=False)
    assert marker.exists()
    assert qat.started == ["app"]


def test_a_reset_that_fails_stops_the_launch(monkeypatch):
    """Running against the wrong state is the failure this exists to prevent;
    a replay that carried on anyway would fail somewhere far from the cause."""
    monkeypatch.setenv(launch.RESET_ENV, "echo no such snapshot >&2; exit 3")
    qat = StartsNothing()
    with pytest.raises(launch.LaunchError) as error:
        launch.start(qat, "app", follow=False)
    assert "exit code 3" in str(error.value)
    assert "no such snapshot" in str(error.value)
    assert qat.started == []
