# -*- coding: utf-8 -*-
"""
QML embedded in a widget layout the *other* way: QQuickView wrapped by
QWidget.createWindowContainer, instead of QQuickWidget.

This is the question that decides whether mixing QML into an existing widget
application is testable at all. QQuickWidget renders the scene into an internal,
non-top-level window that Qat cannot traverse; createWindowContainer keeps a real
QQuickWindow, which Qat should be able to see.

STILL UNVERIFIED. This is the single most valuable thing to run against a real
setup if your applications embed QML with QQuickWidget.
"""

import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtQuick import QQuickView
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QPushButton,
)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("QatQmlContainerSample")

    window = QMainWindow()
    window.setObjectName("mainWindow")

    root = QWidget()
    root.setObjectName("rootWidget")
    layout = QVBoxLayout(root)

    button = QPushButton("Widget button")
    button.setObjectName("widgetButton")
    layout.addWidget(button)

    view = QQuickView()
    view.setObjectName("embeddedQuickWindow")
    view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
    view.setSource(QUrl.fromLocalFile(str(Path(__file__).with_name("scene.qml"))))

    container = QWidget.createWindowContainer(view, root)
    container.setObjectName("qmlContainer")
    container.setMinimumHeight(140)
    layout.addWidget(container)

    window.setCentralWidget(root)
    window.resize(420, 260)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
