# QAT Recorder

Record-and-playback tooling for [Qat (Qt Application Tester)](https://qat.readthedocs.io/),
targeting Linux.

Qat can drive a Qt application and inspect its objects, but it cannot record one:
nothing in it can tell a human's click apart from `button->click()` in your own
code. This project adds that, and turns what it captures into pytest or Gherkin.

```
you click a button
   ↓  native filter (C++, in-process)      QEvent::spontaneous() -> was this a person?
   ↓  capture (Python)                     raw events -> semantic actions
   ↓  resolver                             which object was that, durably?
   ↓  generator                            pytest / Gherkin you can commit
```

---

## Install

Two files: `install.sh` (or `install.ps1`) and the wheel, in the same folder.

```bash
bash install.sh vm            # everything needed to record on this machine
bash install.sh vm --agent    # ...plus the agent, for remote testers
bash install.sh doctor        # what is installed, what is missing
bash install.sh uninstall     # remove all of it
```

It detects the distribution, installs the compiler, cmake and the right Qt dev
packages, creates the environment, and builds the event filter.

On a tester's Windows machine:

```powershell
.\install.ps1 local
```

---

## Use

```bash
# 1. how nameable is your UI? (no filter needed)
python -m qat_recorder audit --launch ./myapp --pause

# 2. record — click around while it runs
python -m qat_recorder record --lib ~/qatrec-filter/libqatrec.5.15.so \
       --app ./myapp --seconds 60 --out ./recorded

# 3. replay — pure Qat, no filter
QATREC_SECRET_passwordField=… pytest recorded/test_recorded.py -v

# 4. re-generate from the IR at any time, without re-recording
python -m qat_recorder emit recorded/recording.json --format both --out generated/
```

`--pause` holds the application open so you can navigate to the screen you want
audited — dialogs and wizards are where the worst naming usually lives.

## Start with `audit`

You build these applications, so the cheapest fix for a fragile recording is an
`objectName` in the source, not cleverness in the recorder:

```
Scanned 502 objects — grading the 239 a person can operate
  (263 internal: layouts, models, scrollbars and the like. --all to include them)

  strong  120  50.2%    weak  51  21.3%    fragile  36  15.1%

Suggested source changes:
  - add objectName to 44 x QAction (currently fragile: positional…)
  - add objectName to 11 x QToolButton in 'toolBar' (currently weak: translation…)
```

Exit code is non-zero below a threshold, so it can gate CI on its own.

## Recording on a VM

The application usually lives on a Linux VM while the tester sits elsewhere. An
agent on each VM makes that work: it runs the Qat client beside the application,
so only the Action IR crosses the network.

```bash
# on the VM
bash install.sh vm --agent          # prints the fingerprint and the hosts add line

# on the tester's machine
qat-recorder hosts add vm-01 10.0.0.5:8765 --fingerprint 2c6e…04af \
                                           --token-file ~/.qatrec/token
qat-recorder panel --agent vm-01
```

The agent needs a token and TLS — it refuses to start otherwise — and testers pin
its certificate fingerprint, so there is no CA to run. One tester holds a host at
a time; a second is told who has it.

**Recording is interactive**, so the VM's display must be visible to the operator
(VNC, X forwarding, or the machine's own desktop). Replay needs no display, which
is why CI can run headless.

---

## Things worth knowing before you start

All measured, not assumed — see [FINDINGS.md](FINDINGS.md).

**QML embedded via `QQuickWidget` is invisible to Qat.** Zero objects, while the
same scene as a top-level `QQuickView` is fully readable and clickable. This
decides how testable your QML is.

**On Linux you cannot attach to a running application.** Injection is
`LD_PRELOAD`, which only applies at exec, so the recorder launches it.

**Qat resolves the application path through symlinks** and launches the resolved
target, bypassing wrapper scripts that set `LD_LIBRARY_PATH` or `QT_PLUGIN_PATH`.

**Passwords are never written to disk.** The filter does not transmit typed
characters at all; values are read back from the widget, where `echoMode` is
known, and redacted before anything is serialised.

**Prebuilt Qat server libraries do not cover this distro range.** Qt 6.8+ needs
glibc 2.38 and will not load on RHEL 9 or Ubuntu 22.04; nothing loads on EL8.

---

## Layout

| Path | What it is |
|---|---|
| `native/` | The C++ event filter, a test application, and its test scripts |
| `qat_recorder/ir.py` | The Action IR — the contract between capture and generation |
| `qat_recorder/backend.py` | The five-operation port onto Qat, plus an in-memory fake |
| `qat_recorder/naming.py` | Object-name resolution, validated against the live app |
| `qat_recorder/capture.py` | Raw events → semantic actions |
| `qat_recorder/emit/` | pytest and Gherkin generators |
| `qat_recorder/player.py` | Replay an IR directly, without generating code |
| `qat_recorder/agent/` | Remote agent, client, registry, remote controller |
| `qat_recorder/ui/` | Control panel; all logic in a Qt-free controller |
| `spike/` | Sample applications, including the QML embedding variants |
| `ci/run.sh` | The definition of "green"; runs locally too |

---

## Tests

```bash
bash ci/run.sh              # everything
bash ci/run.sh offline      # ~250 tests, no display, no Qt, seconds
```

| Suite | Needs |
|---|---|
| Offline (IR, resolver, capture, emitters, player, panel, agent) | Python |
| Live conformance — pins the fake against the real Qat server | a Linux desktop, `--live` |
| Native filter — real X input vs programmatic input | Docker |
| Record → emit → **execute** | Docker |

The conformance suite exists because the resolver is unit-tested against an
in-memory model of Qat's matching rules. On its first run it caught a real bug:
Qat reports `echoMode` as the string `'Password'`, not a number, so password
fields were not being redacted while every offline test passed.

---

## Documents

| | |
|---|---|
| [GETTING-STARTED.md](GETTING-STARTED.md) | The practical path, step by step |
| [FINDINGS.md](FINDINGS.md) | Everything established by measurement |
| [PLAN.md](PLAN.md) | Architecture and the reasoning behind it |
| [PHASES.md](PHASES.md) | What each phase built, and the bugs it found |
| [deploy/README.md](deploy/README.md) | Installing the agent on a VM |

---

## Status

All phases built and tested. Not yet done: the panel has not been driven against
a real in-house application, EL8 has not been tested on real hardware, the Qt 6
artifact covers 6.6–6.11 rather than 6.2+, and the filter has not been offered
upstream.
