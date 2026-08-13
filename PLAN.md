# Architecture and the reasoning behind it

Written before any code existed, and kept because the reasoning still holds.
Where this disagrees with [FINDINGS.md](FINDINGS.md), FINDINGS wins — it is
measurement, this is argument.

---

## 1. The problem

**Playback was already solved.** Qat ships the full input-simulation API —
`mouse_click`, `double_click`, `mouse_drag`, `type_in`, `shortcut`, `press_key`,
touch, pinch, flick — with `wait_for_object` synchronisation built into every
call that takes an object definition, and documented synchronous event execution.
We are not building an automation engine; we are building a code generator that
emits into one that already exists.

**Recording was not.** Qat can see every object but cannot tell a human's click
from `button->click()` in your own code. Both look identical from outside the
process.

---

## 2. Why an in-process event filter, and not signal hooking

The obvious cheap approach is to hook Qt signals from the Python client and
correlate them against OS-level input. It does not work on this platform.

| | Signal hooking (client-side) | Event filter (in-process C++) |
|---|---|---|
| User vs. programmatic | Cannot distinguish — `setChecked()` fires the same signal a click does. Needs an OS-input timing heuristic with false positives and negatives. | `QEvent::spontaneous()` is exact, and auto-ignores our own playback events. |
| Wayland | Broken — no XRecord equivalent, by design. Needs evdev plus `input` group on every machine. | Unaffected; never touches the display server. |
| Objects created later | A dialog opened after hook time was never connected; its interactions are silently lost. | Sees every event to every object, always. |
| Custom widgets, drag, scroll | Invisible — no semantic signal exists. | Fully captured. |
| Cost on Linux | Low, but fragile. | gcc only; `LD_PRELOAD` is already the mechanism. |

On Windows the C++ path would have meant an MSVC × Qt matrix. On Linux the axis
that gets harder — Wayland — is exactly the one the C++ path eliminates, while
the axis that made C++ expensive collapses to a single compiler.

Qat is MIT-licensed, so the filter is upstreamable rather than a permanent fork.
It uses only public Qt API and does not touch Qat's source.

---

## 3. Structure

```
┌─ Application under test (dynamic Qt) ─────────────────────────┐
│  LD_PRELOAD                                                   │
│    ├─ Qat's injector → version detect → libQatServer          │
│    │     properties, methods, object discovery, playback      │
│    └─ libqatrec.so  (ours)                                    │
│          type switch → spontaneous() gate → queue → socket    │
└───────────────────────────┬───────────────────────────────────┘
                            │ TCP, newline-delimited JSON
┌───────────────────────────┴───────────────────────────────────┐
│  Recorder process (Python)                                    │
│    events.py    wire format                                   │
│    capture.py   raw events → semantic actions                 │
│    naming.py    definition synthesis + live validation        │
│    ir.py        normalized Action IR (JSON on disk)           │
│    emit/        IR → pytest | Gherkin                         │
│    player.py    replay IR directly                            │
│    ui/          control panel (Qt-free controller + widgets)  │
│    agent/       the same, driven from another machine         │
└───────────────────────────────────────────────────────────────┘
```

### The Action IR is the load-bearing abstraction

Recording produces normalised JSON; every generator consumes it. That means the
capture backend can be swapped, object names can be re-resolved, and output can be
re-emitted in either format — all without re-recording.

It also turned out to be the right network boundary: when the remote agent was
built, the IR was already exactly what needed to cross the wire. A seam that
lands in the right place twice is probably in the right place.

### Two seams, both introduced for testability, both paid off

**The backend port** (`top_windows`, `children`, `parent`, `properties`,
`find_all`, `identity`) exists so the resolver can be tested against an in-memory
tree. It is also what makes the capture backend swappable.

**The Qt-free controller** exists so the panel's state machine can be tested
without a display. It is also what let a remote controller drop in with no change
to any widget.

### Event filter design constraints

A global filter on `QCoreApplication` runs in the main thread for *every* event.
It must be cheap:

1. Switch on event type first; reject everything else immediately.
2. Check `spontaneous()`.
3. Resolve the object's locator inline — bounded, and only for the few events
   that survive the switch. A `QObject*` cannot safely be dereferenced later from
   another thread.
4. **Never** do TCP I/O in the filter. A writer thread drains a queue.

---

## 4. Object naming — the quality ceiling

A recorded script is only as durable as the definitions in it. The resolver
escalates from most to least durable and, critically, **validates each candidate
against the live application before accepting it**: a candidate is used only if
`find_all()` returns exactly the object being named.

| Strategy | Robustness |
|---|---|
| `objectName` | STRONG |
| `objectName` + `type` | STRONG |
| QML `id` | STRONG |
| `type` + a stable non-visible property | MODERATE |
| `type` + visible text | WEAK — breaks under translation |
| any of the above + `parent`/`container` | MODERATE |
| definition + positional index | FRAGILE — breaks on reorder |
| — | UNRESOLVED, recorded honestly rather than guessed |

Two details that matter:

**`parent` versus `container`.** `container` searches descendants recursively, so
scoping by an outer widget can still match a nested duplicate. Only `parent`,
which matches direct children, disambiguates. It is tried first.

**Indices are not part of the definition.** Qat's format has no index selector, so
a positional reference cannot be expressed as one. It lives in `Target.index`,
which emitters render as `find_all_objects(...)[n]`.

Ambiguity surfaces during recording rather than weeks later in CI. Squish's
recorder does not reliably do this; it is the clearest advantage here.

---

## 5. Credentials

The filter does **not** transmit typed characters. A run of key presses only
marks which field is being edited; the value is read back from the widget through
Qat when the run ends.

That is more correct than replaying keystrokes — it survives backspaces,
selection replacement, paste and IME composition — and it means the characters
exist in exactly one place, where `echoMode` is known, so redaction happens
before anything reaches disk rather than after. A crash dump or packet capture
cannot leak them either.

---

## 6. The build matrix

**Build against the oldest version you support**, on every axis. Old-built runs on
new; new-built does not run on old.

Upstream does not follow this rule, which is why its Qt 6.8+ libraries will not
load on RHEL 9 or Ubuntu 22.04, and why nothing loads on EL8. See
[FINDINGS.md §3](FINDINGS.md) for the measured numbers and the two traps —
`-static-libgcc` raising the floor, and nominal glibc versions being unreliable.

---

## 7. Remote

Qat is built for one machine: it discovers the injected server's port through a
file on the local filesystem, and the application dials back to `127.0.0.1`.
Rather than tunnel around that, the agent runs the Qat client **on the VM**,
beside the application, and only the Action IR crosses the network.

No web framework: `http.server` plus `ssl` gives HTTPS and a bearer token from
the standard library, and long-polling replaces WebSocket since events arrive at
human speed. Installing on a locked-down RHEL box is `pip install qat` plus this
package.

The agent launches processes on request, so it *is* a remote execution service.
Token and TLS are therefore mandatory rather than optional, and the CLI refuses
to start without them.
