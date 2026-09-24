# -*- coding: utf-8 -*-
"""
Qat's prebuilt servers, and the machine they will not load on.

The failure they cause is invisible everywhere except the application's own
output, and it looks exactly like an injection problem:

    Detected Qt version 6.8.6
    QtCore library found at .../spine.Build3383/bin/../lib/Qt/lib/libQt6Core.so.6
    Loaded Qat server from: ".../qat/bin/libQatServer.6.8.so"
    Failed to load Qat server: ...
    /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38' not found

Injection worked. The application bundles Qt 6.8.6, Qat therefore chose the
server built for Qt 6.8, and that one was built on Ubuntu 24.04. On a 22.04
machine it cannot be loaded at all -- so the application starts perfectly by
hand, and cannot be recorded.

Measured from the libraries shipped in the wheel:

    libQatServer.6.2 .. 6.7    glibc 2.14 - 2.34    loads on 22.04
    libQatServer.6.8 .. 6.11   glibc 2.38           does not
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qat_recorder import servers


def fake_server(folder: Path, qt: str, glibc: str, glibcxx: str) -> Path:
    """A file that measures like a real server without being one.

    The measurement reads version strings out of the bytes, which is what makes
    it testable without shipping four megabytes of ELF per case.
    """
    path = folder / f"libQatServer.{qt}.so"
    path.write_bytes(b"\x7fELF" + b"\x00" * 32
                     + f"GLIBC_{glibc}\x00GLIBCXX_{glibcxx}\x00".encode())
    return path


@pytest.fixture()
def bin_dir(tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir()
    fake_server(folder, "6.5", "2.34", "3.4.29")
    fake_server(folder, "6.7", "2.34", "3.4.29")
    fake_server(folder, "6.8", "2.38", "3.4.32")
    fake_server(folder, "6.9", "2.38", "3.4.32")
    fake_server(folder, "5.15", "2.14", "3.4.26")
    return folder


JAMMY = servers.Machine(glibc=(2, 35), glibcxx=(3, 4, 30))
NOBLE = servers.Machine(glibc=(2, 39), glibcxx=(3, 4, 33))


# --- measuring --------------------------------------------------------------

def test_what_a_server_needs_is_read_from_the_server(bin_dir):
    glibc, glibcxx = servers.requirements(bin_dir / "libQatServer.6.8.so")
    assert glibc == (2, 38)
    assert glibcxx == (3, 4, 32)


def test_a_server_is_named_for_the_qt_it_was_built_for():
    assert servers.qt_version_of("libQatServer.6.8.so") == (6, 8)
    assert servers.qt_version_of("libQatServer.5.15.so") == (5, 15)
    assert servers.qt_version_of("libinjector.so") is None


def test_the_split_is_at_qt_68(bin_dir):
    verdicts = {library.name: servers.loads_on(library, JAMMY)
                for library in servers.survey(bin_dir)}
    assert verdicts["libQatServer.6.7.so"] is True
    assert verdicts["libQatServer.6.8.so"] is False
    assert verdicts["libQatServer.6.9.so"] is False


def test_a_newer_machine_has_no_problem(bin_dir):
    assert all(servers.loads_on(library, NOBLE)
               for library in servers.survey(bin_dir))
    assert servers.repairs(servers.survey(bin_dir), NOBLE) == {}


def test_an_unmeasurable_machine_is_left_alone(bin_dir):
    """Not knowing is a reason to say nothing, not a reason to rearrange
    somebody's installation."""
    unknown = servers.Machine(glibc=None, glibcxx=None)
    assert servers.repairs(servers.survey(bin_dir), unknown) == {}


# --- repairing --------------------------------------------------------------

def test_the_stand_in_is_the_newest_one_that_loads(bin_dir):
    plan = servers.repairs(servers.survey(bin_dir), JAMMY)
    assert set(path.name for path in plan) == {"libQatServer.6.8.so",
                                               "libQatServer.6.9.so"}
    assert all(standin.name == "libQatServer.6.7.so"
               for standin in plan.values())


def test_a_stand_in_is_never_newer_than_what_it_replaces(bin_dir):
    """Qt is binary compatible forward, not backward: a library built against
    6.9 inside a 6.8 application is not a substitution, it is a crash."""
    plan = servers.repairs(servers.survey(bin_dir), JAMMY)
    for target, standin in plan.items():
        assert standin.qt < servers.qt_version_of(target.name)


def test_a_stand_in_comes_from_the_same_major_version(tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir()
    fake_server(folder, "5.15", "2.14", "3.4.26")
    fake_server(folder, "6.8", "2.38", "3.4.32")
    # Qt 5 cannot stand in for Qt 6, so there is no repair to offer.
    assert servers.repairs(servers.survey(folder), JAMMY) == {}


def test_the_original_is_kept_and_can_be_restored(bin_dir):
    original = (bin_dir / "libQatServer.6.8.so").read_bytes()
    standin = (bin_dir / "libQatServer.6.7.so").read_bytes()

    servers.apply_repairs(servers.repairs(servers.survey(bin_dir), JAMMY))
    assert (bin_dir / "libQatServer.6.8.so").read_bytes() == standin
    kept = bin_dir / ("libQatServer.6.8.so" + servers.BACKUP_SUFFIX)
    assert kept.read_bytes() == original

    restored = servers.restore(bin_dir)
    assert (bin_dir / "libQatServer.6.8.so").read_bytes() == original
    assert not kept.exists()
    assert len(restored) == 2


def test_repairing_twice_does_not_lose_the_original(bin_dir):
    """The second run must not back up the stand-in over the original."""
    original = (bin_dir / "libQatServer.6.8.so").read_bytes()
    for _ in range(2):
        servers.apply_repairs(servers.repairs(servers.survey(bin_dir), JAMMY))
    kept = bin_dir / ("libQatServer.6.8.so" + servers.BACKUP_SUFFIX)
    assert kept.read_bytes() == original


def test_a_substituted_server_is_not_measured_as_a_candidate(bin_dir):
    """The backup sits in the same folder and must not be surveyed as though
    it were another server Qat might choose."""
    servers.apply_repairs(servers.repairs(servers.survey(bin_dir), JAMMY))
    names = [library.name for library in servers.survey(bin_dir)]
    assert all(not name.endswith(servers.BACKUP_SUFFIX) for name in names)
    assert servers.substituted(bin_dir)


# --- saying it --------------------------------------------------------------

def test_the_report_names_the_file_the_version_and_the_cure(bin_dir):
    text = servers.report(bin_dir, JAMMY)
    assert "libQatServer.6.8.so" in text
    assert "2.38" in text
    assert "WILL NOT LOAD HERE" in text
    assert "qat-servers --fix" in text


def test_a_machine_with_nothing_to_repair_says_so(bin_dir):
    text = servers.report(bin_dir, NOBLE)
    assert "Nothing to repair" in text
    assert "WILL NOT LOAD HERE" not in text


def test_the_report_survives_an_empty_directory(tmp_path):
    assert "No Qat server libraries found" in servers.report(tmp_path, JAMMY)


def test_a_launch_that_times_out_names_this_as_a_possible_cause(monkeypatch,
                                                                bin_dir):
    """From the recorder's side an unloadable server is indistinguishable from
    an application that is simply slow. The only evidence is a line in the
    application's own output, which nobody reads until somebody says to."""
    from qat_recorder import launch

    monkeypatch.setattr(servers, "qat_bin_dir", lambda: bin_dir)
    monkeypatch.setattr(servers.Machine, "here", classmethod(lambda cls: JAMMY))

    hint = launch.server_hint()
    assert "libQatServer.6.8.so" in hint
    assert "qat-servers" in hint


def test_the_hint_stays_quiet_when_there_is_nothing_to_say(monkeypatch,
                                                           bin_dir):
    from qat_recorder import launch

    monkeypatch.setattr(servers, "qat_bin_dir", lambda: bin_dir)
    monkeypatch.setattr(servers.Machine, "here", classmethod(lambda cls: NOBLE))
    assert launch.server_hint() == ""


def test_what_this_machine_provides_is_asked_of_this_machine():
    """Both may be None off Linux; neither may be a guess."""
    machine = servers.Machine.here()
    for value in (machine.glibc, machine.glibcxx):
        assert value is None or isinstance(value, tuple)


def test_the_embedded_qml_plugin_goes_in_under_every_version_qat_ships(tmp_path):
    """The server loads plugins ending in the Qt version it was BUILT for, and
    after --fix that is not the one in its file name: the 6.7 server standing in
    as libQatServer.6.8.so looks for 6.7.so. Installed under every 6.x Qat's QML
    plugin ships for, whichever server loads finds it -- and never under Qt 5,
    which a Qt 6 build cannot serve."""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    for version in ("5.15", "6.2", "6.7", "6.8", "6.10"):
        (plugins / f"libQmlPlugin.{version}.so").write_bytes(b"qat's")
    built = tmp_path / "build" / "libQatrecEmbeddedPlugin.6.so"
    built.parent.mkdir()
    built.write_bytes(b"ours")

    installed = servers.install_embedded_plugin(built, bin_dir=tmp_path)

    assert sorted(path.name for path in installed) == [
        "libQatrecEmbeddedPlugin.6.10.so", "libQatrecEmbeddedPlugin.6.2.so",
        "libQatrecEmbeddedPlugin.6.7.so", "libQatrecEmbeddedPlugin.6.8.so"]
    assert all(path.read_bytes() == b"ours" for path in installed)
    assert not (plugins / "libQatrecEmbeddedPlugin.5.15.so").exists()


def test_the_embedded_qml_plugin_is_built_with_the_filter():
    text = (Path(__file__).resolve().parents[1] / "native"
            / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "add_library(qatembedded SHARED qatembedded.cpp)" in text
    assert 'OUTPUT_NAME "QatrecEmbeddedPlugin.${QT_VERSION_MAJOR}"' in text
