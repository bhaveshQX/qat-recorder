# -*- coding: utf-8 -*-
"""
Which build of the recorder is actually installed, and what it can do.

Every wheel is called `qat_recorder-0.1.0-py3-none-any.whl`. Four of them with
different contents and the same name have crossed to the same VM, and there is
no way to tell from the outside which one arrived -- a stale copy in
~/Downloads installs exactly as quietly as a fresh one. When the next recording
then fails in the old way, the log looks identical to the log from the fix, and
the round trip is spent proving what is installed rather than what is wrong.

So the build identifies itself: a hash of the files that actually behave
differently between versions, and a plain list of the behaviours that were
argued about. `python -m qat_recorder version` prints it, the agent prints it on
startup, and both are in whatever log gets pasted next.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List, Tuple

#: What the recorder's behaviour actually lives in. Changing any of these is
#: what makes a build different from the one before it in any way that matters
#: on a VM.
PARTS = (
    "resources/wrapper.sh",
    "resources/native/qatgate.c",
    "resources/native/qatrec.cpp",
    "launch.py",
    "servers.py",
)


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def build_id() -> str:
    """A short hash of the parts that decide how a launch behaves."""
    digest = hashlib.sha256()
    for name in PARTS:
        path = _package_dir() / name
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def version() -> str:
    try:
        from qat_recorder import __version__       # noqa: PLC0415

        return str(__version__)
    except Exception:                              # noqa: BLE001
        return "unknown"


#: Behaviours worth asking about by name, and the evidence that they are
#: present. Each one is a bug that cost a round trip to find; a build either has
#: the fix or it does not, and now it says so.
FEATURES: Tuple[Tuple[str, str, str], ...] = (
    ("gate", "resources/wrapper.sh", "QATREC_GATE"),
    ("no injector in the launching shell", "resources/wrapper.sh",
     "QATREC_APP_IS_SCRIPT"),
    ("gate refuses shells", "resources/native/qatgate.c", "is_a_shell"),
    ("follows the app into a child", "launch.py", "def start("),
    ("checks the filter exists", "launch.py", "def missing_pieces("),
    ("checks the filter's Qt", "launch.py", "def qt_mismatch("),
    ("repairs Qat's servers", "servers.py", "def apply_repairs("),
    ("launch diagnostics", "resources/wrapper.sh", "qatrec-wrapper: cwd"),
)


def features() -> List[Tuple[str, bool]]:
    """Each named behaviour, and whether this installation has it."""
    cache = {}
    found = []
    for label, name, marker in FEATURES:
        if name not in cache:
            try:
                cache[name] = (_package_dir() / name).read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                cache[name] = ""
        found.append((label, marker in cache[name]))
    return found


def describe() -> str:
    """The whole answer to "which build is this?" in a paste-able block."""
    lines = [f"qat-recorder {version()}  build {build_id()}",
             f"  installed at {_package_dir()}"]
    for label, present in features():
        lines.append(f"  [{'x' if present else ' '}] {label}")
    missing = [label for label, present in features() if not present]
    if missing:
        lines.append("")
        lines.append("This build predates the fixes that are unticked. If a "
                     "newer wheel was meant to be")
        lines.append("installed, the one that arrived was not it -- every "
                     "wheel has the same file name.")
    return "\n".join(lines)


def one_line() -> str:
    """For a startup banner, where a block would be noise."""
    return f"qat-recorder {version()} (build {build_id()})"
