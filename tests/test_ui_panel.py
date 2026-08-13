# -*- coding: utf-8 -*-
"""
Smoke tests for the control panel, headless.

Runs under QT_QPA_PLATFORM=offscreen with an injected controller backed by the
synthetic tree, so no application is launched and no display is needed. The panel
holds no logic of its own, so these tests only check the wiring: does an action
reach the table, do the buttons reflect the state, does the detail pane explain
how an object was identified.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from qat_recorder.ir import Robustness                          # noqa: E402
from qat_recorder.ui.controller import RecorderController, State  # noqa: E402
from qat_recorder.ui.panel import CheckpointDialog, RecorderPanel  # noqa: E402
from tests.fixtures import build_tree                            # noqa: E402
from tests.test_capture import click_pair, event                 # noqa: E402
from tests.test_ui_controller import FakeQat, FakeReceiver       # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def panel(qt_app):
    backend, nodes = build_tree()
    nodes["username"].props["text"] = "alice"
    receiver = FakeReceiver()
    controller = RecorderController(
        FakeQat(), lib_path="/tmp/lib.so", app_path="/tmp/app",
        app_name="sample", backend=backend, receiver=receiver)
    widget = RecorderPanel(controller=controller)
    widget._test = {"receiver": receiver, "nodes": nodes}
    yield widget
    widget._timer.stop()
    widget.deleteLater()


# --- wiring ----------------------------------------------------------------

def test_panel_starts_idle_with_only_record_enabled(panel):
    assert panel.act_record.isEnabled()
    assert not panel.act_stop.isEnabled()
    assert not panel.act_checkpoint.isEnabled()
    assert not panel.act_save.isEnabled()


def test_recording_enables_the_right_controls(panel):
    panel.start_recording()
    assert panel.controller.state is State.RECORDING
    assert not panel.act_record.isEnabled()
    assert panel.act_stop.isEnabled()
    assert panel.act_checkpoint.isEnabled()
    assert panel.field_app.isEnabled() is False       # locked while recording


def test_an_action_reaches_the_table(panel):
    panel.start_recording()
    panel._test["receiver"].push(*click_pair(1000, "QPushButton", "loginButton"))
    panel._tick()

    assert panel.table.rowCount() == 1
    assert panel.table.item(0, 1).text() == "click"
    assert panel.table.item(0, 2).text() == "loginButton"
    assert panel.table.item(0, 3).text() == Robustness.STRONG.value


def test_fragile_steps_are_coloured_differently(panel):
    panel.start_recording()
    panel._test["receiver"].push(
        *click_pair(1000, "QPushButton", text="Apply", index=0,
                    path=[("QGroupBox", "duplicateGroup")]))
    panel._tick()

    assert panel.table.item(0, 3).text() == Robustness.FRAGILE.value
    strong = panel.table.item(0, 3).foreground().color().name()
    assert strong != "#000000"


def test_details_pane_explains_how_the_object_was_identified(panel):
    panel.start_recording()
    panel._test["receiver"].push(*click_pair(1000, "QPushButton", "loginButton"))
    panel._tick()
    panel.table.setCurrentCell(0, 0)

    text = panel.details.toPlainText()
    assert "objectName" in text
    assert "durability  strong" in text
    assert "strategy" in text


def test_details_pane_shows_the_positional_warning(panel):
    panel.start_recording()
    panel._test["receiver"].push(
        *click_pair(1000, "QPushButton", text="Apply", index=1,
                    path=[("QGroupBox", "duplicateGroup")]))
    panel._tick()
    panel.table.setCurrentCell(0, 0)

    text = panel.details.toPlainText()
    assert "index" in text
    assert "order-dependent" in text
    assert "warning" in text


def test_pause_button_becomes_resume(panel):
    panel.start_recording()
    panel.toggle_pause()
    assert panel.controller.state is State.PAUSED
    assert panel.act_pause.text() == "Resume"
    panel.toggle_pause()
    assert panel.controller.state is State.RECORDING
    assert panel.act_pause.text() == "Pause"


def test_counters_report_review_items(panel):
    panel.start_recording()
    panel._test["nodes"]["password"].props["text"] = "hunter2"
    panel._test["receiver"].push(
        event("key_press", 500, "QLineEdit", "passwordField", key=ord("H")),
        *click_pair(1500, "QPushButton", text="Apply", index=0,
                    path=[("QGroupBox", "duplicateGroup")]))
    panel._tick()
    panel.stop_recording()

    text = panel.counters.text()
    assert "steps" in text
    assert "needs review" in text
    assert "redacted" in text


def test_undo_removes_the_row_as_well_as_the_action(panel):
    panel.start_recording()
    panel._test["receiver"].push(*click_pair(1000, "QPushButton", "loginButton"))
    panel._tick()
    assert panel.table.rowCount() == 1

    panel.undo_last()
    assert panel.table.rowCount() == 0
    assert panel.controller.summary()["actions"] == 0


def test_stopping_enables_save_and_disables_recording(panel):
    panel.start_recording()
    panel._test["receiver"].push(*click_pair(1000, "QPushButton", "loginButton"))
    panel._tick()
    panel.stop_recording()

    assert panel.controller.state is State.STOPPED
    assert panel.act_save.isEnabled()
    assert not panel.act_checkpoint.isEnabled()
    assert panel.act_record.isEnabled()
    assert panel.field_app.isEnabled()


def test_closing_while_recording_stops_cleanly(panel):
    panel.start_recording()
    panel.close()
    assert panel.controller.state is State.STOPPED


# --- host picker -----------------------------------------------------------

def test_host_picker_offers_local_first(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("QATREC_CONFIG_DIR", str(tmp_path))
    widget = RecorderPanel()
    try:
        assert widget.host_box.count() >= 1
        assert widget.host_box.itemText(0) == "this machine"
        assert widget.host_box.itemData(0) == ""
    finally:
        widget.deleteLater()


def test_registered_hosts_appear_in_the_picker(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("QATREC_CONFIG_DIR", str(tmp_path))
    from qat_recorder.agent.registry import Host, Registry

    registry = Registry.load(tmp_path / "hosts.json")
    registry.add(Host(name="vm-01", host="10.0.0.5", fingerprint="ab"))
    registry.add(Host(name="vm-02", host="10.0.0.6", fingerprint="cd"))
    registry.save()

    widget = RecorderPanel()
    try:
        entries = [widget.host_box.itemText(i)
                   for i in range(widget.host_box.count())]
        assert entries == ["this machine", "vm-01", "vm-02"]
    finally:
        widget.deleteLater()


def test_choosing_a_remote_host_disables_local_browsing(qt_app, tmp_path,
                                                        monkeypatch):
    """Those paths live on the VM; a local file dialog would be lying."""
    monkeypatch.setenv("QATREC_CONFIG_DIR", str(tmp_path))
    from qat_recorder.agent.registry import Host, Registry

    registry = Registry.load(tmp_path / "hosts.json")
    registry.add(Host(name="vm-01", host="10.0.0.5", fingerprint="ab"))
    registry.save()

    widget = RecorderPanel()
    try:
        assert all(b.isEnabled() for b in widget._browse_buttons)
        widget.host_box.setCurrentIndex(1)
        assert not any(b.isEnabled() for b in widget._browse_buttons)
        assert "vm-01" in widget.windowTitle()

        widget.host_box.setCurrentIndex(0)
        assert all(b.isEnabled() for b in widget._browse_buttons)
        assert "this machine" in widget.windowTitle()
    finally:
        widget.deleteLater()


def test_a_broken_registry_does_not_stop_the_panel_opening(qt_app, tmp_path,
                                                           monkeypatch):
    monkeypatch.setenv("QATREC_CONFIG_DIR", str(tmp_path))
    (tmp_path / "hosts.json").write_text("{ not json", encoding="utf-8")
    widget = RecorderPanel()
    try:
        assert widget.host_box.count() == 1        # local only, still usable
    finally:
        widget.deleteLater()


def test_host_picker_is_locked_while_recording(panel):
    panel.start_recording()
    assert not panel.host_box.isEnabled()


# --- checkpoint dialog -----------------------------------------------------

def test_checkpoint_dialog_prefills_the_current_value(qt_app):
    dialog = CheckpointDialog("statusLabel", {"text": "ready", "enabled": True})
    name, expected = dialog.selection()
    assert name == "text"                 # useful properties come first
    assert expected == "ready"
    dialog.deleteLater()


def test_checkpoint_dialog_updates_when_the_property_changes(qt_app):
    dialog = CheckpointDialog("statusLabel", {"text": "ready", "enabled": True})
    dialog.property_box.setCurrentText("enabled")
    name, expected = dialog.selection()
    assert name == "enabled"
    assert expected == "True"
    dialog.deleteLater()
