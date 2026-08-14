# -*- coding: utf-8 -*-
"""
The control panel.

Deliberately thin: every decision lives in `RecorderController`, which is tested
without a display. This module wires widgets to it and does no logic of its own
beyond presentation.

It runs as a separate process from the application under test, which matters —
the event filter lives inside the application, so operating this window is never
mistaken for input to the thing being recorded.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from qat_recorder.ir import Action, ActionKind, Robustness
from qat_recorder.ui.controller import ControllerError, RecorderController, State


def _is_step(action: Action) -> bool:
    """Whether an action is something the operator did, rather than bookkeeping."""
    return action.kind is not ActionKind.LAUNCH


def _registered_hosts() -> list:
    """Registered VMs, if any. A broken registry must not stop the panel
    opening — the operator can still record locally."""
    try:
        from qat_recorder.agent.registry import Registry
        return Registry.load().names()
    except Exception:                                        # noqa: BLE001
        return []

#: Robustness is the one thing an operator must not have to read carefully, so it
#: is encoded as colour as well as text.
#:
#: Two palettes, because one is not enough: saturated mid-tones chosen against a
#: light background turn nearly unreadable on a dark one, and vice versa. The
#: panel inherits the desktop theme, so it cannot assume either.
ROBUSTNESS_COLOURS_LIGHT = {
    Robustness.STRONG: "#1a7f5a",
    Robustness.MODERATE: "#2f6f9f",
    Robustness.WEAK: "#a8710a",
    Robustness.FRAGILE: "#b4451a",
    Robustness.UNRESOLVED: "#8c1f1f",
}

ROBUSTNESS_COLOURS_DARK = {
    Robustness.STRONG: "#5fd3aa",
    Robustness.MODERATE: "#7cc0f0",
    Robustness.WEAK: "#f0b95c",
    Robustness.FRAGILE: "#ff8b5e",
    Robustness.UNRESOLVED: "#ff7b7b",
}


def robustness_colour(robustness: Robustness) -> str:
    """Pick the variant that is legible against the current theme."""
    app = QApplication.instance()
    dark = False
    if app is not None:
        dark = app.palette().window().color().lightness() < 128
    palette = ROBUSTNESS_COLOURS_DARK if dark else ROBUSTNESS_COLOURS_LIGHT
    return palette[robustness]

POLL_INTERVAL_MS = 200


class CheckpointDialog(QDialog):
    """Choose which property of the picked object to assert on."""

    def __init__(self, label: str, properties: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add checkpoint")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Checking <b>{label}</b>"))

        form = QFormLayout()
        self.property_box = QComboBox()
        interesting = [key for key in ("text", "title", "checked", "enabled",
                                       "visible", "currentText", "value")
                       if key in properties]
        others = sorted(key for key in properties if key not in interesting)
        for key in interesting + others:
            self.property_box.addItem(key)
        form.addRow("Property", self.property_box)

        self.expected = QLineEdit()
        form.addRow("Expected value", self.expected)
        layout.addLayout(form)

        self._properties = properties
        self.property_box.currentTextChanged.connect(self._prefill)
        self._prefill(self.property_box.currentText())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _prefill(self, name: str) -> None:
        value = self._properties.get(name)
        self.expected.setText("" if value is None else str(value))

    def selection(self):
        return self.property_box.currentText(), self.expected.text()


class RecorderPanel(QMainWindow):
    def __init__(self, controller: Optional[RecorderController] = None,
                 lib_path: str = "", app_path: str = "", app_name: str = ""):
        super().__init__()
        self.setWindowTitle("QAT Recorder")
        self.resize(900, 620)

        self.controller = controller
        self._injected = controller is not None
        self._picking_hint_shown = False
        self._browse_buttons = []

        self._build_toolbar()
        self._build_body(lib_path, app_path, app_name)
        self.statusBar().showMessage("Ready")

        if controller is not None:
            self._attach(controller)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

        self._refresh_controls()

    # -- construction ------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = self.addToolBar("Session")
        bar.setMovable(False)

        self.act_record = QAction("Record", self)
        self.act_pause = QAction("Pause", self)
        self.act_stop = QAction("Stop", self)
        self.act_checkpoint = QAction("Add checkpoint", self)
        self.act_undo = QAction("Undo last", self)
        self.act_save = QAction("Save…", self)
        self.act_replay = QAction("Replay", self)
        self.act_replay.setToolTip(
            "Run the recording where the application is. On a remote host that "
            "is the host, not this machine.")

        self.act_record.triggered.connect(self.start_recording)
        self.act_pause.triggered.connect(self.toggle_pause)
        self.act_stop.triggered.connect(self.stop_recording)
        self.act_checkpoint.triggered.connect(self.arm_checkpoint)
        self.act_undo.triggered.connect(self.undo_last)
        self.act_save.triggered.connect(self.save_session)
        self.act_replay.triggered.connect(self.replay_session)

        for action in (self.act_record, self.act_pause, self.act_stop):
            bar.addAction(action)
        bar.addSeparator()
        for action in (self.act_checkpoint, self.act_undo):
            bar.addAction(action)
        bar.addSeparator()
        bar.addAction(self.act_save)
        bar.addAction(self.act_replay)

    def _build_body(self, lib_path: str, app_path: str, app_name: str) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)

        # -- what to record ------------------------------------------------
        setup = QWidget()
        form = QFormLayout(setup)
        form.setContentsMargins(0, 0, 0, 6)

        self.host_box = QComboBox()
        self.host_box.addItem("this machine", "")
        for name in _registered_hosts():
            self.host_box.addItem(name, name)
        self.host_box.currentIndexChanged.connect(self._host_changed)
        form.addRow("Record on", self.host_box)

        self.field_app = QLineEdit(app_path)
        self.field_lib = QLineEdit(lib_path)
        self.field_name = QLineEdit(app_name)
        self.field_name.setPlaceholderText("name used in the generated test")

        form.addRow("Application", self._with_browse(self.field_app))
        form.addRow("Filter library", self._with_browse(self.field_lib))
        form.addRow("Record as", self.field_name)
        outer.addWidget(setup)

        # -- what was recorded ---------------------------------------------
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "Action", "Object", "Durability"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.currentCellChanged.connect(lambda *_: self._show_details())
        splitter.addWidget(self.table)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setFont(QFont("monospace"))
        self.details.setPlaceholderText("Select a step to see how it was identified")
        splitter.addWidget(self.details)
        splitter.setSizes([560, 340])

        outer.addWidget(splitter, 1)

        self.counters = QLabel("")
        self.counters.setTextFormat(Qt.TextFormat.RichText)
        outer.addWidget(self.counters)

        self.setCentralWidget(central)

    def _with_browse(self, field: QLineEdit) -> QWidget:
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(field, 1)
        button = QPushButton("Browse…")
        button.clicked.connect(lambda: self._browse_into(field))
        row.addWidget(button)
        self._browse_buttons.append(button)
        return wrapper

    def _browse_into(self, field: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select file", field.text())
        if path:
            field.setText(path)

    # -- controller wiring -------------------------------------------------

    def _attach(self, controller) -> None:
        self.controller = controller
        controller.on_action = self._on_action
        controller.on_state = lambda _state: self._refresh_controls()
        controller.on_picked = self._on_picked
        controller.on_error = lambda message: self.statusBar().showMessage(message)

        # Where the application runs is the operator's most important piece of
        # context: the paths in this form mean different filesystems depending
        # on the answer, and browsing for them locally is meaningless when the
        # application is on a VM.
        location = getattr(controller, "location", "")
        self.setWindowTitle(f"QAT Recorder — {location}" if location
                            else "QAT Recorder")
        remote = bool(location) and location != "this machine"
        for button in self._browse_buttons:
            button.setEnabled(not remote)
            if remote:
                button.setToolTip("Paths refer to the remote host")

    def _host_changed(self) -> None:
        """Switching host means a different controller next time Record is
        pressed; drop the old one rather than reuse it against a new machine."""
        if self._injected:
            return
        if self.controller is not None and self.controller.state in (
                State.IDLE, State.STOPPED):
            self.controller = None
        name = self.host_box.currentData()
        self.setWindowTitle(f"QAT Recorder — {name}" if name
                            else "QAT Recorder — this machine")
        for button in self._browse_buttons:
            button.setEnabled(not name)

    def _make_controller(self):
        selected = self.host_box.currentData()
        if selected:
            try:
                controller = build_remote_controller(selected)
            except Exception as error:                       # noqa: BLE001
                self._warn(f"Could not connect to {selected}.\n\n{error}")
                return None
            self._attach(controller)
            return controller

        try:
            import qat
        except ImportError:
            self._warn("Qat is not installed in this environment.")
            return None

        app_path = self.field_app.text().strip()
        lib_path = self.field_lib.text().strip()
        if not app_path or not Path(app_path).exists():
            self._warn("Choose the application to record.")
            return None
        if not lib_path or not Path(lib_path).exists():
            self._warn("Choose the qatrec filter library (libqatrec.*.so).")
            return None

        controller = RecorderController(
            qat, lib_path=lib_path, app_path=app_path,
            app_name=self.field_name.text().strip() or Path(app_path).name)
        self._attach(controller)
        return controller

    # -- actions -----------------------------------------------------------

    def start_recording(self) -> None:
        # Rebuild from the form on a fresh run, since the paths may have changed.
        # An injected controller (tests, embedding) is always reused as given.
        needs_controller = self.controller is None or (
            not self._injected and self.controller.state is State.STOPPED)
        if needs_controller and self._make_controller() is None:
            return

        # The panel does not know, and does not need to know, whether the
        # application runs here or on a VM. It hands over the paths the operator
        # typed; the controller decides what they mean.
        configure = getattr(self.controller, "configure", None)
        if callable(configure):
            configure(self.field_app.text().strip(),
                      self.field_lib.text().strip(),
                      self.field_name.text().strip())

        self.table.setRowCount(0)
        self.details.clear()
        try:
            self.controller.start()
        except Exception as error:                            # noqa: BLE001
            self._warn(f"Could not start recording.\n\n{error}")
            return
        self._timer.start()
        self.statusBar().showMessage("Recording — interact with the application")

    def toggle_pause(self) -> None:
        if self.controller is None:
            return
        try:
            if self.controller.state is State.PAUSED:
                self.controller.resume()
                self.statusBar().showMessage("Recording")
            else:
                self.controller.pause()
                self.statusBar().showMessage(
                    "Paused — anything you do now is discarded")
        except ControllerError as error:
            self.statusBar().showMessage(str(error))

    def stop_recording(self) -> None:
        if self.controller is None:
            return
        self._timer.stop()
        try:
            self.controller.stop()
        except ControllerError as error:
            self.statusBar().showMessage(str(error))
            return
        summary = self.controller.summary()
        self.statusBar().showMessage(
            f"Stopped — {summary['actions']} step(s) recorded")
        self._refresh_controls()
        self._update_counters()

    def arm_checkpoint(self) -> None:
        if self.controller is None:
            return
        try:
            self.controller.arm_checkpoint()
            self.statusBar().showMessage(
                "Click the object you want to check — that click is not recorded")
        except ControllerError as error:
            self.statusBar().showMessage(str(error))

    def undo_last(self) -> None:
        if self.controller is None:
            return
        removed = self.controller.drop_last_action()
        if removed is None:
            self.statusBar().showMessage("Nothing to undo")
            return
        if self.table.rowCount():
            self.table.removeRow(self.table.rowCount() - 1)
        self.statusBar().showMessage(f"Removed {removed.kind.value}")
        self._update_counters()

    def save_session(self) -> None:
        if self.controller is None:
            return
        directory = QFileDialog.getExistingDirectory(self, "Save recording into")
        if not directory:
            return
        try:
            written = self.controller.save(directory)
        except Exception as error:                            # noqa: BLE001
            self._warn(f"Could not save.\n\n{error}")
            return
        self.statusBar().showMessage(f"Wrote {len(written)} file(s) to {directory}")

    def replay_session(self) -> None:
        """Run what was just recorded, where the application is.

        Blocking on purpose. A replay is a single question with a yes or no
        answer, and the operator is asking it deliberately -- a progress dialog
        that could be cancelled halfway would leave an application running on
        someone else's screen.
        """
        if self.controller is None:
            return
        self.statusBar().showMessage("Replaying…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = self.controller.replay()
        except Exception as error:                            # noqa: BLE001
            self._warn(f"Could not replay.\n\n{error}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        verdict = "passed" if result.get("ok") else "FAILED"
        self.statusBar().showMessage(f"Replay {verdict}")
        self.details.setPlainText(
            f"replay {verdict}   (exit code {result.get('exit_code')})\n"
            f"in {result.get('directory', '?')}\n\n"
            + (result.get("output") or ""))

    # -- callbacks ---------------------------------------------------------

    def _tick(self) -> None:
        if self.controller is None:
            return
        try:
            self.controller.poll()
        except Exception as error:                            # noqa: BLE001
            self._timer.stop()
            self._warn(f"Recording stopped.\n\n{error}")
            return
        self._update_counters()

    def _on_action(self, action: Action) -> None:
        # LAUNCH is bookkeeping, not a step the operator performed. Locally it is
        # created before callbacks are attached so it never arrives here; over
        # the wire it does. Skipping it explicitly keeps the two identical, and
        # keeps table rows aligned with `_steps()` for the detail pane.
        if not _is_step(action):
            return

        row = self.table.rowCount()
        self.table.insertRow(row)

        target = action.target
        robustness = target.robustness if target else Robustness.STRONG
        cells = [
            str(row + 1),
            action.kind.value,
            target.label if target else "-",
            robustness.value if target else "",
        ]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if column == 3 and target is not None:
                item.setForeground(QColor(robustness_colour(robustness)))
            self.table.setItem(row, column, item)
        self.table.scrollToBottom()

    def _on_picked(self, target, properties: dict) -> None:
        dialog = CheckpointDialog(target.label, properties, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.statusBar().showMessage("Checkpoint cancelled")
            return
        name, expected = dialog.selection()
        try:
            self.controller.add_checkpoint(name, expected)
        except ControllerError as error:
            self.statusBar().showMessage(str(error))
            return
        self.statusBar().showMessage(f"Checkpoint on {target.label}.{name}")

    # -- presentation ------------------------------------------------------

    def _show_details(self) -> None:
        recording = self.controller.recording if self.controller else None
        if recording is None:
            return
        row = self.table.currentRow()
        # Same predicate the table uses, so row N is always step N.
        steps = [a for a in recording.actions if _is_step(a)]
        if row < 0 or row >= len(steps):
            self.details.clear()
            return
        action = steps[row]
        lines = [
            f"action      {action.kind.value}",
            f"arguments   {action.args}",
            f"at          {action.t:.2f}s",
        ]
        if action.target is not None:
            lines.extend([
                "",
                f"object      {action.target.label}",
                f"strategy    {action.target.strategy}",
                f"durability  {action.target.robustness.value}",
                f"definition  {action.target.definition}",
            ])
            if action.target.index is not None:
                lines.append(f"index       {action.target.index}  "
                             "(positional: order-dependent)")
            if action.target.warnings:
                lines.append("")
                for warning in action.target.warnings:
                    lines.append(f"warning     {warning}")
        if action.note:
            lines.append("")
            lines.append(f"note        {action.note}")
        self.details.setPlainText("\n".join(lines))

    def _update_counters(self) -> None:
        if self.controller is None:
            self.counters.setText("")
            return
        summary = self.controller.summary()
        parts = [
            f"steps <b>{summary['actions']}</b>",
            f"events {summary['events']}",
        ]
        if summary["fragile"]:
            parts.append(f"<span style='color:{robustness_colour(Robustness.FRAGILE)}'>"
                         f"needs review <b>{summary['fragile']}</b></span>")
        if summary["unresolved"]:
            parts.append(
                f"<span style='color:{robustness_colour(Robustness.UNRESOLVED)}'>"
                f"unidentified {summary['unresolved']}</span>")
        if summary["secrets"]:
            parts.append(f"redacted {summary['secrets']}")
        if summary["dropped"]:
            parts.append(f"discarded {summary['dropped']}")
        self.counters.setText(" &nbsp;·&nbsp; ".join(parts))

    def _refresh_controls(self) -> None:
        state = self.controller.state if self.controller else State.IDLE
        recording = state in (State.RECORDING, State.PICKING)

        self.act_record.setEnabled(state in (State.IDLE, State.STOPPED))
        self.act_pause.setEnabled(state in (State.RECORDING, State.PAUSED))
        self.act_pause.setText("Resume" if state is State.PAUSED else "Pause")
        self.act_stop.setEnabled(state is not State.IDLE and state is not State.STOPPED)
        self.act_checkpoint.setEnabled(state is State.RECORDING)
        self.act_undo.setEnabled(recording or state is State.STOPPED)
        self.act_save.setEnabled(state is State.STOPPED)
        # Only once recording has stopped: a replay launches its own copy of the
        # application, and two instances fighting over one screen tests nothing.
        self.act_replay.setEnabled(state is State.STOPPED)

        idle = state in (State.IDLE, State.STOPPED)
        for field in (self.field_app, self.field_lib, self.field_name):
            field.setEnabled(idle)
        # Switching host mid-session would be meaningless, and switching after
        # stopping must not happen before the recording has been saved.
        self.host_box.setEnabled(idle and not self._injected)

        if state is State.PICKING:
            self.statusBar().showMessage(
                "Click the object you want to check — that click is not recorded")
        elif self._picking_hint_shown:
            # Leaving PICKING: clear the prompt rather than leave a stale
            # instruction on screen for the rest of the session.
            self.statusBar().clearMessage()
        self._picking_hint_shown = state is State.PICKING

    def _warn(self, message: str) -> None:
        QMessageBox.warning(self, "QAT Recorder", message)

    def closeEvent(self, event):                             # noqa: N802
        self._timer.stop()
        if self.controller is not None and self.controller.state in (
                State.RECORDING, State.PAUSED, State.PICKING):
            try:
                self.controller.stop()
            except Exception:                                # noqa: BLE001
                pass
        super().closeEvent(event)


def build_remote_controller(agent: str, owner: str = ""):
    """Connect to an agent on a VM, by registered name or bare address.

    Paths shown in the panel then refer to that host's filesystem, not this one.
    """
    from qat_recorder.agent.registry import Registry, client_for
    from qat_recorder.agent.remote import RemoteRecorderController

    host = Registry.load().resolve(agent)
    return RemoteRecorderController(client_for(host, owner=owner))


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="qat-recorder-panel")
    parser.add_argument("--lib", default="", help="path to libqatrec.*.so")
    parser.add_argument("--app", default="", help="path to the application")
    parser.add_argument("--name", default="", help="name used in generated code")
    parser.add_argument("--agent", default="",
                        help="record on a remote host: a registered name, or "
                             "HOST[:PORT]")
    args = parser.parse_args(argv)

    controller = None
    if args.agent:
        try:
            controller = build_remote_controller(args.agent)
        except Exception as error:                           # noqa: BLE001
            print(f"could not set up the remote connection: {error}",
                  file=sys.stderr)
            return 2

    app = QApplication(sys.argv[:1])
    panel = RecorderPanel(controller=controller, lib_path=args.lib,
                          app_path=args.app, app_name=args.name)
    panel.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
