# -*- coding: utf-8 -*-
"""
Building the event filter from the packaged C++ source.

The filter has to be compiled against the Qt the application under test uses, so
it cannot be shipped prebuilt in a pure-Python wheel. Shipping the *source*
inside the wheel is the next best thing: `pip install` puts everything on the VM,
and one command turns it into a `.so`. Otherwise the source has to be copied over
separately, which is a step people reasonably forget.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


class BuildError(RuntimeError):
    """The filter could not be built, with a reason worth acting on."""


def source_dir() -> Path:
    """Where the packaged C++ source lives."""
    path = Path(__file__).resolve().parent / "resources" / "native"
    if not (path / "qatrec.cpp").exists():
        raise BuildError(
            f"the C++ source is missing from this installation ({path}); "
            "the wheel may predate it — rebuild or reinstall")
    return path


def check_prerequisites() -> list:
    """Missing tools, described the way the operator needs to fix them."""
    missing = []
    if shutil.which("cmake") is None:
        missing.append("cmake            (dnf install cmake  |  apt install cmake)")
    if shutil.which("c++") is None and shutil.which("g++") is None:
        missing.append("a C++ compiler   (dnf install gcc-c++  |  apt install g++)")
    return missing


def build(out_dir: str, with_test_app: bool = False, jobs: Optional[int] = None,
          verbose: bool = False) -> Path:
    """Configure and build the filter. Returns the path to the library."""
    missing = check_prerequisites()
    if missing:
        raise BuildError("missing build tools:\n  " + "\n  ".join(missing))

    source = source_dir()
    build_path = Path(out_dir).expanduser().resolve()
    build_path.mkdir(parents=True, exist_ok=True)

    configure = [
        "cmake", "-S", str(source), "-B", str(build_path),
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DQATREC_BUILD_TEST_APP={'ON' if with_test_app else 'OFF'}",
    ]
    _run(configure, "cmake configure", verbose,
         hint="Qt development headers are usually what is missing here:\n"
              "  Qt 5: dnf install qt5-qtbase-devel   | apt install qtbase5-dev\n"
              "  Qt 6: dnf install qt6-qtbase-devel   | apt install qt6-base-dev")

    compile_command = ["cmake", "--build", str(build_path)]
    if jobs:
        compile_command += ["-j", str(jobs)]
    else:
        compile_command += ["-j"]
    _run(compile_command, "compile", verbose,
         hint="If the linker cannot find -lstdc++, the static C++ library is "
              "missing:\n"
              "  dnf install libstdc++-static   | apt install libstdc++-dev\n"
              "That is only needed to build a filter that runs on OTHER "
              "machines; a newer build of this tool falls back automatically.")

    produced = sorted(build_path.glob("libqatrec*.so"))
    if not produced:
        raise BuildError(
            f"the build reported success but produced no library in {build_path}")
    return produced[0]


def _run(command: list, what: str, verbose: bool, hint: str = "") -> None:
    result = subprocess.run(command, capture_output=not verbose, text=True)
    if result.returncode == 0:
        return
    detail = ""
    if not verbose:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        detail = "\n".join("  " + line for line in tail[-15:])
    message = f"{what} failed"
    if detail:
        message += ":\n" + detail
    if hint:
        message += "\n\n" + hint
    raise BuildError(message)
