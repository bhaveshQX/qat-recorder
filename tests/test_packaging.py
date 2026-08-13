# -*- coding: utf-8 -*-
"""
Packaging: the things that only break once the code is installed rather than
run from a checkout.

The launcher wrapper is the example that motivated this file. It was located
relative to the repository layout, which works in development and points into
site-packages once installed — so `pip install` on a VM produced a recorder that
could not launch anything.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from qat_recorder.ui.controller import default_wrapper

ROOT = Path(__file__).resolve().parents[1]


def test_wrapper_ships_inside_the_package():
    wrapper = default_wrapper()
    assert wrapper.exists()
    assert wrapper.read_text(encoding="utf-8").startswith("#!")


def test_wrapper_lives_under_the_package_not_the_repo_layout():
    """It must be found the same way from a wheel as from a checkout."""
    import qat_recorder
    package_root = Path(qat_recorder.__file__).resolve().parent
    wrapper = default_wrapper()
    # Either inside the package, or staged in the temp dir for a read-only install.
    assert (package_root in wrapper.parents
            or wrapper.parent == Path(tempfile.gettempdir()))


def test_wrapper_is_executable():
    """Wheels do not reliably preserve the executable bit; it is restored."""
    wrapper = default_wrapper()
    if os.name == "nt":
        pytest.skip("no executable bit on Windows")
    assert os.access(wrapper, os.X_OK)


def test_wrapper_appends_rather_than_replaces_ld_preload(tmp_path):
    """Qat sets LD_PRELOAD to its injector; ours must be added, not substituted,
    or Qat's server never loads."""
    if os.name == "nt":
        pytest.skip("shell wrapper is Linux-only")

    probe = tmp_path / "probe.sh"
    probe.write_text('#!/bin/sh\necho "$LD_PRELOAD"\n', encoding="utf-8")
    probe.chmod(0o755)

    result = subprocess.run(
        ["bash", str(default_wrapper())],
        env={**os.environ,
             "LD_PRELOAD": "/qat/libinjector.so",
             "QATREC_LIB": "/opt/libqatrec.so",
             "QATREC_APP": str(probe)},
        capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
    assert "/qat/libinjector.so" in result.stdout
    assert "/opt/libqatrec.so" in result.stdout


def test_wrapper_works_when_nothing_was_preloaded(tmp_path):
    if os.name == "nt":
        pytest.skip("shell wrapper is Linux-only")

    probe = tmp_path / "probe.sh"
    probe.write_text('#!/bin/sh\necho "[$LD_PRELOAD]"\n', encoding="utf-8")
    probe.chmod(0o755)

    env = {key: value for key, value in os.environ.items()
           if key != "LD_PRELOAD"}
    result = subprocess.run(
        ["bash", str(default_wrapper())],
        env={**env, "QATREC_LIB": "/opt/libqatrec.so", "QATREC_APP": str(probe)},
        capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[/opt/libqatrec.so]"


def test_declared_package_data_covers_the_wrapper_and_native_source():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.setuptools.package-data]" in text
    assert "resources/*.sh" in text
    assert "resources/native/*" in text


def test_the_cpp_source_ships_with_the_package():
    """Without this, `build-filter` cannot work on a VM that only has the wheel
    — which is exactly how it is installed."""
    from qat_recorder.native import source_dir

    source = source_dir()
    assert (source / "qatrec.cpp").exists()
    assert (source / "CMakeLists.txt").exists()


def test_core_imports_without_qat_or_qt():
    """The tester's machine installs this without qat; the VM installs it with.

    Importing the core must not require either, so a desk install stays small
    and a CI image does not need Qt.
    """
    code = (
        "import sys\n"
        "sys.modules['qat'] = None\n"
        "import qat_recorder\n"
        "from qat_recorder.emit import emit_python\n"
        "from qat_recorder.capture import CaptureSession\n"
        "from qat_recorder.agent.registry import Registry\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True, timeout=60,
                            cwd=str(ROOT))
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
