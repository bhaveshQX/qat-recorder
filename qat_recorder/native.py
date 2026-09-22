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


def discard_previous(build_path: Path) -> None:
    """Clear what a previous build would otherwise decide for this one.

    CMake caches which Qt it found. Installing qt6-base-dev and building again
    in the same directory therefore builds against Qt 5 exactly as before, for
    no visible reason -- and the stale `libqatrec.5.15.so` left beside the new
    `libqatrec.6.2.so` sorts first, so even the path reported afterwards is the
    wrong one. Both are somebody's afternoon, spent on nothing.

    Configuring from scratch costs a couple of seconds. This is a build
    directory for two source files; there is nothing in it worth keeping.
    """
    cache = build_path / "CMakeCache.txt"
    if cache.exists():
        cache.unlink()
    shutil.rmtree(build_path / "CMakeFiles", ignore_errors=True)


def built_filters(build_path: Path) -> set:
    """The filter libraries currently in a build directory."""
    return set(build_path.glob("libqatrec*.so"))


def discard_superseded(build_path: Path, keep: Path) -> None:
    """Remove filters left by an earlier build against a different Qt.

    After the new one exists, never before: a build that fails after the old
    filter was deleted leaves a directory with no filter in it at all, and the
    recorder pointed at a path that no longer resolves -- which surfaces as one
    line of the application's standard error and nowhere else.

    They cannot both stay. `libqatrec.5.15.so` sorts before `libqatrec.6.2.so`,
    so whatever picks a filter out of this directory by name picks the one that
    was just replaced.
    """
    for path in built_filters(build_path):
        if path == keep:
            continue
        try:
            path.unlink()
        except OSError:                              # pragma: no cover
            pass


def build(out_dir: str, with_test_app: bool = False, jobs: Optional[int] = None,
          verbose: bool = False, qt_major: Optional[int] = None,
          qt_prefix: str = "") -> Path:
    """Configure and build the filter. Returns the path to the library.

    `qt_major` forces which Qt to build against, for an application that ships
    its own Qt and therefore has nothing to do with what this machine has
    installed.
    """
    missing = check_prerequisites()
    if missing:
        raise BuildError("missing build tools:\n  " + "\n  ".join(missing))

    source = source_dir()
    build_path = Path(out_dir).expanduser().resolve()
    build_path.mkdir(parents=True, exist_ok=True)
    existing = built_filters(build_path)
    discard_previous(build_path)

    configure = [
        "cmake", "-S", str(source), "-B", str(build_path),
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DQATREC_BUILD_TEST_APP={'ON' if with_test_app else 'OFF'}",
    ]
    if qt_major:
        configure.append(f"-DQATREC_QT_MAJOR={qt_major}")
    if qt_prefix:
        # Where more than one Qt is installed, which is exactly the machine an
        # application that ships its own Qt tends to be developed on.
        configure.append(f"-DCMAKE_PREFIX_PATH={qt_prefix}")
    wanted = f"Qt {qt_major}" if qt_major else "Qt"
    _run(configure, "cmake configure", verbose,
         hint=f"{wanted} development headers are usually what is missing here:\n"
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

    # The one this build made, not the one alphabetically first: after a
    # rebuild against another Qt both are there, and `libqatrec.5.15.so` sorts
    # before `libqatrec.6.2.so`.
    produced = built_filters(build_path)
    fresh = produced - existing
    if not produced:
        raise BuildError(
            f"the build reported success but produced no library in {build_path}")
    library = max(fresh or produced, key=lambda path: path.stat().st_mtime)
    discard_superseded(build_path, library)
    return library


def gate_of(library) -> Optional[Path]:
    """The gate library built beside `library`, if this build produced one.

    It is found by position rather than reported separately because everything
    downstream -- the panel, the CLI, a generated test -- knows where the filter
    is and nothing else. See `qat_recorder.launch.gate_for()`.
    """
    candidate = Path(library).resolve().parent / "libqatgate.so"
    return candidate if candidate.exists() else None


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
