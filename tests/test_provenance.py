# -*- coding: utf-8 -*-
"""
Which build is installed.

Every wheel is called `qat_recorder-0.1.0-py3-none-any.whl`. Four of them with
different contents and the same name crossed to the same machine, and a stale
copy installed exactly as quietly as a fresh one -- so a recording failed in
the old way, and the log was indistinguishable from the log the fix produces.
Two round trips were spent establishing what was installed rather than what was
wrong.

A build that says what it is ends that.
"""

from __future__ import annotations

from qat_recorder import provenance


def test_the_build_id_is_stable_for_the_same_files():
    assert provenance.build_id() == provenance.build_id()
    assert len(provenance.build_id()) == 12


def test_the_build_id_follows_the_files_that_change_behaviour(monkeypatch,
                                                              tmp_path):
    """A hash of the version number would say nothing: the version has been
    0.1.0 throughout."""
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / "native").mkdir()
    for name in provenance.PARTS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("one", encoding="utf-8")

    monkeypatch.setattr(provenance, "_package_dir", lambda: tmp_path)
    before = provenance.build_id()
    (tmp_path / "resources" / "wrapper.sh").write_text("two", encoding="utf-8")
    assert provenance.build_id() != before


def test_every_named_feature_is_present_in_this_checkout():
    """The list is the record of what each round trip cost. If a marker stops
    matching, either the fix was reverted or the marker moved -- both worth
    stopping for."""
    missing = [label for label, present in provenance.features()
               if not present]
    assert missing == [], f"this build is missing: {missing}"


def test_an_older_build_is_described_as_older(monkeypatch, tmp_path):
    for name in provenance.PARTS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("an older wrapper with none of the markers",
                        encoding="utf-8")
    monkeypatch.setattr(provenance, "_package_dir", lambda: tmp_path)

    text = provenance.describe()
    assert "[ ] no injector in the launching shell" in text
    assert "predates the fixes" in text
    assert "same file name" in text


def test_it_survives_an_installation_that_is_missing_pieces(monkeypatch,
                                                            tmp_path):
    monkeypatch.setattr(provenance, "_package_dir", lambda: tmp_path)
    assert provenance.build_id()                      # must not raise
    assert "qat-recorder" in provenance.describe()


def test_the_one_line_form_carries_the_build():
    line = provenance.one_line()
    assert provenance.build_id() in line
    assert "qat-recorder" in line


def test_the_agent_says_which_build_it_is_on_startup():
    """The first line of every log that gets pasted."""
    from pathlib import Path

    source = (Path(provenance.__file__).parent / "agent" / "cli.py").read_text(
        encoding="utf-8")
    assert "provenance.one_line()" in source


def test_a_generated_token_is_all_that_reaches_stdout(capsys):
    """`--generate-token > ~/.qatrec/token` is how install.sh makes the token
    file. A banner on stdout became its first line, the agent took both lines as
    the token, and every panel got 401 with the right token in hand."""
    from qat_recorder.agent import cli

    assert cli.main(["--generate-token"]) == 0
    out = capsys.readouterr().out
    assert len(out.split()) == 1, out
