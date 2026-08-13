# Getting started

A practical path from nothing to a recorded, replaying test. Each step is
checkable on its own, so when something breaks you know which layer broke.

**Steps 0–2 need none of this project's code and take under an hour. They tell
you whether the rest is worth doing.**

---

## Step 0 — Does Qat work with your application at all?

Nothing here is ours. If Qat cannot see your application, nothing built on top of
it can either.

```bash
python3 -m venv ~/qatrec && ~/qatrec/bin/pip install qat
```

Find out which Qt your application uses — it decides everything downstream:

```bash
ldd /opt/acme/bin/hmi | grep -i qt
# libQt6Core.so.6 => ... 6.6.2      -> Qt 6.6
```

Two things must be true: your application links Qt **dynamically** (the `ldd`
output proves it), and Qat ships a server for that Qt version.

```python
import qat
qat.test_settings.Settings.lock_ui = "never"
qat.register_application("hmi", "/opt/acme/bin/hmi")
ctx = qat.start_application("hmi")
print(len(qat.find_all_objects({"type": "QWidget"})))    # non-zero = working
qat.close_application(ctx)
```

**What to check:** `Successfully loaded Qat server`, then
`Application hmi successfully started and ready for testing`, and a non-zero
count.

### If it fails here

| Symptom | Cause |
|---|---|
| `Could not retrieve server port number` | the application died on launch — run it by hand first |
| ...and it runs fine by hand | **the symlink trap**: Qat resolves the path and launches the *resolved* binary, without whatever a wrapper script set up. Register the real target. |
| ...and the GUI appears anyway | a **single-instance guard** — the second copy handed off to the first and exited. `pkill` the original and retry. |
| No server library loads | see [FINDINGS.md §3](FINDINGS.md) — on EL8 no prebuilt Qat server loads at all |
| The app appears frozen | Qat locks input during a session; set `lock_ui = "never"` |

Note also that on Linux you **cannot attach** to an already-running application.
Qat has to launch it.

---

## Step 1 — How nameable is your UI?

The cheapest useful thing here, and it needs no filter.

```bash
~/qatrec/bin/pip install ./qat_recorder-0.1.0-py3-none-any.whl
~/qatrec/bin/python -m qat_recorder audit --launch /opt/acme/bin/hmi --pause
```

`--pause` keeps the application open so you can navigate to the screen you care
about — a dialog, a wizard page — and audit *that*. Those are usually where the
worst naming is, and they do not exist at startup.

**Act on the output before recording anything.** You build these applications, so
adding `objectName` in the source is far cheaper than any cleverness in a
recorder, and every `fragile` object is a test step that will break the next time
someone reorders a layout.

The report grades only objects a person can operate; layouts, data models and
scrollbars are counted but not graded. `--all` includes them.

---

## Step 2 — If you use QML, answer this now

**QML embedded with `QQuickWidget` is invisible to Qat.** Zero objects. The same
scene as a top-level `QQuickView` works completely.

```bash
~/qatrec/bin/python -c "
import qat
qat.test_settings.Settings.lock_ui='never'
qat.register_application('hmi','/opt/acme/bin/hmi'); qat.start_application('hmi')
print('QQuickItem:', len(qat.find_all_objects({'type':'QQuickItem'})))
"
```

Zero means your QML is unrecordable as currently embedded. Try
`QWidget::createWindowContainer` with a `QQuickView` —
`spike/qml_container_app.py` is written and ready to test exactly that.

If it is a QML application, install the declarative package first or the QML half
stays invisible regardless:

```bash
sudo dnf install qt5-qtdeclarative-devel     # or qt6-qtdeclarative-devel
```

---

## Step 3 — Build the event filter

This is the piece that knows whether **you** clicked something. Qat cannot tell
your click from `button->click()` in your own code; `QEvent::spontaneous()` can,
and it is only readable from inside the process.

```bash
~/qatrec/bin/python -m qat_recorder build-filter
```

The C++ source ships inside the wheel, so there is nothing to transfer. If cmake
or the Qt headers are missing it prints the exact install line.

For an artifact that runs on *other* machines, build it against the oldest target
you support:

```bash
docker build -f docker/Dockerfile.build --build-arg BASE=almalinux:8 \
             --build-arg QT_PKG=qt5-qtbase-devel -t qatrec-build .
docker run --rm -v "$PWD:/work" qatrec-build bash native/tests/build_portable.sh
bash native/tests/verify_distros.sh      # does it load on your targets, really?
```

Do not compare glibc version numbers to decide compatibility — they lie in both
directions. See [FINDINGS.md §3](FINDINGS.md).

---

## Step 4 — Record

```bash
~/qatrec/bin/python -m qat_recorder record \
    --lib ~/qatrec-filter/libqatrec.5.15.so \
    --app /opt/acme/bin/hmi \
    --seconds 60 --out ./recorded
```

Interact with the application normally for a minute. You get `recording.json`
(the source of truth), a pytest module, a Gherkin feature and step definitions.

**Read `test_recorded.py` before trusting it.** Fragile steps are flagged inline,
on the line that will break:

```python
    # fragile: positional: breaks if siblings are added, removed or reordered
    qat.mouse_click(qat.find_all_objects(APPLY)[1])
```

Fix those in your source with an `objectName` and re-record, or accept them
knowingly.

**Passwords never reach disk.** A field with a non-normal `echoMode` becomes
`secret('passwordField')`, so the file is safe to commit.

Prefer a GUI? `python -m qat_recorder panel --lib … --app …` gives Record /
Pause / Stop, a live step list colour-coded by durability, and checkpoints.

---

## Step 5 — Replay

Replay needs **no filter** — it is pure Qat.

```bash
~/qatrec/bin/python -c "import qat; qat.register_application('hmi','/opt/acme/bin/hmi')"
QATREC_SECRET_passwordField='the-real-password' \
    ~/qatrec/bin/pytest recorded/test_recorded.py -v
```

Headless, for CI:

```bash
QT_QPA_PLATFORM=offscreen QATREC_SECRET_passwordField=… pytest recorded/ -v
```

---

## Step 6 — Recording from a tester's desk (optional)

Only worth doing once 0–5 work. `bash install.sh vm --agent` sets up the VM side
and prints the exact `hosts add` line for the tester's machine. Full detail in
[deploy/README.md](deploy/README.md).

**The display is the part people forget.** Recording is interactive, so the VM's
screen must be visible to the tester over VNC or X forwarding. This does not
affect what gets captured — `spontaneous()` is about the window system, so clicks
over VNC are indistinguishable from local ones.

---

## Order of operations

| Step | Time | Tells you |
|---|---|---|
| 0 | 30 min | whether Qat sees your app at all |
| 1 | 10 min | how much of your UI is durably nameable |
| 2 | 10 min | whether your QML is testable |
| 3 | 1 hour | you have a working filter for your Qt |
| 4 | 30 min | a real recorded session |
| 5 | 30 min | it replays |
| 6 | half a day | testers can work from their desks |

**Stop after step 2 if the answers are bad.** Those three facts decide whether
this is worth pursuing, and all three are cheap to establish.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| "Could not retrieve server port number" | app died on launch — see step 0 |
| Filter captures nothing | `QATREC_PORT` unset, or Qat's UI lock is on |
| Filter captures nothing, only under Qat | Qat overwrites `LD_PRELOAD`; use the shipped wrapper |
| Shortcuts missing from recordings | old build — bound shortcuts need the `QEvent::Shortcut` path |
| Replay fails on `type_in` | the secret is not in the environment (`QATREC_SECRET_*`) |
| Replay fails "UI has changed since recording" | a positional target moved — re-record or add an `objectName` |
| `cannot find -lstdc++` when building | `dnf install libstdc++-static`, or use a newer build which falls back |
| Agent refuses to start | by design: it needs a token, and TLS unless on loopback |
| Zero QML objects | `QQuickWidget` — see step 2 |
