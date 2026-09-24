# -*- coding: utf-8 -*-
"""
Running a generated test where the application lives.

A recorded test imports `qat` and launches the application itself, so it can
only run on the machine the application is on. When a session is driven from a
tester's desk that machine is the VM, and copying the file across by hand to run
it is not a workflow -- avoiding exactly that is why the agent exists.

So the artifacts are written on the host that recorded them, and this module
runs them there. The same code serves a local session, where "there" happens to
be the same machine.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

#: Enough to see which step failed and why, without shipping a novel over the
#: wire. pytest puts the failure at the end, so the tail is the useful half.
MAX_OUTPUT = 20000

DEFAULT_TIMEOUT = 300.0

NO_DISPLAY = (
    "no DISPLAY is set for the agent, so the application cannot open a window. "
    "Start the agent from a terminal inside the graphical session you are "
    "watching (or export DISPLAY first) -- replay drives a real UI and needs a "
    "screen to draw on, exactly as recording did."
)


#: pytest reprints everything the application wrote to its own stdout, under a
#: heading like `---- Captured stdout call ----`, *after* the traceback. An
#: application that retries a socket once a second writes tens of thousands of
#: lines while a step waits for an object -- so keeping the tail of the output
#: keeps the noise and throws the failure away. That is not hypothetical: a
#: replay that failed on "none of these found a usable object" came back as
#: 20000 characters of reconnect warnings, with the assertion that says which
#: object and what was tried cut off mid-word.
CAPTURED = re.compile(r"^-+ Captured \w+ \w+ -+$")

#: A pytest section heading -- `=== FAILURES ===`, `___ test_name ___` -- which
#: is what ends a block of the application's own output. Rules of dashes are
#: not accepted: an application draws those in its own log, and one of them
#: ending the block early would let the rest of the log through unchanged.
SECTION = re.compile(r"^(?:={5,}|_{5,}).*(?:={5,}|_{5,})$")

#: How many lines to keep from each block the application wrote: enough for its
#: dying words -- the traceback it printed, the last thing it did before it
#: aborted -- and far short of an hour of one warning a second.
CAPTURED_LINES = 40


def condense(text: str, keep: int = CAPTURED_LINES) -> str:
    """The same output, with the application's own noise cut down to its end.

    Only the blocks pytest captured from the application are touched. pytest's
    own report -- the traceback, the assertion, the summary -- is what a replay
    is read for, and it comes through whole however long the log was.
    """
    out: list = []
    block: list = []
    inside = False

    def flush():
        if not block:
            return
        if len(block) <= keep:
            out.extend(block)
        else:
            out.append(f"... {len(block) - keep} earlier lines from the "
                       "application, omitted ...")
            out.extend(block[-keep:])
        del block[:]

    for line in text.splitlines():
        if CAPTURED.match(line):
            flush()
            inside = True
            out.append(line)
        elif inside and SECTION.match(line):
            flush()
            inside = False
            out.append(line)
        elif inside:
            block.append(line)
        else:
            out.append(line)
    flush()

    joined = "\n".join(out)
    return joined + "\n" if text.endswith("\n") else joined


def tail(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return "...\n" + text[-limit:]


def run_pytest(directory, test_file: str = "test_recorded.py",
               timeout: float = DEFAULT_TIMEOUT,
               python: str = "", env=None) -> dict:
    """Run one generated test and report what happened.

    Runs with the session directory as the working directory, because Qat writes
    its `applications.json` into the current directory rather than a config
    directory. Keeping that inside the session's own folder means a replay
    leaves nothing behind anywhere else.
    """
    directory = Path(directory)
    target = directory / test_file
    if not target.exists():
        return {"ok": False, "exit_code": -1, "directory": str(directory),
                "output": f"nothing to replay: {target} does not exist"}

    environment = dict(os.environ if env is None else env)
    if not environment.get("DISPLAY") and not environment.get("WAYLAND_DISPLAY"):
        return {"ok": False, "exit_code": -1, "directory": str(directory),
                "output": NO_DISPLAY}

    # The interpreter running this already has qat and pytest: it is the one the
    # installer built. Resolving `pytest` from PATH would find a different one.
    command = [python or sys.executable, "-m", "pytest", test_file, "-v"]

    try:
        finished = subprocess.run(
            command, cwd=str(directory), env=environment,
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "directory": str(directory),
                "output": f"replay did not finish within {timeout:.0f}s and was "
                          "stopped. A step is probably waiting for something "
                          "that never appeared."}
    except OSError as error:
        return {"ok": False, "exit_code": -1, "directory": str(directory),
                "output": f"could not run pytest: {error}"}

    output = (finished.stdout or "") + (finished.stderr or "")
    return {
        "ok": finished.returncode == 0,
        "exit_code": finished.returncode,
        "directory": str(directory),
        # Condensed before it is trimmed: the trim keeps the end, and the end
        # is the application's log unless the log is cut down first.
        "output": tail(condense(output)),
    }
