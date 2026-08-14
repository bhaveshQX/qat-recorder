# Findings

Everything established by measurement rather than assumption, with how it was
verified. This is the document to read before making a decision about the
project; the phase summaries explain how the code got built, this explains what
is actually true.

Labels: **[source]** read from the installed `qat` package · **[binary]**
measured from ELF symbol tables · **[live]** observed against a running
application · **[container]** reproduced in Docker.

---

## 1. Qat's capabilities

**`QtObject` exposes tree traversal — YES** [source] [live]
`__getattr__` materialises a `children` list into `QtObject` instances, and
`.parent` works. Container/parent chains in generated names are therefore
possible. Confirmed live: 400+ objects reachable, depth 12.

**Callbacks carry signal arguments — YES** [source] [live]
`qat.connect(object_def, "pinged(int)", cb)` delivers the signal's arguments;
Qat validates callback arity against the signature and raises `TypeError` if the
callback wants more. Property connections deliver the new value.

**Callback delivery is lossless and ordered** [live]
500 signals emitted, 500 received, in order, at ~332/s. The callback channel is
sound transport.

**`LD_PRELOAD` launch-only workflow works** [live]
End to end on Linux: Qt version auto-detected, `libQatServer.6.11.so` loaded,
both plugins loaded, application ready for testing.

**Replay works headless** [live]
Injection, object lookup, `mouse_click` and callbacks all function under
`QT_QPA_PLATFORM=offscreen`. Recording does not — it needs a human.

---

## 2. Qat's limits

### QML is only visible in a top-level QQuickView [live]

Same application, same Qat, same Qt:

| QML embedding | Qat visibility |
|---|---|
| `QQuickWidget` (QML inside a widget) | **nothing** — 0 `QQuickItem`, 0 `MouseArea`, 0 `Rectangle` |
| Top-level `QQuickView` | **everything** — 8 `QQuickItem`, both `MouseArea`s, property reads work (`qmlNamedTile.width = 140`), `mouse_click` on a QML `MouseArea` succeeds |

The scene is not failing to load — `qmlErrorLabel` is absent and the
`QQuickWidget` itself is visible. Qat's QML plugin does not traverse into it,
because a `QQuickWidget` renders its scene into an internal, non-top-level
window.

**This is an architectural constraint on the applications under test.**
`QQuickWidget` is the usual way to mix QML into an existing widget application,
and QML embedded that way is unrecordable, unnameable and unreplayable.

Options, cheapest first: use a top-level `QQuickView`/`QQuickWindow` (verified
working); try `QWidget::createWindowContainer(QQuickView)`, which keeps a real
`QQuickWindow` (`spike/qml_container_app.py` is written for this and **remains
unverified** — the single most valuable outstanding experiment); or extend Qat's
QML plugin, which is real work but upstreamable.

### No Linux native plugin ships [binary]

The wheel contains `WindowsNativePlugin.*.dll` and `libCocoaNativePlugin.*.dylib`
but no Linux counterpart. Linux gets the QWidget and QML plugins only, so the
entire native/OS-level layer — not merely `native_pinch` — is absent.

### Attaching to a running process is impossible on Linux [source]

Injection uses `LD_PRELOAD`, which only applies at `exec()`.
[GitLab issue #24](https://gitlab.com/testing-tool/qat/-/issues/24) confirms
`attach_to_application` works on Windows but not Linux. The recorder must own the
process lifecycle.

### Qat locks the application's UI during a session [live]

`Settings.lock_ui` defaults to `auto`, which blocks input to the application —
correct while replaying, exactly backwards while recording or auditing, where it
makes the application look frozen. The tooling sets `lock_ui = "never"`; anything
hand-rolled must too.

### Qat overwrites `LD_PRELOAD` when launching [source] [live]

It sets the variable to its own injector, discarding whatever was there. The
event filter therefore cannot simply be exported beforehand;
`qat_recorder/resources/wrapper.sh` appends it from inside the launched process
and `exec`s the real binary, keeping the same PID so Qat's tracking stays valid.

### Qat resolves the application path through symlinks [source] [live]

`app_launcher.py:465` calls `Path(app_path).resolve()`, then launches the
resolved target. A virtualenv's `bin/python` is a symlink to the system
interpreter, so a Python application under test is launched by a Python that
cannot import the venv's packages — surfacing as
`ProcessLookupError: Could not retrieve server port number`, which looks like an
injection failure and is not.

In production the same applies to applications installed as
`/usr/local/bin/app -> /opt/app-1.2/bin/app`, or started through a wrapper that
sets `LD_LIBRARY_PATH`. `qat_recorder/launcher.py` documents this and provides
`prepare_python_aut_env()`.

### Qat stores configuration in the working directory [live]

`qat.get_config_file()` returns `applications.json` in the **current working
directory**, not a config directory. This is why "Invalid configuration file.
Content will be overwritten." appears when running from a directory that has a
stale one — and why nothing may safely derive a *directory* to delete from that
path. (An earlier `install.sh uninstall` did exactly that and destroyed a
repository.)

### `echoMode` is reported as a string, not a number [live]

`'Password'`, `'Normal'` — not `2`, `0`. Assuming an integer made
`is_secret_field()` return `False` for every real password field, so credentials
would have been written to disk in clear text. Caught on the conformance
suite's first run against a live application, while all 33 offline tests passed.

### Qat represents "no parent" as a null QtObject [live]

Not `None`. Its definition is `None` and every attribute access on it fails.
`QatBackend.parent()` normalises it, because treating it as a real object breaks
identity comparison and produces nonsense container anchors.

---

## 3. The distribution matrix

### Upstream's prebuilt libraries are not built against a common floor [binary]

| Qt version | needs glibc | needs GLIBCXX | implies gcc |
|---|---|---|---|
| 5.15, 6.2 | 2.14 | 3.4.26 | 9 |
| 6.3 – 6.7 | **2.34** | 3.4.29 | 11 |
| 6.8 – 6.11 | **2.38** | 3.4.32 | 13 |

Consequently **no prebuilt Qat server loads on RHEL/Rocky/CentOS 8** — it misses
by one libstdc++ symbol version, since stock EL8 has `GLIBCXX_3.4.25` and the
oldest server library wants `3.4.26`. And **Qt 6.8+ loads only on Ubuntu 24.04
and newer** — not RHEL 9, not Ubuntu 22.04.

For a fleet spanning Ubuntu, RHEL, CentOS and Rocky, a source build of the Qat
server is required from day one.

### Our own artifacts [binary] [container]

Verified by loading each artifact's requirements against six real distribution
images, not by comparing version numbers:

| Artifact | Floor | EL8 | EL9 / Rocky 9 | Ubuntu 20.04 | 22.04 | 24.04 |
|---|---|---|---|---|---|---|
| `libqatrec.5.15.so` | `GLIBC_2.17` | **yes** | yes | yes | yes | yes |
| `libqatrec.6.6.so` | `GLIBC_2.34` | no | yes | no | yes | yes |

`GLIBC_2.17` is older than CentOS 7. One Qt 5 artifact covers the whole range —
including EL8, where no upstream Qat server library loads at all. The Qt 6
artifact's misses are on distributions that do not ship Qt 6 anyway.

### `-static-libgcc` cost portability instead of buying it [binary]

The first Qt 6 build required `GLIBC_2.35` from a host whose nominal glibc is
2.34 — which looked impossible and was not. A *single* symbol,
`_dl_find_object`, pulled in by the static libgcc unwinder on gcc 11+. Dropping
`-static-libgcc` (keeping `-static-libstdc++`) lowered the floor to 2.34.
`libgcc_s.so.1` is present on every Linux system, so linking it statically was
never worth anything.

That mattered more than it looks: **AlmaLinux 9.8 defines up to `GLIBC_2.35`
while Rocky 9.3 stops at `2.34`.** The 2.35 artifact would have loaded on
AlmaLinux and failed on Rocky — same nominal distribution version, different
backports.

### Nominal glibc versions are not a compatibility test [binary]

RHEL-family distributions backport newer symbol versions while keeping the base
number, and they do not all backport the same set. Use
`native/tests/check_symbol_floor.sh` (readelf, authoritative) and
`native/tests/verify_distros.sh` (runs on the target), not arithmetic on version
numbers.

### Qt 6 is not in RHEL/Rocky 9 AppStream [container]

Only `qt5-qtbase-devel 5.15.9`. EPEL supplies Qt 6.6, which is what the Qt 6
artifact is built against — covering 6.6 through 6.11 by Qt's binary
compatibility guarantee, but **not 6.2 to 6.5**. Building against 6.2 LTS would
cover the whole series and needs a vendored Qt.

---

## 4. The event filter

**`QEvent::spontaneous()` is the only reliable discriminator** [live] [container]

Proven both ways. Against a live application, calling
`toggle_remember_programmatically()` — pure code, no user input — fired exactly
the same callback a real click produces, so client-side signal hooking cannot
separate them. In the container, `xdotool` driving a real X server produced 33
captured records while `button->click()` and `setChecked(true)` produced **zero**.

**Shortcuts bound to a QAction never arrive as key presses** [container]

Qt consumes them in its shortcut machinery and sends `QEvent::Shortcut` instead
— which is synthesised, so `spontaneous()` is false for it. Without special
handling, every application shortcut is silently missing from recordings. The
filter tracks when real window-system input last arrived (including
`ShortcutOverride`, which *is* spontaneous and precedes the shortcut machinery)
and accepts a `QEvent::Shortcut` within 250 ms of it.

**Qt delivers `QEvent::Shortcut` to the QAction, which is not a widget**
[container]

Replaying against it fails with "Associated widget was not found". Retargeting to
the *outermost* ancestor does not help either — above the window sits a
`QWidgetWindow`, a `QWindow` rather than a `QWidget`, which fails identically.
The recorder retargets to the nearest named ancestor from the path the filter
reports.

**Not every widget with a `text` property is a text field** [container]

Tab moved focus onto a checkbox mid-typing and the recorder emitted
`type_in(rememberBox, "Remember me")` — the widget's own caption, replayed as if
the user had typed it. Characters landing on anything that is not a text field
are recorded as individual key presses instead.

**A menu item is not an object that can receive a click** [live] [source]

In a widgets application a menu item is a `QAction`: no geometry, no events. The
menu paints it. So the filter sees a click on `QMenuBar` or on the popup `QMenu`,
and neither is clickable in a useful way — Qat aims at the centre of a widget,
and the centre of a menu bar as wide as the window is empty space. Recorded
naively, the menu never opens on replay and the following step fails with
`Unable to find object: {"objectName":"menuOptions"}`.

Qat solves this itself: it wraps each item in a virtual widget carrying the
item's geometry, addressed by the containing menu plus the item's label —
`{"container": {"type": "QMenuBar"}, "text": "File"}`. The filter therefore asks
the menu which item is under the pointer (`QMenu::actionAt`, reached through
`dlsym` so QtWidgets is never linked) and the recorder emits Qat's own form. The
click position is used to identify the item and is then discarded.

**Qt redirects the release of the click that opened a menu** [live]

The popup grabs the mouse as soon as it appears, so the press lands on the
`QMenuBar` and the release on the `QMenu` — at a position outside it. Taken at
face value that is a second click at, in one measured case, (6, −11), which Qat
rejects: `Cannot execute mouse operation: Given coordinates are outside widget's
boundaries`. Opening a menu is one click however many events Qt sends, so the
press names the item and its release is dropped — unless the release names a
*different* item, which is the press-drag-release way of using a menu.

---

## 5. What remains unverified

- **`createWindowContainer` for QML.** The highest-value outstanding experiment
  if applications embed QML via `QQuickWidget`.
- **EL8 on real hardware.** The prediction in §3 is from symbol tables; no EL8
  machine has run it.
- **The control panel against a real application.** Its controller is tested
  against fakes and the panel offscreen; the pipeline beneath it is proven
  end to end, but nobody has clicked Record on a real binary.
- **The agent against a real VM.** Tested over real sockets with real TLS, but
  always against the synthetic object tree.
- **Bundled-Qt applications.** The filter links Qt, so an application shipping
  its own Qt could load a second copy. Verified working with system Qt; the fix
  is Qat's own approach — no Qt dependency, detect the version, `dlopen` a
  matched build.
