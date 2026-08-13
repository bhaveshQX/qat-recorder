# -*- coding: utf-8 -*-
"""
The same QML scene, but as a top-level QQuickView instead of embedded in a
QQuickWidget. Determines which QML embedding style Qat can actually see.

Measured answer: this one is fully visible; QQuickWidget is not.
"""

import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickView


def main():
    app = QGuiApplication(sys.argv)
    app.setApplicationName("QatQmlWindowSample")
    view = QQuickView()
    view.setObjectName("quickWindow")
    view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
    view.setSource(QUrl.fromLocalFile(str(Path(__file__).with_name("scene.qml"))))
    view.resize(400, 160)
    view.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
