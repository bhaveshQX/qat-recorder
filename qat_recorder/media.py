# -*- coding: utf-8 -*-
"""
What the screen looked like: stills at the gaps, and a video of the session.

Both live on the machine the application runs on, which is the only machine that
can see it. The tester is somewhere else, watching through VNC, and the panel
fetches these back over the same connection everything else uses.

Two different jobs:

**A still at every gap.** When an event cannot be turned into a step, the one
thing nobody can reconstruct afterwards is what was on screen at that moment --
which dialog was open, which tab, what the thing they clicked looked like. The
reason string says why the recorder failed; the picture says what the operator
was doing.

Of the *screen*, not of the application. The things that go missing at a gap are
overwhelmingly pop-ups -- a combo box's drop-down, a context menu, a modal
dialog, a file picker that is not a Qt widget at all -- and none of those are in
a photograph of the application's own window. Qat can only give the second kind,
so it is the fallback where there is no ffmpeg.

**A video of the whole session.** For the failures that are about *sequence*: a
dialog that appeared and closed again, a step that went to the wrong window, a
recording that diverges from the session ten steps before anyone noticed.
Grabbed with ffmpeg from the X display, because Qat can photograph the
application and cannot film the desktop.

Neither is allowed to break a recording. A missing ffmpeg, a headless VM, a
read-only directory: all of them mean no media and a note saying so, never a
lost session. Someone spent twenty minutes clicking; losing that because a video
encoder is not installed would be indefensible.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

#: Ten frames a second. Enough to see a dialog open and close, small enough that
#: an hour of recording is not a gigabyte, and cheap enough that the encoder
#: does not compete with the application for the CPU it is being tested on.
FRAMERATE = 10

#: ffmpeg gets this long to finish writing the file after being asked to stop.
#: It has to flush and close the container; killed halfway, the mp4 is unplayable.
CLOSE_TIMEOUT_S = 10.0

#: A single frame should be instant. If the display is wedged, a gap without a
#: picture is far better than a recording that stalls waiting for one.
GRAB_TIMEOUT_S = 5.0

#: Gaps arrive in bursts -- six unnamed check boxes in one dialog are six gaps a
#: second apart, all looking at the same screen. Six identical photographs of it
#: is disk, bandwidth and noise for no information, so within this window the
#: previous one is reused.
SAME_SCREEN_S = 1.5

#: A hard ceiling, because a session is minutes long and nobody is watching the
#: disk. Past it, the gaps still get recorded; they just stop being photographed
#: and the listing says so.
MAX_STILLS = 400

#: How far the camera may fall behind the operator before it starts
#: skipping. A step with no picture is a small loss; a recorder that
#: stutters while somebody is working is not.
QUEUE_DEPTH = 8


def _display() -> str:
    return os.environ.get("DISPLAY") or ":0"


def _thumb_path(path):
    """Where the small copy of a still lives, beside the full one."""
    return path.with_suffix(".thumb.png")


def _write_thumbnail(tools, frame, path, across: int = 320) -> None:
    """A small copy, by taking every Nth pixel.

    The actions table shows one of these per step at seventy pixels wide. Sending
    the full screen for that is what made the panel crawl: a 1920x1200 still is
    around 150 kB, so a fifty-step session was seven megabytes of thumbnails --
    fetched over a tunnel, and decoded at full size to be drawn tiny.

    Nearest-neighbour, in Python, on the raw frame that has just been grabbed:
    no decode, no image library, no new dependency. It is not a good downscale
    and it does not need to be -- it needs to be recognisable at seventy pixels,
    and to be about one per cent of the bytes.
    """
    width, height = frame.size
    if width <= across:
        return
    step = max(1, width // across)
    small_w, small_h = width // step, height // step
    if small_w < 2 or small_h < 2:
        return

    source = frame.rgb
    out = bytearray(small_w * small_h * 3)
    at = 0
    for row in range(small_h):
        base = (row * step) * width * 3
        for column in range(small_w):
            pixel = base + (column * step) * 3
            out[at:at + 3] = source[pixel:pixel + 3]
            at += 3
    try:
        tools.to_png(bytes(out), (small_w, small_h), output=str(path))
    except Exception:                                        # noqa: BLE001
        pass


def _grabbers(display: str, path) -> list:
    """Ways to photograph the whole screen, best first."""
    return [
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "x11grab",
         "-i", display, "-frames:v", "1", str(path)],
        # ImageMagick. `-window root` is the whole screen, everything on it.
        ["import", "-display", display, "-window", "root", str(path)],
        ["scrot", "--overwrite", str(path)],
        ["gnome-screenshot", "-f", str(path)],
        ["spectacle", "-b", "-n", "-f", "-o", str(path)],
    ]


#: What is worth telling the operator about when only Qat is left.
SCREEN_GRABBERS = ("mss (in the wheel)", "ffmpeg", "import", "scrot",
                   "gnome-screenshot", "spectacle")


class SessionMedia:
    """Stills and video for one recording, in one directory."""

    def __init__(self, directory, qat_module=None, record_video: bool = True,
                 capture_steps: bool = True):
        self.directory = Path(directory)
        self.qat = qat_module
        self.record_video = record_video
        #: A picture for every step, not only for every gap. Worth roughly a
        #: tenth of a second and a couple of hundred kilobytes each, so it is
        #: a switch rather than a certainty.
        self.capture_steps = capture_steps
        #: Why there is no video, when there is none. Shown in the panel rather
        #: than swallowed, because "no video" and "ffmpeg is not installed on
        #: this VM" are different problems with different fixes.
        self.video_note = ""
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._shots = 0
        self._last_shot = ""
        self._last_shot_at = 0.0
        self._pending = deque()
        self._queued = 0
        self._worker = None
        #: Said once, not once per attempt.
        self.still_note = ""

    # -- stills ------------------------------------------------------------

    @property
    def shots_dir(self) -> Path:
        return self.directory / "shots"

    def take(self, name: str) -> str:
        """Photograph the screen. Returns the file name, or "" if it failed.

        The screen, not the application. Qat can photograph the widget tree it
        is attached to, and that is exactly the wrong picture for this: what
        goes missing at a gap is a *popup* -- a combo box's drop-down, a context
        menu, a modal dialog, a GTK file picker that is not a Qt widget at all.
        Those are separate top-level windows or override-redirect surfaces, and
        a photograph of the application's own window does not contain them. The
        operator ends up looking at the main window and asking why the recorder
        thinks that is what they were doing.

        So the display is grabbed instead, through the same ffmpeg already used
        to film the session. Qat is the fallback for a machine that has no
        ffmpeg: the main window is worth more than nothing.
        """
        shot = f"{name}.png"
        try:
            self.shots_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return ""
        path = self.shots_dir / shot

        if not (self._grab_with_mss(path)
                or self._grab_screen(path)
                or self._ask_qat(path)):
            return ""
        self._shots += 1
        return shot

    def _grab_with_mss(self, path) -> bool:
        """The whole screen, from Python, with no external programme at all.

        This is the one that has to work, because every other way of
        photographing a screen depends on something a locked-down VM may simply
        not have: ffmpeg lives in a third-party repository on RHEL, ImageMagick
        and scrot are packages somebody has to have installed, and Qat can only
        photograph the widget tree it is attached to -- which is precisely not
        the pop-up, the other tab or the dialog stacked over it.

        mss calls XGetImage through ctypes. It ships in the wheel, needs no
        compiler and no distribution package, and captures the display exactly
        as it looks: every window, every menu, every native dialog.
        """
        if not (os.environ.get("DISPLAY") or os.name == "nt"):
            return False
        try:
            import mss                                       # noqa: PLC0415
            import mss.tools                                 # noqa: PLC0415

            # mss.MSS on 10+, mss.mss on older releases the VM may have.
            camera_class = getattr(mss, "MSS", None) or mss.mss
            with camera_class() as camera:
                # monitors[0] is the whole virtual screen, not the first of
                # them -- a VM with two heads still gives one picture.
                frame = camera.grab(camera.monitors[0])
                mss.tools.to_png(frame.rgb, frame.size, output=str(path))
                _write_thumbnail(mss.tools, frame, _thumb_path(path))
        except Exception:                                    # noqa: BLE001
            return False
        return path.is_file() and path.stat().st_size > 0

    def _grab_screen(self, path) -> bool:
        """One frame of the whole display: popups, dialogs and all.

        Several ways, because no single one is reliably present. ffmpeg is the
        best answer and is also what films the session, but on RHEL and its
        derivatives it lives in a third-party repository and is routinely
        absent; ImageMagick and scrot are in the base repositories nearly
        everywhere. Any of them gives the whole screen, which is the point.
        """
        if not os.environ.get("DISPLAY"):
            return False
        for command in _grabbers(_display(), path):
            if shutil.which(command[0]) is None:
                continue
            try:
                subprocess.run(command, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               timeout=GRAB_TIMEOUT_S, check=False)
            except Exception:                                # noqa: BLE001
                continue
            if path.is_file() and path.stat().st_size > 0:
                return True
        return False

    def _ask_qat(self, path) -> bool:
        """The application's own window. Better than nothing, worse than the screen."""
        if self.qat is None:
            return False
        try:
            self.qat.take_screenshot(str(path))
        except Exception:                                    # noqa: BLE001
            return False
        if not (path.is_file() and path.stat().st_size > 0):
            return False
        if not self.still_note:
            self.still_note = (
                "stills are of the application's own window only, because none "
                "of " + ", ".join(SCREEN_GRABBERS) + " is installed here. "
                "Pop-ups, menus, other tabs and native dialogs stacked over it "
                "are not in them. Install one -- ImageMagick is in the base "
                "repository of every distribution this runs on -- and stills "
                "become the whole screen.")
        return True

    def take_numbered(self, prefix: str) -> str:
        """A still named so the order it was taken in survives a directory listing."""
        return self.take(f"{prefix}-{self._shots:03d}")

    def take_for_gap(self, prefix: str) -> str:
        """A picture of the screen a gap happened on.

        Queued, not taken here. This runs inside the poll loop, which holds the
        session lock, and grabbing and encoding a 1920x1200 screen takes the
        better part of a second. Every HTTP request waits on that same lock, so
        one gap used to stall the panel's entire connection -- including the
        socket feeding it the steps. That is how a recording could finish before
        the panel had shown any of it.

        Each gap gets its own picture. Sharing one between gaps a second apart
        saved a file and made the mapping a lie: the second showed the screen
        the first happened on, which is the one thing a still exists to settle.
        """
        return self._enqueue(f"{prefix}-{self._shots:03d}")

    # -- taken behind the pump, never in front of it ------------------------

    def capture_async(self, name: str) -> str:
        """Photograph the screen for a step, and return the name now.

        A step is folded inside the poll loop, and that loop is what keeps the
        panel's picture of the session current. Nothing slow may happen in it.
        """
        if not self.capture_steps:
            return ""
        return self._enqueue(name)

    def _enqueue(self, name: str) -> str:
        """Decide the name now; let the worker do the work.

        If the worker is already behind, the shot is skipped rather than queued.
        Falling further behind the operator helps nobody, and a step without a
        picture costs almost nothing.
        """
        if self._shots >= MAX_STILLS:
            self.still_note = (
                f"stopped after {MAX_STILLS} stills; the steps and gaps are all "
                "still recorded, they just have no picture")
            return ""
        shot = f"{name}.png"
        with self._lock:
            if self._queued >= QUEUE_DEPTH:
                return ""
            # Queued *before* the worker is started, and both under the lock.
            # The other order loses shots: the worker wakes, finds the queue
            # empty, exits, and the item appended a moment later is left with
            # nothing running to take it.
            self._pending.append(shot)
            self._queued += 1
            self._shots += 1
            if self._worker is None:
                self._worker = threading.Thread(target=self._run_queue, daemon=True)
                self._worker.start()
        return shot

    def _run_queue(self) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    # Cleared inside the same lock that queues work, so there is
                    # no window where a caller sees a live worker that is on its
                    # way out.
                    self._worker = None
                    return
                shot = self._pending.popleft()
            try:
                self.shots_dir.mkdir(parents=True, exist_ok=True)
                path = self.shots_dir / shot
                if not self._grab_with_mss(path):
                    if not self._grab_screen(path):
                        self._ask_qat(path)
            except Exception:                                # noqa: BLE001
                pass
            finally:
                with self._lock:
                    self._queued -= 1

    def settle(self, timeout: float = 5.0) -> None:
        """Wait for the queued shots, so a saved session is not missing them."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._queued <= 0:
                    return
            time.sleep(0.05)

    def stills(self) -> list:
        if not self.shots_dir.is_dir():
            return []
        return sorted(path.name for path in self.shots_dir.glob("*.png")
                      if not path.name.endswith(".thumb.png"))

    # -- video -------------------------------------------------------------

    @property
    def video(self) -> Path:
        return self.directory / "session.mp4"

    def start_video(self) -> None:
        """Begin filming the display. Never raises."""
        if not self.record_video:
            self.video_note = "video was turned off for this session"
            return
        with self._lock:
            if self._process is not None:
                return
            if shutil.which("ffmpeg") is None:
                self.video_note = (
                    "ffmpeg is not installed on this machine, so the session "
                    "was not filmed. Stills at the gaps are unaffected.")
                return
            if not os.environ.get("DISPLAY"):
                self.video_note = (
                    "no DISPLAY, so there is no screen to film. Recording is "
                    "interactive, so this usually means the agent was started "
                    "from a session that cannot see the desktop.")
                return
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._process = subprocess.Popen(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-f", "x11grab", "-framerate", str(FRAMERATE),
                     "-i", _display(),
                     "-codec:v", "libx264", "-preset", "ultrafast",
                     # Odd dimensions are common on a VM and libx264 refuses
                     # them with yuv420p; round both down to even.
                     "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                     "-pix_fmt", "yuv420p",
                     str(self.video)],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE)
            except Exception as error:                       # noqa: BLE001
                self._process = None
                self.video_note = f"could not start ffmpeg: {error}"

    def stop_video(self) -> None:
        """Ask ffmpeg to finish, and give it time to close the file."""
        with self._lock:
            process, self._process = self._process, None
        if process is None:
            return
        try:
            # `q` on stdin is ffmpeg's own "stop cleanly". A signal would leave
            # the mp4 without its index, which no browser will play.
            if process.stdin:
                process.stdin.write(b"q")
                process.stdin.flush()
                process.stdin.close()
        except Exception:                                    # noqa: BLE001
            pass
        try:
            process.wait(timeout=CLOSE_TIMEOUT_S)
        except Exception:                                    # noqa: BLE001
            process.kill()
            self.video_note = ("ffmpeg did not stop cleanly; the video may be "
                               "truncated")

    @property
    def filming(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def describe(self) -> dict:
        """What the panel needs to show and fetch this."""
        video = self.video if self.video.exists() else None
        return {
            "stills": self.stills(),
            "video": video.name if video else "",
            "video_bytes": video.stat().st_size if video else 0,
            "video_note": self.video_note,
            "still_note": self.still_note,
            "filming": self.filming,
        }

    def path_of(self, name: str) -> Optional[Path]:
        """Resolve a media file name to a path inside this session, or None.

        The name arrives over HTTP, so it is checked rather than trusted: only
        files that really are inside this session's directory are served.
        """
        candidates = [self.shots_dir / name, self.directory / name]
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                resolved.relative_to(self.directory.resolve())
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                return resolved
        return None
