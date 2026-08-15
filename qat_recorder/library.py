# -*- coding: utf-8 -*-
"""
Where recordings live once they are worth keeping.

A recording that lands in `./recorded` is a scratch file: the next one
overwrites it, and nothing says which application it belongs to or what it was
meant to prove. A suite is a different thing, so it gets a different place --
one directory per test, grouped by application, on the machine where the
application runs:

    ~/qatrec-tests/
        qbittorrent/
            add-a-torrent/
                meta.json
                recording.json
                test_recorded.py
                recorded.feature
                steps.py
                unresolved.txt
            filter-by-name/
                ...

Three properties are deliberate.

**Grouped by application.** The panel asks for "the tests for this app", which
is the only question anyone actually asks of a suite.

**One directory per test, never shared.** Replay runs with that directory as the
working directory, and Qat writes its `applications.json` into the working
directory -- so tests that shared a folder would quietly share state.

**Named by a person.** The directory name is a slug of the name the operator
typed; `meta.json` keeps the name as they wrote it. A suite is read by people,
and `a7f3c091` is not a test case.

Nothing here deletes anything. Removing a test is `rm -r` on a directory the
operator can see, which is a great deal safer than a convenience function --
this project has already destroyed a repository once by deleting a path it had
derived rather than been given.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

#: Overridable so tests do not touch a real home directory, and so a team can
#: put the suite on shared storage.
ROOT_ENV = "QATREC_TESTS"

DEFAULT_ROOT = "~/qatrec-tests"

GENERATED = ("recording.json", "test_recorded.py", "recorded.feature",
             "steps.py", "unresolved.txt")


def slug(text: str, fallback: str = "test") -> str:
    """A directory name from something a person typed."""
    cleaned = re.sub(r"[^0-9a-zA-Z._-]+", "-", (text or "").strip()).strip("-.")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return (cleaned or fallback).lower()[:60]


@dataclass
class TestCase:
    """One saved recording."""

    #: Not a pytest test class, despite the name. Without this, pytest collects
    #: it wherever it is imported and warns about the constructor -- in the
    #: user's own suite as well as ours.
    __test__ = False

    app: str
    name: str
    directory: Path
    created: str = ""
    steps: int = 0
    unresolved: int = 0
    needs_review: int = 0
    app_path: str = ""
    #: What happened when this test was last run: "passed", "failed", or empty
    #: for never. Written the moment it is saved, because a test nobody has run
    #: is a guess, and the person who can still do something about it is the one
    #: who just recorded it.
    verified: str = ""
    verified_at: str = ""

    @property
    def id(self) -> str:
        """How a client refers to it: `app/name`, both already slugs."""
        return f"{self.app}/{self.directory.name}"

    @property
    def runnable(self) -> bool:
        return (self.directory / "test_recorded.py").exists()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "app": self.app,
            "name": self.name,
            "directory": str(self.directory),
            "created": self.created,
            "steps": self.steps,
            "unresolved": self.unresolved,
            "needs_review": self.needs_review,
            "app_path": self.app_path,
            "runnable": self.runnable,
            "verified": self.verified,
            "verified_at": self.verified_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TestCase":
        return cls(
            app=str(data.get("app", "")),
            name=str(data.get("name", "")),
            directory=Path(str(data.get("directory", ""))),
            created=str(data.get("created", "")),
            steps=int(data.get("steps", 0) or 0),
            unresolved=int(data.get("unresolved", 0) or 0),
            needs_review=int(data.get("needs_review", 0) or 0),
            app_path=str(data.get("app_path", "")),
            verified=str(data.get("verified", "")),
            verified_at=str(data.get("verified_at", "")),
        )


@dataclass
class TestLibrary:
    """The suite on this machine."""

    __test__ = False                       # see TestCase

    root: Path = field(default_factory=lambda: Path(
        os.environ.get(ROOT_ENV) or DEFAULT_ROOT).expanduser())

    def __post_init__(self):
        self.root = Path(self.root).expanduser()

    # -- writing -----------------------------------------------------------

    def reserve(self, app: str, name: str) -> Path:
        """A directory for a new test, never an existing one.

        Two recordings called "login" are two tests, not one revision of one:
        the second becomes `login-2`. Silently overwriting the first would lose
        work that took a person a minute of clicking to produce.
        """
        base = slug(name, "test")
        parent = self.root / slug(app, "app")
        candidate = parent / base
        suffix = 2
        while candidate.exists():
            candidate = parent / f"{base}-{suffix}"
            suffix += 1
        return candidate

    def save(self, app: str, name: str, files: dict,
             app_path: str = "", summary: Optional[dict] = None) -> TestCase:
        """Write a recording into the library and return what was written."""
        if not str(name).strip():
            raise ValueError("a saved test needs a name")

        directory = self.reserve(app, name)
        directory.mkdir(parents=True)

        for filename, text in files.items():
            (directory / filename).write_text(text, encoding="utf-8")

        summary = summary or {}
        case = TestCase(
            app=directory.parent.name,
            name=str(name).strip(),
            directory=directory,
            created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            steps=int(summary.get("actions", 0) or 0),
            unresolved=int(summary.get("unresolved", 0) or 0),
            needs_review=int(summary.get("fragile", 0) or 0),
            app_path=app_path,
        )
        (directory / "meta.json").write_text(
            json.dumps(case.to_dict(), indent=2), encoding="utf-8")
        return case

    def record_verdict(self, case: TestCase, result: Mapping[str, Any]) -> TestCase:
        """Store what happened when this test was run.

        Kept in the test's own meta.json rather than a central report, so a
        directory that is copied to another machine carries its own history, and
        so nothing has to be reconciled if two people record at once.
        """
        case.verified = "passed" if result.get("ok") else "failed"
        case.verified_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        document = case.to_dict()
        # The failure output goes in the metadata too. Whoever reads this later
        # wants to know why, not merely that.
        document["last_output"] = str(result.get("output") or "")[-8000:]
        (case.directory / "meta.json").write_text(
            json.dumps(document, indent=2), encoding="utf-8")
        return case

    # -- reading -----------------------------------------------------------

    def apps(self) -> list:
        if not self.root.exists():
            return []
        return sorted(child.name for child in self.root.iterdir()
                      if child.is_dir())

    def list(self, app: str = "") -> list:
        """Saved tests, newest last, for one application or for all of them.

        A directory that cannot be read is skipped rather than raising: a suite
        with one damaged entry should still list the other forty.
        """
        found = []
        for folder in self._folders(app):
            case = self._read(folder)
            if case is not None:
                found.append(case)
        return sorted(found, key=lambda case: (case.app, case.created))

    def get(self, test_id: str) -> TestCase:
        """Resolve `app/name`, refusing anything that escapes the library."""
        parts = [part for part in str(test_id).split("/") if part]
        if len(parts) != 2 or any(part in (".", "..") for part in parts):
            raise LookupError(f"not a test id: {test_id!r}")

        directory = (self.root / parts[0] / parts[1])
        # Belt and braces: the id is checked above, and the resolved path is
        # checked to be inside the library here. A test id arrives over a
        # network, and `../../etc` is the oldest trick there is.
        try:
            directory.resolve().relative_to(self.root.resolve())
        except (ValueError, OSError) as error:
            raise LookupError(f"not a test id: {test_id!r}") from error

        case = self._read(directory)
        if case is None:
            raise LookupError(f"no saved test {test_id!r}")
        return case

    # -- helpers -----------------------------------------------------------

    def _folders(self, app: str = ""):
        if not self.root.exists():
            return
        parents = ([self.root / slug(app, "app")] if app
                   else [child for child in self.root.iterdir() if child.is_dir()])
        for parent in parents:
            if not parent.is_dir():
                continue
            for folder in sorted(parent.iterdir()):
                if folder.is_dir():
                    yield folder

    def _read(self, folder: Path) -> Optional[TestCase]:
        if not folder.is_dir():
            return None
        meta: dict[str, Any] = {}
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # No metadata, or unreadable. Still a test if it has a test in it.
            if not (folder / "test_recorded.py").exists():
                return None
        case = TestCase.from_dict({**meta, "app": folder.parent.name,
                                   "name": meta.get("name") or folder.name})
        case.directory = folder            # authoritative: it is where we found it
        return case
