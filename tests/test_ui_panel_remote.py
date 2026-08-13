# -*- coding: utf-8 -*-
"""
The panel driven by a remote controller, headless.

This is the claim Phase 4's design made and Phase 6.2 cashes in: the panel holds
no knowledge of where the application runs, so pointing it at an agent required
no change to any widget. These tests exercise the same panel code as
test_ui_panel.py, with a real agent on a real socket behind it.
"""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from qat_recorder.agent.client import AgentClient                 # noqa: E402
from qat_recorder.agent.remote import RemoteRecorderController    # noqa: E402
from qat_recorder.ir import Robustness                            # noqa: E402
from qat_recorder.ui.controller import State                      # noqa: E402
from qat_recorder.ui.panel import RecorderPanel                   # noqa: E402
from tests.test_agent import TOKEN, Harness                       # noqa: E402
from tests.test_capture import click_pair                         # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def remote_panel(qt_app):
    harness = Harness()
    client = AgentClient("127.0.0.1", harness.port, TOKEN, owner="alice",
                         insecure_plaintext=True, timeout=15)
    controller = RemoteRecorderController(client, poll_wait=1.0)
    panel = RecorderPanel(controller=controller)
    panel.field_app.setText("/opt/demo/bin/demo")
    panel.field_lib.setText("/opt/qatrec/libqatrec.6.4.so")
    panel.field_name.setText("demo")
    panel._harness = harness
    yield panel
    panel._timer.stop()
    try:
        if controller.state not in (State.IDLE, State.STOPPED):
            controller.stop()
    finally:
        harness.close()
        panel.deleteLater()


def tick_until(panel, until, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        panel._tick()
        if until():
            return True
        time.sleep(0.1)
    panel._tick()
    return until()


# --- the panel does not know it is remote ----------------------------------

def test_title_names_the_remote_host(remote_panel):
    assert "127.0.0.1" in remote_panel.windowTitle()


def test_browse_buttons_are_disabled_for_a_remote_host(remote_panel):
    """Local file dialogs would be lying: those paths are on the VM."""
    assert remote_panel._browse_buttons
    assert all(not button.isEnabled()
               for button in remote_panel._browse_buttons)


def test_recording_starts_on_the_agent(remote_panel):
    remote_panel.start_recording()
    assert remote_panel.controller.state is State.RECORDING
    assert remote_panel.controller.session_id
    assert not remote_panel.act_record.isEnabled()
    assert remote_panel.act_stop.isEnabled()


def test_the_paths_typed_in_the_form_reach_the_agent(remote_panel):
    remote_panel.start_recording()
    assert remote_panel.controller.app_path == "/opt/demo/bin/demo"
    assert remote_panel.controller.app_name == "demo"


def test_remote_actions_populate_the_table(remote_panel):
    remote_panel.start_recording()
    remote_panel._harness.push(*click_pair(1000, "QPushButton", "loginButton"))

    assert tick_until(remote_panel, lambda: remote_panel.table.rowCount() >= 1)
    row = remote_panel.table.rowCount() - 1
    assert remote_panel.table.item(row, 1).text() == "click"
    assert remote_panel.table.item(row, 2).text() == "loginButton"
    assert remote_panel.table.item(row, 3).text() == Robustness.STRONG.value


def test_details_pane_works_over_the_wire(remote_panel):
    remote_panel.start_recording()
    remote_panel._harness.push(*click_pair(
        1000, "QPushButton", text="Apply", index=1,
        path=[("QGroupBox", "duplicateGroup")]))
    assert tick_until(remote_panel, lambda: remote_panel.table.rowCount() >= 1)

    remote_panel.table.setCurrentCell(0, 0)
    text = remote_panel.details.toPlainText()
    assert "durability  fragile" in text
    assert "order-dependent" in text


def test_counters_update_from_the_agent(remote_panel):
    remote_panel.start_recording()
    remote_panel._harness.push(*click_pair(1000, "QPushButton", "loginButton"))
    assert tick_until(remote_panel, lambda: remote_panel.table.rowCount() >= 1)
    assert "steps" in remote_panel.counters.text()


def test_pause_and_stop_drive_the_agent(remote_panel):
    remote_panel.start_recording()
    remote_panel.toggle_pause()
    assert remote_panel.controller.state is State.PAUSED
    assert remote_panel.act_pause.text() == "Resume"

    remote_panel.toggle_pause()
    remote_panel.stop_recording()
    assert remote_panel.controller.state is State.STOPPED
    assert remote_panel.act_save.isEnabled()


def test_saving_pulls_the_files_back_from_the_vm(remote_panel, tmp_path):
    remote_panel.start_recording()
    remote_panel._harness.push(*click_pair(1000, "QPushButton", "loginButton"))
    tick_until(remote_panel, lambda: remote_panel.table.rowCount() >= 1)
    remote_panel.stop_recording()

    written = remote_panel.controller.save(str(tmp_path))
    assert len(written) == 4
    assert (tmp_path / "test_recorded.py").exists()
    assert "qat.mouse_click" in (tmp_path / "test_recorded.py").read_text(
        encoding="utf-8")


def test_a_busy_host_is_reported_in_the_ui(remote_panel, monkeypatch):
    """A second tester must be told who holds the machine, not shown a crash."""
    remote_panel.start_recording()

    other_client = AgentClient("127.0.0.1", remote_panel._harness.port, TOKEN,
                               owner="bob", insecure_plaintext=True, timeout=15)
    other = RemoteRecorderController(other_client, poll_wait=1.0)
    second = RecorderPanel(controller=other)
    second.field_app.setText("/opt/demo/bin/demo")
    second.field_lib.setText("/opt/qatrec/lib.so")

    warnings = []
    monkeypatch.setattr(second, "_warn", warnings.append)
    second.start_recording()

    assert warnings
    assert "alice" in warnings[0]
    assert second.controller.state is not State.RECORDING
    second.deleteLater()
