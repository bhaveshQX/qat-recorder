# -*- coding: utf-8 -*-
"""
Representative application under test.

Deliberately includes the cases that stress an object-name resolver:
  - well-named widgets (objectName set)
  - unnamed widgets distinguishable only by text
  - two unnamed widgets with IDENTICAL type and text (forces ambiguity)
  - a password field (echoMode != Normal) for the credential-redaction rule
  - a burst signal source for callback-throughput measurement
  - an embedded QML scene, with one named and one unnamed item
"""

import sys
from pathlib import Path

from PySide6.QtCore import Signal, Slot, QUrl
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QCheckBox, QLineEdit, QComboBox, QLabel, QGroupBox,
)


class MainWindow(QMainWindow):
    """Main window of the sample application."""

    pinged = Signal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName("mainWindow")
        self.setWindowTitle("Qat Spike Sample")

        root = QWidget()
        root.setObjectName("rootWidget")
        outer = QVBoxLayout(root)

        # --- Named widgets -------------------------------------------------
        form_box = QGroupBox("Credentials")
        form_box.setObjectName("credentialsGroup")
        form = QFormLayout(form_box)

        self.username = QLineEdit()
        self.username.setObjectName("usernameField")
        form.addRow("User", self.username)

        self.password = QLineEdit()
        self.password.setObjectName("passwordField")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password", self.password)

        self.env = QComboBox()
        self.env.setObjectName("envSelector")
        self.env.addItems(["staging", "production", "local"])
        form.addRow("Environment", self.env)

        self.remember = QCheckBox("Remember me")
        self.remember.setObjectName("rememberBox")
        form.addRow("", self.remember)

        outer.addWidget(form_box)

        # --- Unnamed, distinguishable only by text --------------------------
        row1 = QHBoxLayout()
        for text in ("Import", "Export"):
            btn = QPushButton(text)          # no objectName on purpose
            row1.addWidget(btn)
        outer.addLayout(row1)

        # --- Unnamed AND identical: the ambiguity case ----------------------
        ambiguous = QGroupBox("Duplicate controls")
        ambiguous.setObjectName("duplicateGroup")
        row2 = QHBoxLayout(ambiguous)
        for _ in range(2):
            btn = QPushButton("Apply")       # same type, same text, no name
            row2.addWidget(btn)
        outer.addWidget(ambiguous)

        # --- Named action + status -----------------------------------------
        self.login = QPushButton("Sign in")
        self.login.setObjectName("loginButton")
        self.login.clicked.connect(self._on_login)
        outer.addWidget(self.login)

        self.status = QLabel("ready")
        self.status.setObjectName("statusLabel")
        outer.addWidget(self.status)

        # --- QML scene ------------------------------------------------------
        try:
            from PySide6.QtQuickWidgets import QQuickWidget
            quick = QQuickWidget()
            quick.setObjectName("quickView")
            quick.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
            quick.setMinimumHeight(130)
            quick.setSource(QUrl.fromLocalFile(
                str(Path(__file__).with_name("scene.qml"))))
            outer.addWidget(quick)
        except Exception as error:                      # noqa: BLE001
            note = QLabel(f"QML unavailable: {error}")
            note.setObjectName("qmlErrorLabel")
            outer.addWidget(note)

        self.setCentralWidget(root)
        self.resize(460, 620)

    @Slot()
    def _on_login(self):
        self.status.setText(f"signed in as {self.username.text() or 'anonymous'}")

    @Slot(int)
    def burst(self, count: int):
        """Emit `pinged` `count` times as fast as possible (throughput probe)."""
        for i in range(count):
            self.pinged.emit(i)

    @Slot()
    def toggle_remember_programmatically(self):
        """Flip the checkbox from code — no user input involved.

        This is the signal-vs-user-input discrimination case: it fires exactly
        the same signal a real click would.
        """
        self.remember.setChecked(not self.remember.isChecked())


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("QatSpikeSample")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
