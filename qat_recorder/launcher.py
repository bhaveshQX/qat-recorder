# -*- coding: utf-8 -*-
"""
Launch helpers, and one Qat behaviour worth knowing about.

Qat resolves the registered application path through symlinks before launching it
(`qat/internal/app_launcher.py:465` — `app_path = Path(app_path).resolve()`), then
spawns the resolved target with `cwd` set to that target's directory.

Two consequences on Linux:

1. **Wrapper scripts are bypassed.** Applications installed as
   `/usr/local/bin/app -> /opt/app-1.2/bin/app`, or launched through a shell
   wrapper that sets `LD_LIBRARY_PATH` / `QT_PLUGIN_PATH`, will be started from
   the resolved binary without whatever the wrapper set up. Register the wrapper's
   *real* target and reproduce its environment, or the application may fail to
   start in ways that look like injection failures.

2. **Virtualenv interpreters lose their site-packages.** `venv/bin/python` is a
   symlink to the system interpreter, so a Python application under test is
   launched by the *system* python and cannot import anything the venv installed.
   `prepare_python_aut_env()` works around it.
"""

from __future__ import annotations

import os
import site
import sys


def prepare_python_aut_env(*extra_paths: str) -> str:
    """Make a Python application under test importable after symlink resolution.

    Puts this interpreter's site-packages (plus any `extra_paths`) on `PYTHONPATH`
    in `os.environ`, so that the resolved system interpreter can still import them.
    Must be called *before* `qat.start_application()`, which snapshots the
    environment.

    Returns the resulting PYTHONPATH. Native applications do not need this.
    """
    paths = []
    for path in list(extra_paths):
        if path and os.path.isdir(path):
            paths.append(str(path))

    try:
        candidates = list(site.getsitepackages())
    except AttributeError:                       # pragma: no cover - unusual builds
        candidates = []
    user_site = getattr(site, "getusersitepackages", None)
    if callable(user_site):
        try:
            candidates.append(user_site())
        except Exception:                        # noqa: BLE001  # pragma: no cover
            pass

    for path in candidates:
        if path and os.path.isdir(path):
            paths.append(str(path))

    existing = os.environ.get("PYTHONPATH", "")
    if existing:
        paths.extend(existing.split(os.pathsep))

    # Preserve order, drop duplicates.
    merged = os.pathsep.join(dict.fromkeys(paths))
    os.environ["PYTHONPATH"] = merged
    return merged


def describe_resolution(app_path: str) -> str:
    """Report where Qat will actually launch `app_path` from."""
    real = os.path.realpath(app_path)
    if real == os.path.abspath(app_path):
        return f"{app_path} (not a symlink)"
    return f"{app_path} -> {real}  [Qat launches the resolved target]"


def venv_hint() -> str:
    """One-line diagnostic for the common venv failure."""
    real = os.path.realpath(sys.executable)
    if real != os.path.abspath(sys.executable):
        return (f"interpreter {sys.executable} resolves to {real}; "
                "call prepare_python_aut_env() before start_application()")
    return "interpreter is not a symlink; no PYTHONPATH workaround needed"
