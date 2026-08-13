# -*- coding: utf-8 -*-
"""
A synthetic object tree mirroring `spike/sample_app.py`.

Kept structurally identical to the real sample application so the same naming
cases can be replayed against a live app in `test_conformance.py`.
"""

from qat_recorder.backend import FakeBackend, FakeNode

# Qt type hierarchies, most-derived first, so `type` matching is inheritance-aware.
WIDGET = ["QWidget", "QObject"]
BUTTON = ["QPushButton", "QAbstractButton"] + WIDGET
LINEEDIT = ["QLineEdit"] + WIDGET
COMBO = ["QComboBox"] + WIDGET
CHECK = ["QCheckBox", "QAbstractButton"] + WIDGET
GROUP = ["QGroupBox"] + WIDGET
LABEL = ["QLabel"] + WIDGET
WINDOW = ["QMainWindow"] + WIDGET


def build_tree():
    """Return (backend, index-of-interesting-nodes)."""
    window = FakeNode(WINDOW, {
        "objectName": "mainWindow", "windowTitle": "Qat Spike Sample"})
    root = window.add(FakeNode(WIDGET, {"objectName": "rootWidget"}))

    # A QAction, because Qt delivers QEvent::Shortcut to the action rather than
    # to a widget, and the recorder has to retarget away from it.
    save_action = window.add(FakeNode(["QAction", "QObject"], {
        "objectName": "saveAction", "text": "Save"}))

    creds = root.add(FakeNode(GROUP, {
        "objectName": "credentialsGroup", "title": "Credentials"}))

    # echoMode is the enum NAME string, matching what a live Qat server returns
    # (verified on Linux against Qt 6.11). Using ints here previously hid a bug
    # that stopped password fields being redacted.
    username = creds.add(FakeNode(LINEEDIT, {
        "objectName": "usernameField", "echoMode": "Normal"}))
    password = creds.add(FakeNode(LINEEDIT, {
        "objectName": "passwordField", "echoMode": "Password"}))
    env = creds.add(FakeNode(COMBO, {"objectName": "envSelector"}))
    remember = creds.add(FakeNode(CHECK, {
        "objectName": "rememberBox", "text": "Remember me"}))

    # Unnamed, distinguishable by text alone.
    import_btn = root.add(FakeNode(BUTTON, {"text": "Import"}))

    # Unnamed and duplicated across containers: unique only when scoped.
    export_outer = root.add(FakeNode(BUTTON, {"text": "Export"}))
    advanced = root.add(FakeNode(GROUP, {
        "objectName": "advancedGroup", "title": "Advanced"}))
    export_inner = advanced.add(FakeNode(BUTTON, {"text": "Export"}))

    # Unnamed, identical, and in the same container: not resolvable by properties.
    duplicates = root.add(FakeNode(GROUP, {
        "objectName": "duplicateGroup", "title": "Duplicate controls"}))
    apply_a = duplicates.add(FakeNode(BUTTON, {"text": "Apply"}))
    apply_b = duplicates.add(FakeNode(BUTTON, {"text": "Apply"}))

    login = root.add(FakeNode(BUTTON, {
        "objectName": "loginButton", "text": "Sign in"}))
    status = root.add(FakeNode(LABEL, {
        "objectName": "statusLabel", "text": "ready"}))

    nodes = {
        "window": window, "root": root, "creds": creds,
        "username": username, "password": password, "env": env,
        "remember": remember, "import_btn": import_btn,
        "export_outer": export_outer, "export_inner": export_inner,
        "advanced": advanced, "duplicates": duplicates,
        "apply_a": apply_a, "apply_b": apply_b,
        "login": login, "status": status, "save_action": save_action,
    }
    return FakeBackend([window]), nodes
