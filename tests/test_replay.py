# -*- coding: utf-8 -*-
"""
Replaying where the application is.

A recorded test launches the application itself, so it can only run on the
machine the application is on. When a session is driven from a tester's desk,
that machine is the VM -- and having to copy the file across by hand to run it
is the errand the agent exists to remove.
"""

import os
import sys

import pytest

from qat_recorder.replay import (
    MAX_OUTPUT, NO_DISPLAY, condense, run_pytest, tail)

PASSING = "def test_ok():\n    assert True\n"
FAILING = "def test_no():\n    assert 1 == 2\n"

DISPLAY = {"DISPLAY": ":0"}


def _env(**extra):
    """A minimal environment with a display, plus whatever the test needs."""
    base = {"PATH": os.environ.get("PATH", ""), **DISPLAY}
    if "SYSTEMROOT" in os.environ:          # subprocess on Windows needs it
        base["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    base.update(extra)
    return base


def test_a_passing_test_reports_success(tmp_path):
    (tmp_path / "test_recorded.py").write_text(PASSING, encoding="utf-8")
    result = run_pytest(tmp_path, python=sys.executable, env=_env())
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert "test_ok" in result["output"]


def test_a_failing_test_reports_the_failure(tmp_path):
    (tmp_path / "test_recorded.py").write_text(FAILING, encoding="utf-8")
    result = run_pytest(tmp_path, python=sys.executable, env=_env())
    assert result["ok"] is False
    assert result["exit_code"] != 0
    assert "test_no" in result["output"]


def test_a_missing_test_says_so_rather_than_erroring(tmp_path):
    result = run_pytest(tmp_path, python=sys.executable, env=_env())
    assert result["ok"] is False
    assert "does not exist" in result["output"]


def test_no_display_is_reported_as_the_real_problem(tmp_path):
    """Otherwise it surfaces as an opaque Qt abort, which sends the operator
    looking at the recording rather than at their session."""
    (tmp_path / "test_recorded.py").write_text(PASSING, encoding="utf-8")
    result = run_pytest(tmp_path, python=sys.executable,
                        env={"PATH": os.environ.get("PATH", "")})
    assert result["ok"] is False
    assert "DISPLAY" in result["output"]
    assert NO_DISPLAY.split(",")[0] in result["output"]


def test_a_hanging_test_is_stopped_and_explained(tmp_path):
    (tmp_path / "test_recorded.py").write_text(
        "import time\n\ndef test_hangs():\n    time.sleep(30)\n",
        encoding="utf-8")
    result = run_pytest(tmp_path, python=sys.executable, timeout=2.0,
                        env=_env())
    assert result["ok"] is False
    assert "did not finish" in result["output"]


def test_output_is_capped_from_the_end():
    """pytest puts the failure last, so the tail is the half worth keeping."""
    text = "".join(f"line {i}\n" for i in range(10000))
    trimmed = tail(text, limit=500)
    assert len(trimmed) < 600
    assert trimmed.endswith("line 9999\n")
    assert trimmed.startswith("...")


def test_short_output_is_left_alone():
    assert tail("brief") == "brief"


# --- what a failed replay is read for ---------------------------------------

def _pytest_report(app_lines: int) -> str:
    """A failing pytest run of an application that will not stop logging."""
    noise = "".join(
        "[Mako3.SocketComm] WARN : Connection refused, attempt {}\n".format(i)
        for i in range(app_lines))
    return (
        "=================================== FAILURES ==================="
        "================\n"
        "_____________________________ test_recorded_session ____________"
        "________________\n"
        "E   AssertionError: none of these found a usable object after 20s:\n"
        "E     {'objectName': 'openCase'}\n"
        "----------------------------- Captured stdout call -------------"
        "----------------\n"
        + noise +
        "=========================== short test summary info ============"
        "================\n"
        "FAILED test_recorded.py::test_recorded_session - AssertionError\n")


def test_the_application_log_does_not_push_out_the_failure():
    """The whole point of reading a replay. An application retrying a socket
    once a second wrote 20000 characters of warnings while one step waited, and
    the tail kept those and cut the assertion off mid-word -- so the report said
    nothing at all about which object was not found."""
    condensed = condense(_pytest_report(5000))

    assert "none of these found a usable object" in condensed
    assert "{'objectName': 'openCase'}" in condensed
    assert len(tail(condensed)) < MAX_OUTPUT       # nothing is trimmed away
    # The end of the application's own log survives: it is where it says why it
    # gave up, when that is the failure.
    assert "attempt 4999" in condensed
    assert "attempt 0\n" not in condensed
    assert "omitted" in condensed


def test_pytests_own_report_is_never_condensed():
    """Only what the application wrote. A short run passes through untouched."""
    report = ("=================================== FAILURES ==============="
              "====================\n"
              "E   AssertionError: none of these found a usable object\n"
              "=========================== short test summary info ========"
              "====================\n")
    assert condense(report) == report


def test_a_quiet_application_keeps_every_line_it_wrote():
    report = _pytest_report(5)
    assert condense(report) == report


# --- through the agent ------------------------------------------------------

class StubController:
    """Just enough controller for the agent to hold a session."""

    class _State:
        value = "stopped"

    def __init__(self):
        self.state = self._State()
        self.recording = None
        self.on_picked = None
        self.on_error = None

    def poll(self):
        return 0


def _session(tmp_path, monkeypatch):
    from qat_recorder.agent.server import Agent

    monkeypatch.setenv("QATREC_SESSIONS", str(tmp_path))
    agent = Agent(token="t", controller_factory=lambda **_: StubController())
    from qat_recorder.agent.server import Session
    session = Session(StubController(), owner="tester", app="sample")
    agent.session = session
    return agent, session


def test_the_agent_replays_in_the_sessions_own_directory(tmp_path, monkeypatch):
    agent, session = _session(tmp_path, monkeypatch)
    directory = session.directory()
    directory.mkdir(parents=True)
    (directory / "test_recorded.py").write_text(PASSING, encoding="utf-8")

    monkeypatch.setenv("DISPLAY", ":0")
    result = agent.replay(session.id)
    assert result["ok"] is True
    assert result["directory"] == str(directory)


def test_the_agent_refuses_to_replay_while_still_recording(tmp_path, monkeypatch):
    from qat_recorder.agent.protocol import AgentError

    agent, session = _session(tmp_path, monkeypatch)
    session.controller.state.value = "recording"
    with pytest.raises(AgentError, match="stop the recording"):
        agent.replay(session.id)


def test_each_session_gets_its_own_directory(tmp_path, monkeypatch):
    from qat_recorder.agent.server import Session

    monkeypatch.setenv("QATREC_SESSIONS", str(tmp_path))
    first = Session(StubController(), owner="a", app="sample")
    second = Session(StubController(), owner="b", app="sample")
    assert first.directory() != second.directory()
