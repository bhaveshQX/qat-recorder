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
MAX_STILLS = 200


def _display() -> str:
    return os.environ.get("DISPLAY") or ":0"


class SessionMedia:
    """Stills and video for one recording, in one directory."""

    def __init__(self, directory, qat_module=None, record_video: bool = True):
        self.directory = Path(directory)
        self.qat = qat_module
        self.record_video = record_video
        #: Why there is no video, when there is none. Shown in the panel rather
        #: than swallowed, because "no video" and "ffmpeg is not installed on
        #: this VM" are different problems with different fixes.
        self.video_note = ""
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._shots = 0
        self._last_shot = ""
        self._last_shot_at = 0.0
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

        if not (self._grab_screen(path) or self._ask_qat(path)):
            return ""
        self._shots += 1
        return shot

    def _grab_screen(self, path) -> bool:
        """One frame of the whole display: popups, dialogs and all."""
        if not os.environ.get("DISPLAY") or shutil.which("ffmpeg") is None:
            return False
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-f", "x11grab", "-i", _display(), "-frames:v", "1",
                 str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=GRAB_TIMEOUT_S, check=False)
        except Exception:                                    # noqa: BLE001
            return False
        return path.is_file() and path.stat().st_size > 0

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
                "stills are of the application's own window: ffmpeg is not "
                "installed here, so pop-ups, menus and native dialogs stacked "
                "over it are not in them")
        return True

    def take_numbered(self, prefix: str) -> str:
        """A still named so the order it was taken in survives a directory listing."""
        return self.take(f"{prefix}-{self._shots:03d}")

    def take_for_gap(self, prefix: str) -> str:
        """A picture of the screen a gap happened on -- or the last one.

        Two gaps a few hundred milliseconds apart are looking at the same
        screen. Photographing it twice costs a file and tells nobody anything,
        so within SAME_SCREEN_S the previous still is what the second gap gets.
        """
        now = time.monotonic()
        if self._last_shot and now - self._last_shot_at <= SAME_SCREEN_S:
            return self._last_shot
        if self._shots >= MAX_STILLS:
            self.still_note = (
                f"stopped after {MAX_STILLS} stills; the gaps are still "
                "recorded, they just have no picture")
            return ""
        name = self.take_numbered(prefix)
        if name:
            self._last_shot, self._last_shot_at = name, now
        return name

    def stills(self) -> list:
        if not self.shots_dir.is_dir():
            return []
        return sorted(path.name for path in self.shots_dir.glob("*.png"))

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
