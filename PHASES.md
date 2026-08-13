# What each phase built

Consolidated from the per-phase summaries. Kept because the bugs are the useful
part — most of them are things that only appear when code meets reality, and
several would be easy to reintroduce.

---

## Phase 0 — Spike

Six questions, answered before writing anything. Tree traversal and callback
arguments came back clean; the distribution matrix did not. Full results in
[FINDINGS.md](FINDINGS.md).

**Found:** Qat already ships an object spy (`qat/gui/spy/`) and a picker API, so
neither needed rebuilding. No Linux native plugin ships at all. Prebuilt server
libraries are not built against a common floor, so a source build is required
from day one for a fleet spanning Ubuntu, RHEL, CentOS and Rocky.

---

## Phase 1 — Action IR and the name resolver

The parts that do not depend on how capture works, so they survive unchanged
under any capture backend.

Injection could not run on the development machine, so the resolver was built
against a **five-operation backend port** with an in-memory implementation. That
was the intended architecture arriving early rather than scaffolding — and it
made the whole resolver testable in 0.1 s with no application, display or
injection.

The risk it created was made explicit: `FakeBackend` reimplements Qat's match
semantics from documentation. `tests/test_conformance.py` pins the assumptions
against a real server, and **on its first run it caught a credential leak** —
`echoMode` is a string, not a number, so no password field was being redacted
while all 33 offline tests passed.

**Bugs found:**

- **Sibling ordering was reversed.** `FakeBackend.walk()` used a stack, flipping
  sibling order, so identical buttons resolved to indices 1 and 0. The test
  suite did not catch it — it only asserted the indices *differed*. Found by
  printing the resolver's actual output. Traversal order is now a documented
  contract, pinned on both the fake and the live server.
- **Positional fallbacks were unscoped.** `{type, text}` + index was preferred
  over the equally-matching `{type, text, parent}` + index; the scoped form is
  safer because a new matching object elsewhere cannot shift the index.
- **Audit suggestions grouped by the wrong container** — by whatever scope the
  *definition* needed rather than the object's real parent, so the advice pointed
  at the wrong file.

---

## Phase 2 — The event filter

A **standalone shared library** preloaded alongside Qat's injector, rather than a
fork of Qat's server: it never touches Qat's source or protocol, upgrades
independently, and stays upstreamable.

Verified with `xdotool` against a real Xvfb server — the only honest test, since
nothing in-process can fake a spontaneous event. **33 records from real input,
zero from `button->click()` and `setChecked(true)`.**

**Found:** building on Ubuntu 24.04 produced a `GLIBC_2.38` artifact, unusable on
RHEL 9 — the exact mistake criticised in upstream, appearing the moment the
"build against the oldest target" rule was not followed. Rebuilt on AlmaLinux 8:
`GLIBC_2.17`, which loads everywhere including EL8.

---

## Phase 3 — Capture and code generation

Raw events → semantic actions → pytest and Gherkin, joined by a container run
that goes from a real mouse click to a compiling test module.

Three decisions worth knowing:

- **Typed text is read back from the widget, not reconstructed from keystrokes.**
  Survives backspaces, paste and IME; keeps characters in one place where
  `echoMode` is known.
- **The same click arrives several times** — Qt delivers to the window and then
  the widget. Grouped by timestamp; the most specific recipient wins.
- **A press and a release are one click.**

**Bugs found:**

- **Typing into a checkbox produced a fabricated step.** Tab moved focus onto a
  checkbox mid-typing and the flush read its `text` property — its *caption* —
  back as user input. None of the 18 capture tests caught it, because they all
  typed into a `QLineEdit`.
- **Qat locks the UI during a session**, which blocks the very input being
  captured.
- **Qat overwrites `LD_PRELOAD`** with its own injector, so the filter must be
  appended from inside the launched process.
- **Generated Gherkin repeated `When`** on every line instead of chaining with
  `And`.

---

## Phase 4 — Control panel

All logic in `RecorderController`, a plain Python class with **no Qt import**;
the widgets only wire signals to it. 13 tests cover the entire state machine with
no display, no application, no human.

Checkpoints reuse the click stream rather than Qat's picker: `activate_picker()`
only toggles a server-side mode and gives the client no way to learn what was
picked. "Add checkpoint" arms picking, and the next click selects a target
instead of being recorded — **including its release**, since dropping only the
press leaves an orphan release that the folder correctly interprets as a drag.

**Bugs found:**

- **Actions appeared one step late.** Event groups only closed when the *next*
  interaction arrived, so a live panel showed nothing until the operator did
  something else.
- **Durability colours were unreadable on a dark theme** — chosen against a light
  background, worst on the selected row. Found by rendering the panel and looking
  at it.

---

## Phase 5 — CI, packaging, and replay

`ci/run.sh` is the definition of green and runs locally; the GitLab and GitHub
files are thin wrappers. Five stages, cheapest first.

The important addition was **running** a generated test rather than only
compiling it. Compiling proves syntax; running proves the recorder understood
what happened — and it immediately found three defects in shortcut handling:

- Shortcuts were attributed to the focused widget, but Qt dispatches them at
  window scope.
- Once bound to a `QAction`, shortcuts **vanished from recordings entirely** —
  Qt consumes the key and sends `QEvent::Shortcut`, which is not spontaneous.
  Silently dropping every application shortcut is worse than failing.
- `QEvent::Shortcut` is delivered to the `QAction`, which is not a widget;
  retargeting to the outermost ancestor lands on a `QWidgetWindow`, which fails
  identically.

**Also found:** `-static-libgcc` raised the artifact's glibc floor via a single
symbol, and nominal distribution glibc versions are not a compatibility test.
Both in [FINDINGS.md §3](FINDINGS.md).

---

## Phase 6 — The remote agent

An agent per VM, running the Qat client beside the application so only the Action
IR crosses the network. Stdlib only — `http.server` plus `ssl` — with long-polled
reads instead of WebSocket, so installing on a locked-down host needs no web
framework.

`RemoteRecorderController` implements the same surface as the local one and
**no widget changed**. Recording is exclusive per host; a second tester is told
who holds it and since when.

**Bugs found:**

- **Actions delivered twice** — the reader used the *applied* cursor, so looping
  before the caller polled re-fetched the same range.
- **Undo shifted every later index**, so a reader holding cursor *N* skipped one.
- **`stop()` released the host**, destroying the session before `save()` could
  fetch anything. Stopping and giving up the machine are different acts.
- **A spurious `launch` row appeared only when remote**, because locally that
  action is created before callbacks attach. It also misaligned the detail pane,
  which filtered its list differently — a latent bug that only remote exposed.

---

## Packaging, found by deploying

Three assumptions that were invisible from a development checkout and broke on a
real VM:

- **`wrapper.sh` was located by a source-checkout path** and was not in the wheel
  at all, so `pip install` produced a recorder that could not launch anything.
- **The C++ source was not shipped**, so `build-filter` had nothing to compile on
  a machine that only had the wheel.
- **`-static-libstdc++` was hardcoded on**, and RHEL does not install
  `libstdc++.a` by default — so a perfectly reasonable local build died on a flag
  that bought it nothing.

---

## The incident

`install.sh uninstall` destroyed the repository. It asked Qat where its
configuration lived; Qat returned a path in the **current working directory**,
because that is where it writes `applications.json`. The script took that path's
*parent* and removed it.

The guard checked for `/`, `$HOME`, `.` and `..` — every case except the one that
occurred. It was then run with `--yes`, skipping the confirmation that would have
shown the repository in the list.

Recovered from the last built wheel, which contained the complete Python package
and the C++ source; everything else was rebuilt. The rules now in `install.sh`:
the directory list is fixed rather than derived, every entry is checked to be
under `$HOME`, and config files reported by another program are removed as
**files**, never as parent directories.

The project also had no version control until after the loss. It does now.
