# -*- coding: utf-8 -*-
"""
Receiving the native event stream.

`native/qatrec.cpp` writes newline-delimited JSON to a TCP socket. This module
turns that into `RawEvent` objects. It deliberately knows nothing about Qat or
about semantics — folding raw events into actions is `capture.py`'s job.

The wire format is the contract between the C++ and Python halves:

    {"kind": "mouse_press", "t": 1786533080600, "button": 1, "modifiers": 0,
     "x": 51, "y": 51,
     "target": {"class": "QGroupBox", "objectName": "credentialsGroup",
                "title": "Credentials", "index": 0,
                "path": [{"class": "QWidget", "objectName": "rootWidget"},
                         {"class": "QMainWindow", "objectName": "mainWindow"}]}}

A click on a menu or menu bar carries one extra field, `menuItem`, naming the
item under the pointer. The coordinates are how the filter works that out; they
are not how the click is replayed.

Note what is absent: the characters typed. Those never cross the socket, so a
crash dump, a log or a packet capture cannot leak a password.
"""

from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Iterable, Iterator, Mapping, Optional

#: Objects that receive input events before the widget that visually owns them.
#: Qt delivers a click to the window and then to the widget, so the same physical
#: interaction arrives more than once; these classes are the less useful copy.
WINDOW_CLASSES = frozenset({
    "QWidgetWindow", "QWindow", "QQuickWindow", "QQuickWidgetOffscreenWindow",
})


@dataclass(frozen=True)
class Locator:
    """Structural identification of the object an event was delivered to."""

    cls: str = ""
    object_name: str = ""
    text: str = ""
    title: str = ""
    index: int = -1
    #: For a click on a menu or a menu bar: the label of the item under the
    #: pointer. Menu items are QActions rather than widgets, so they never
    #: receive the event themselves and the menu has to be asked which one was
    #: hit. Empty for everything else, and for QML, where a menu item is a real
    #: object and arrives through the ordinary fields above.
    menu_item: str = ""
    menu_item_name: str = ""
    #: For a click inside a list, tree or table: which model index was hit.
    #: Rows are not widgets either -- the viewport receives the event -- so the
    #: view has to be asked. -1 means "not an item view, or below the last row".
    item_row: int = -1
    item_column: int = 0
    item_text: str = ""
    item_view: str = ""
    item_view_class: str = ""
    path: tuple = field(default_factory=tuple)

    @property
    def is_item(self) -> bool:
        return self.item_row >= 0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Locator":
        return cls(
            cls=str(data.get("class", "")),
            object_name=str(data.get("objectName", "")),
            text=str(data.get("text", "")),
            title=str(data.get("title", "")),
            index=int(data.get("index", -1)),
            menu_item=str(data.get("menuItem", "")),
            menu_item_name=str(data.get("menuItemName", "")),
            item_row=int(data.get("itemRow", -1)),
            item_column=int(data.get("itemColumn", 0)),
            item_text=str(data.get("itemText", "")),
            item_view=str(data.get("itemView", "")),
            item_view_class=str(data.get("itemViewClass", "")),
            path=tuple(
                (str(item.get("class", "")), str(item.get("objectName", "")))
                for item in data.get("path", [])
            ),
        )

    @property
    def is_window(self) -> bool:
        return self.cls in WINDOW_CLASSES

    def nearest_named_ancestor(self) -> Optional[str]:
        for _, object_name in self.path:
            if object_name:
                return object_name
        return None

    def specificity(self) -> tuple:
        """Higher is better when several objects saw the same interaction."""
        return (0 if self.is_window else 1, len(self.path), bool(self.object_name))


@dataclass(frozen=True)
class RawEvent:
    kind: str
    t: int
    target: Locator
    button: int = 0
    modifiers: int = 0
    x: int = 0
    y: int = 0
    key: int = 0
    dx: int = 0
    dy: int = 0
    #: Set for `shortcut` records, already formatted by Qt (e.g. "Ctrl+S").
    keys: str = ""
    #: Set for the `hello` record the filter sends when it starts: what this
    #: build of it can report. A filter is compiled on the machine it runs on
    #: and stays there until somebody rebuilds it, so the Python half cannot
    #: assume it is current -- and telling "this view has no rows" apart from
    #: "the filter here cannot say" is the difference between a useful failure
    #: and a misleading one.
    features: tuple = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RawEvent":
        return cls(
            kind=str(data.get("kind", "")),
            t=int(data.get("t", 0)),
            target=Locator.from_dict(data.get("target", {})),
            button=int(data.get("button", 0)),
            modifiers=int(data.get("modifiers", 0)),
            x=int(data.get("x", 0)),
            y=int(data.get("y", 0)),
            key=int(data.get("key", 0)),
            dx=int(data.get("dx", 0)),
            dy=int(data.get("dy", 0)),
            keys=str(data.get("keys", "")),
            features=tuple(str(name) for name in data.get("features", ())),
        )


def parse_lines(lines: Iterable[str]) -> Iterator[RawEvent]:
    """Parse a stream of JSON lines, skipping blanks and malformed records."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield RawEvent.from_dict(json.loads(line))
        except (ValueError, TypeError):
            continue


class EventReceiver:
    """Listens for one connection from the injected library.

    Used as a context manager; `port` is what to put in QATREC_PORT. Passing
    port 0 lets the OS choose, and `actual_port` reports it.
    """

    def __init__(self, port: int = 0, host: str = "127.0.0.1"):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, port))
        self._server.listen(1)
        self._server.settimeout(0.5)
        self.actual_port = self._server.getsockname()[1]
        self.queue: Queue = Queue()
        self._running = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.malformed = 0

    def __enter__(self) -> "EventReceiver":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def start(self) -> None:
        self._running.set()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        try:
            self._server.close()
        except OSError:
            pass

    def drain(self, timeout: float = 0.0) -> list:
        """Return everything received so far."""
        events = []
        while True:
            try:
                events.append(self.queue.get(timeout=timeout) if timeout
                              else self.queue.get_nowait())
            except Empty:
                return events
            timeout = 0.0

    def _serve(self) -> None:
        connection = None
        while self._running.is_set() and connection is None:
            try:
                connection, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
        if connection is None:
            return

        connection.settimeout(0.5)
        buffer = b""
        with connection:
            while self._running.is_set():
                try:
                    chunk = connection.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.decode("utf-8", "replace").strip()
                    if not text:
                        continue
                    try:
                        self.queue.put(RawEvent.from_dict(json.loads(text)))
                    except (ValueError, TypeError):
                        self.malformed += 1
