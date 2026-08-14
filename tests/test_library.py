# -*- coding: utf-8 -*-
"""
The saved suite.

A recording that lands in `./recorded` is a scratch file that the next one
overwrites. A suite is a different thing: named tests, grouped by application,
each in its own directory, on the machine that can run them.
"""

import json

import pytest

from qat_recorder.library import TestLibrary, slug

FILES = {"test_recorded.py": "def test_ok():\n    assert True\n",
         "recording.json": "{}"}


@pytest.fixture()
def library(tmp_path):
    return TestLibrary(root=tmp_path)


@pytest.mark.parametrize("raw, expected", [
    ("Add a torrent", "add-a-torrent"),
    ("  Login / logout  ", "login-logout"),
    # Accents are dropped rather than transliterated: the directory name only
    # has to be stable and safe, and meta.json keeps the name as written.
    ("Ünïcode name", "n-code-name"),
    ("", "test"),
    ("../../etc/passwd", "etc-passwd"),
])
def test_names_become_safe_directory_names(raw, expected):
    assert slug(raw) == expected


def test_a_saved_test_keeps_the_name_a_person_typed(library):
    case = library.save("qbittorrent", "Add a torrent", FILES)
    assert case.name == "Add a torrent"          # as written, for reading
    assert case.directory.name == "add-a-torrent"  # as stored, for the filesystem
    assert case.id == "qbittorrent/add-a-torrent"
    assert (case.directory / "test_recorded.py").exists()


def test_two_tests_with_one_name_are_two_tests(library):
    """The second must not overwrite the first: that is a minute of someone's
    clicking, gone."""
    first = library.save("app", "login", FILES)
    second = library.save("app", "login", FILES)
    assert first.directory != second.directory
    assert second.directory.name == "login-2"
    assert first.directory.exists()


def test_tests_are_grouped_by_application(library):
    library.save("qbittorrent", "one", FILES)
    library.save("hmi", "two", FILES)

    assert sorted(library.apps()) == ["hmi", "qbittorrent"]
    assert [case.name for case in library.list("qbittorrent")] == ["one"]
    assert [case.name for case in library.list("hmi")] == ["two"]
    assert len(library.list()) == 2


def test_a_saved_test_records_what_it_cost(library):
    case = library.save("app", "login", FILES, app_path="/bin/app",
                        summary={"actions": 7, "unresolved": 2, "fragile": 1})
    assert case.steps == 7
    assert case.unresolved == 2
    assert case.needs_review == 1

    meta = json.loads((case.directory / "meta.json").read_text(encoding="utf-8"))
    assert meta["app_path"] == "/bin/app"
    assert meta["name"] == "login"


def test_an_unnamed_test_is_refused(library):
    with pytest.raises(ValueError, match="needs a name"):
        library.save("app", "   ", FILES)


def test_a_test_can_be_fetched_by_id(library):
    saved = library.save("app", "login", FILES)
    assert library.get(saved.id).directory == saved.directory


@pytest.mark.parametrize("bad", [
    "../etc", "app", "", "app/../../etc", "a/b/c", "./app/login",
])
def test_an_id_cannot_escape_the_library(library, bad):
    """Test ids arrive over a network."""
    library.save("app", "login", FILES)
    with pytest.raises(LookupError):
        library.get(bad)


def test_a_damaged_entry_does_not_hide_the_others(library):
    library.save("app", "good", FILES)
    broken = library.root / "app" / "broken"
    broken.mkdir(parents=True)
    (broken / "meta.json").write_text("{not json", encoding="utf-8")

    names = [case.name for case in library.list("app")]
    assert "good" in names          # the readable one still lists


def test_an_entry_with_no_metadata_still_counts_if_it_is_runnable(library):
    folder = library.root / "app" / "hand-made"
    folder.mkdir(parents=True)
    (folder / "test_recorded.py").write_text("def test_x():\n    pass\n",
                                             encoding="utf-8")
    listed = library.list("app")
    assert [case.name for case in listed] == ["hand-made"]
    assert listed[0].runnable is True


def test_an_empty_directory_is_not_a_test(library):
    (library.root / "app" / "nothing-here").mkdir(parents=True)
    assert library.list("app") == []


def test_the_root_can_be_pointed_elsewhere(tmp_path, monkeypatch):
    """So a team can put the suite on shared storage, and so tests do not write
    into a real home directory."""
    monkeypatch.setenv("QATREC_TESTS", str(tmp_path / "suite"))
    assert TestLibrary().root == tmp_path / "suite"
