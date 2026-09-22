# -*- coding: utf-8 -*-
"""
Qat's prebuilt servers, and the machines they refuse to load on.

Qat injects a server library chosen by the Qt version it finds in the running
application: Qt 6.8.6 means `libQatServer.6.8.so`. Those libraries are shipped
prebuilt in the wheel, and they were not all built on the same distribution. The
Qt 6.8 and newer ones were built against glibc 2.38 -- Ubuntu 24.04 -- so on a
22.04 or RHEL 9 machine the injection ends there:

    Detected Qt version 6.8.6
    Loaded Qat server from: ".../qat/bin/libQatServer.6.8.so"
    Failed to load Qat server: ...
    /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38' not found

Nothing is wrong with the application, the filter, or the injection. The
application starts perfectly by hand, because by hand nothing tries to load that
file into it.

Qt promises binary compatibility forward across a major version: a library built
against Qt 6.7 runs inside a Qt 6.8.6 application. So the repair is to stand the
newest server that *does* load on this machine in for the one that does not,
keeping the original beside it. This module measures which is which and performs
that substitution; `python -m qat_recorder qat-servers` is the front end.

Measured, not assumed: the floors come from the version strings in each library,
the same way native/tests/check_symbol_floor.sh measures our own filter.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

Version = Tuple[int, ...]

#: What a substituted file's original is kept as. Restorable, and obvious to
#: anybody who lists the directory wondering what happened to it.
BACKUP_SUFFIX = ".qatrec-original"

_NAME = re.compile(r"^libQatServer\.(\d+)\.(\d+)\.so$")


def _highest(data: bytes, prefix: str, parts: int) -> Optional[Version]:
    pattern = prefix.encode() + b"_" + rb"\.".join([rb"(\d+)"] * parts)
    found = [tuple(int(number) for number in match)
             for match in re.findall(pattern, data)]
    return max(found) if found else None


def show(version: Optional[Version]) -> str:
    return ".".join(str(part) for part in version) if version else "-"


@dataclass
class Library:
    """One prebuilt server, and what a machine must provide to load it."""

    path: Path
    qt: Optional[Version]
    glibc: Optional[Version]
    glibcxx: Optional[Version]

    @property
    def name(self) -> str:
        return self.path.name


def requirements(path) -> Tuple[Optional[Version], Optional[Version]]:
    """The glibc and libstdc++ versions `path` needs, from its own bytes."""
    data = Path(path).read_bytes()
    return _highest(data, "GLIBC", 2), _highest(data, "GLIBCXX", 3)


def qt_version_of(name: str) -> Optional[Version]:
    """The Qt version a server is named for. `libQatServer.6.8.so` -> (6, 8)."""
    match = _NAME.match(name)
    return (int(match.group(1)), int(match.group(2))) if match else None


def qat_bin_dir() -> Optional[Path]:
    """Where the installed Qat keeps its binaries."""
    try:
        from importlib import resources  # noqa: PLC0415

        path = Path(str(resources.files("qat"))) / "bin"
    except Exception:                                  # noqa: BLE001
        return None
    return path if path.is_dir() else None


def survey(bin_dir=None) -> List[Library]:
    """Every prebuilt server, measured."""
    folder = Path(bin_dir) if bin_dir is not None else qat_bin_dir()
    if folder is None or not folder.is_dir():
        return []
    libraries = []
    for path in sorted(folder.glob("libQatServer.*.so")):
        if path.name.endswith(BACKUP_SUFFIX):
            continue
        glibc, glibcxx = requirements(path)
        libraries.append(Library(path=path, qt=qt_version_of(path.name),
                                 glibc=glibc, glibcxx=glibcxx))
    return libraries


# -- what this machine provides ----------------------------------------------

def machine_glibc() -> Optional[Version]:
    """The glibc on this machine, asked of glibc itself."""
    try:
        value = os.confstr("CS_GNU_LIBC_VERSION")       # "glibc 2.35"
    except (AttributeError, ValueError, OSError):
        value = None
    if not value:
        try:
            libc = ctypes.CDLL("libc.so.6")
            libc.gnu_get_libc_version.restype = ctypes.c_char_p
            value = libc.gnu_get_libc_version().decode()
        except Exception:                               # noqa: BLE001
            return None
    match = re.search(r"(\d+)\.(\d+)", value)
    return (int(match.group(1)), int(match.group(2))) if match else None


def machine_libstdcxx() -> Optional[Version]:
    """The newest GLIBCXX this machine's libstdc++ defines."""
    candidates = []
    found = ctypes.util.find_library("stdc++")
    if found:
        candidates.append(found)
    candidates += [
        "/usr/lib/x86_64-linux-gnu/libstdc++.so.6",
        "/usr/lib64/libstdc++.so.6",
        "/lib/x86_64-linux-gnu/libstdc++.so.6",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if not path.is_absolute():
            # find_library may return a bare soname; let the loader locate it.
            for root in ("/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/lib"):
                if (Path(root) / candidate).exists():
                    path = Path(root) / candidate
                    break
        try:
            if path.exists():
                return _highest(path.read_bytes(), "GLIBCXX", 3)
        except OSError:                                 # pragma: no cover
            continue
    return None


@dataclass
class Machine:
    glibc: Optional[Version]
    glibcxx: Optional[Version]

    @classmethod
    def here(cls) -> "Machine":
        return cls(glibc=machine_glibc(), glibcxx=machine_libstdcxx())


def loads_on(library: Library, machine: Machine) -> bool:
    """Whether `library` can be loaded on `machine`.

    Unknown is treated as loadable. A missing measurement is a reason to say
    nothing, not a reason to rearrange somebody's installation.
    """
    if machine.glibc and library.glibc and library.glibc > machine.glibc:
        return False
    if machine.glibcxx and library.glibcxx and library.glibcxx > machine.glibcxx:
        return False
    return True


# -- the repair --------------------------------------------------------------

def repairs(libraries: List[Library], machine: Machine) -> Dict[Path, Library]:
    """Which server to stand in for each one that will not load here.

    The stand-in is the newest Qt minor of the same major that does load and is
    not newer than the one being replaced. Older is safe -- Qt guarantees a
    library built against 6.7 keeps working inside a 6.8 application -- and
    newer is not, so this only ever goes down.
    """
    working = [lib for lib in libraries if lib.qt and loads_on(lib, machine)]
    plan: Dict[Path, Library] = {}
    for library in libraries:
        if library.qt is None or loads_on(library, machine):
            continue
        options = [lib for lib in working
                   if lib.qt[0] == library.qt[0] and lib.qt < library.qt]
        if options:
            plan[library.path] = max(options, key=lambda lib: lib.qt)
    return plan


def apply_repairs(plan: Dict[Path, Library]) -> List[Tuple[Path, Path]]:
    """Perform the substitutions, keeping every original.

    Returns the (replaced, stand-in) pairs actually written.
    """
    done = []
    for target, standin in plan.items():
        backup = target.with_name(target.name + BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(target, backup)
        shutil.copy2(standin.path, target)
        done.append((target, standin.path))
    return done


def restore(bin_dir=None) -> List[Path]:
    """Put every original back."""
    folder = Path(bin_dir) if bin_dir is not None else qat_bin_dir()
    if folder is None:
        return []
    restored = []
    for backup in sorted(folder.glob("*" + BACKUP_SUFFIX)):
        target = backup.with_name(backup.name[:-len(BACKUP_SUFFIX)])
        shutil.copy2(backup, target)
        backup.unlink()
        restored.append(target)
    return restored


def substituted(bin_dir=None) -> List[Path]:
    """Servers currently standing in for another."""
    folder = Path(bin_dir) if bin_dir is not None else qat_bin_dir()
    if folder is None:
        return []
    return [backup.with_name(backup.name[:-len(BACKUP_SUFFIX)])
            for backup in sorted(folder.glob("*" + BACKUP_SUFFIX))]


# -- saying it ---------------------------------------------------------------

def report(bin_dir=None, machine: Optional[Machine] = None) -> str:
    """What each server needs, what this machine has, and what to do about it."""
    libraries = survey(bin_dir)
    machine = machine or Machine.here()

    if not libraries:
        return ("No Qat server libraries found. Is qat installed in this "
                "environment?")

    lines = [
        f"this machine   glibc {show(machine.glibc)}   "
        f"libstdc++ {show(machine.glibcxx)}",
        "",
        f"  {'server':<26}{'glibc':>7}{'libstdc++':>11}   ",
    ]
    for library in libraries:
        verdict = "ok" if loads_on(library, machine) else "WILL NOT LOAD HERE"
        lines.append(f"  {library.name:<26}{show(library.glibc):>7}"
                     f"{show(library.glibcxx):>11}   {verdict}")

    already = substituted(bin_dir)
    if already:
        lines.append("")
        lines.append("Already standing in for an unloadable build:")
        for path in already:
            lines.append(f"  {path.name}   "
                         f"(original kept as {path.name}{BACKUP_SUFFIX})")

    plan = repairs(libraries, machine)
    if not plan:
        lines.append("")
        lines.append("Nothing to repair: every server Qat might choose loads "
                     "on this machine.")
        return "\n".join(lines)

    lines.append("")
    lines.append("An application using one of these Qt versions cannot be "
                 "recorded on this machine:")
    for target, standin in sorted(plan.items()):
        lines.append(f"  {target.name}  ->  stand in {standin.name} "
                     f"(glibc {show(standin.glibc)})")
    lines.append("")
    lines.append("Qt is binary compatible forward within a major version, so a "
                 "server built")
    lines.append("against the older minor works inside the newer application. "
                 "To do it:")
    lines.append("")
    lines.append("    python -m qat_recorder qat-servers --fix")
    lines.append("")
    lines.append("Every original is kept, and --restore puts them back.")
    return "\n".join(lines)
