# -*- coding: utf-8 -*-
"""
What is in Qat's object tree, and what is not.

Run this when a recorded session produces steps that name objects Qat cannot
find -- the ones a replay reports as::

    {'objectName': 'roundButton'} -- no object in the application has this

The recorder's event filter reads those names *inside* the application, off the
QObject itself, so an object it names is certainly there. Qat reaches the same
application from outside, through its server, and the two do not always see the
same tree: QML embedded in a way Qat's plugin cannot traverse is invisible to
Qat while being perfectly visible to the filter, and to the person clicking it.

Nothing in the recorder can bridge that. Every call a generated test makes goes
through Qat, so an object Qat has no record of cannot be waited for, clicked or
read, and a recording of one produces a test that can only fail. This says which
side of that line an application is on, before anybody spends another ninety
seconds per step finding out.

    qat-recorder probe --launch /opt/mako/mako_shoulder.sh --pause \\
        roundButton openCase

`--pause` holds the application open so you can navigate to the screen the
session used -- an object that has not been created yet is absent for reasons
that have nothing to do with this question. Every name given after the options
is looked up on its own, so pass the ones the failing replay complained about.
"""

from __future__ import annotations

import sys

#: Asked for one at a time rather than walked, because the walk goes through
#: `children` and the whole question is whether the tree has a path to these at
#: all. A type Qat knows nothing about answers zero, which is the finding.
QML_TYPES = ("QQuickWindow", "QQuickView", "QQuickItem", "QQuickText",
             "QQuickRectangle", "QQuickLoader", "QQuickMouseArea")

WIDGET_TYPES = ("QWidget", "QMainWindow", "QDialog", "QPushButton", "QMenuBar")

#: Enough of the tree to recognise what kind of application this is, and short
#: enough to paste into a message.
WALK_LIMIT = 60


def definition_of(obj) -> dict:
    try:
        return dict(obj.get_definition() or {})
    except Exception as error:                               # noqa: BLE001
        return {"<unreadable>": str(error)}


def describe(obj) -> str:
    definition = definition_of(obj)
    return "{:<28} objectName={!r}".format(
        definition.get("type", "?"), definition.get("objectName", ""))


def count(qat, type_name: str, out) -> int:
    try:
        return len(qat.find_all_objects({"type": type_name}))
    except Exception as error:                               # noqa: BLE001
        out(f"    {type_name}: lookup failed: {error}")
        return -1


def walk(roots, limit: int = WALK_LIMIT) -> list:
    """Every object reachable from the top windows, breadth first."""
    seen = []
    queue = list(roots)
    while queue and len(seen) < limit:
        node = queue.pop(0)
        seen.append(node)
        try:
            children = node.children
        except Exception:                                    # noqa: BLE001
            continue
        if isinstance(children, list):
            queue.extend(children)
    return seen


def report(qat, names=(), out=print) -> dict:
    """Print what Qat has, and return the counts, so a caller can act on them."""
    out("\n--- top windows -------------------------------------------------")
    try:
        windows = list(qat.list_top_windows())
    except Exception as error:                               # noqa: BLE001
        windows = []
        out(f"  list_top_windows() failed: {error}")
    if not windows:
        out("  none. Qat has no root to search from, and nothing below will")
        out("  find anything either.")
    for window in windows:
        out("  " + describe(window))

    out("\n--- how many of each type Qat has -------------------------------")
    out("  QML:")
    qml_total = 0
    for name in QML_TYPES:
        found = count(qat, name, out)
        qml_total += max(0, found)
        out(f"    {name:<20} {found}")
    out("  widgets:")
    widget_total = 0
    for name in WIDGET_TYPES:
        found = count(qat, name, out)
        widget_total += max(0, found)
        out(f"    {name:<20} {found}")

    missing = []
    if names:
        out("\n--- the names the replay could not find -------------------------")
    for name in names:
        try:
            matches = qat.find_all_objects({"objectName": name})
        except Exception as error:                           # noqa: BLE001
            out(f"  {name!r}: lookup failed: {error}")
            continue
        if not matches:
            missing.append(name)
            out(f"  {name!r}: nothing. The filter read this name inside the "
                "application; Qat does not have the object.")
        else:
            for match in matches:
                out(f"  {name!r}: " + describe(match))

    out("\n--- what the tree actually holds --------------------------------")
    reachable = walk(windows)
    for node in reachable:
        out("  " + describe(node))
    if not reachable:
        out("  nothing reachable from the top windows.")

    out("\n--- verdict -----------------------------------------------------")
    if not windows:
        out("  Qat is connected to something that has no top-level window. It")
        out("  is not looking at the process drawing the user interface, which")
        out("  a launch script starting the real binary as a child will do.")
    elif qml_total == 0:
        out("  Qat's tree contains no QML objects at all. If this application")
        out("  draws its interface in QML, none of it can be recorded durably")
        out("  or replayed: either the plugin is not loaded for this Qt")
        out("  version, or the scene is not in a top-level QQuickView /")
        out("  QQuickWindow -- a QQuickWidget, or a QQuickView put inside a")
        out("  widget, renders into a window Qat does not traverse. See")
        out("  FINDINGS.md, 'QML is only visible in a top-level QQuickView';")
        out("  spike/qml_container_app.py is the experiment for the second.")
    elif missing:
        out(f"  Qat sees {qml_total} QML object(s), but not {missing}. That is")
        out("  about those objects specifically: a screen that had not been")
        out("  reached when this ran, or an identity Qat exposes differently.")
    else:
        out(f"  Qat sees {qml_total} QML object(s) and every name asked for.")
        out("  Whatever the replay could not find, it was not this.")

    return {"top_windows": len(windows), "qml": qml_total,
            "widgets": widget_total, "missing": missing}


def run(app: str = "", launch_path: str = "", args: str = "",
        names=(), pause: bool = False, out=print) -> int:
    """Launch the application the way the recorder does, then report."""
    try:
        import qat                                           # noqa: PLC0415
    except ImportError:
        print("qat is not installed in this environment", file=sys.stderr)
        return 2

    from qat_recorder import launch                          # noqa: PLC0415

    name = app or "_qat_recorder_probe"
    if launch_path:
        # Through the recorder's launcher: a launch script very often starts the
        # real binary as a child, and registering the script with Qat directly
        # gives up before the first question can be asked.
        launch.register_for_replay(qat, name, launch_path, args=args)

    # The application must stay usable: the point is to navigate it to the
    # screen in question, and Qat locks the UI for a session by default.
    try:
        qat.test_settings.Settings.lock_ui = "never"
    except AttributeError:
        pass

    context = launch.start(qat, name, app_path=launch_path or None)
    try:
        if pause:
            out("\nApplication is running. Navigate to the screen the session"
                "\nused, then press Enter here.")
            try:
                input()
            except EOFError:
                pass
        report(qat, names, out=out)
    finally:
        try:
            launch.close(qat, context)
        finally:
            if launch_path:
                try:
                    qat.unregister_application(name)
                except Exception:                            # noqa: BLE001
                    pass
    return 0
