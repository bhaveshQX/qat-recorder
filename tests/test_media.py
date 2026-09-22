# -*- coding: utf-8 -*-
"""
Stills and video: kept on the machine the application runs on, fetched by the
panel from somewhere else.

Neither is allowed to cost a recording. Someone spent twenty minutes clicking;
losing that because ffmpeg is not installed, or because a screenshot failed,
would be indefensible -- so most of what these tests pin is what happens when
the media does *not* work.
"""

import pytest

from qat_recorder.media import SessionMedia
from qat_recorder.ui.controller import ControllerError, State
from tests.test_capture import click_pair
from tests.test_drops import NOWHERE, _record_a_gap          # noqa: F401
from tests.test_ui_controller import controller              # noqa: F401  (fixture)


@pytest.fixture()
def no_screen(monkeypatch):
    """A machine that cannot photograph its screen at all.

    mss ships in the wheel and works wherever there is a display, so the
    fallbacks now have to be asked for explicitly to be tested. That is the
    point of it: the paths below are what happens on a headless VM, not what
    happens normally.
    """
    monkeypatch.setattr(SessionMedia, "_grab_with_mss", lambda self, path: False)
    monkeypatch.setattr(SessionMedia, "_grab_screen", lambda self, path: False)


class FakeQat:
    """Writes a file where Qat would put a screenshot."""

    def __init__(self, works=True):
        self.works = works
        self.asked = []

    def take_screenshot(self, path):
        self.asked.append(path)
        if not self.works:
            raise RuntimeError("no display")
        with open(path, "wb") as file:
            file.write(b"\x89PNG\r\n\x1a\n")


# --- stills ----------------------------------------------------------------

def test_a_still_is_written_and_named(tmp_path):
    media = SessionMedia(tmp_path, qat_module=FakeQat())
    assert media.take("gap-0") == "gap-0.png"
    assert (tmp_path / "shots" / "gap-0.png").exists()
    assert media.stills() == ["gap-0.png"]


def test_numbering_keeps_the_order_they_were_taken_in(tmp_path):
    media = SessionMedia(tmp_path, qat_module=FakeQat())
    names = [media.take_numbered("shot") for _ in range(3)]
    assert names == ["shot-000.png", "shot-001.png", "shot-002.png"]
    assert media.stills() == sorted(names), "sorted by name is sorted by time"


def test_a_screenshot_that_fails_is_not_an_exception(tmp_path, no_screen):
    """A recording continues whatever the screen is doing."""
    media = SessionMedia(tmp_path, qat_module=FakeQat(works=False))
    assert media.take("gap-0") == ""
    assert media.stills() == []


def test_no_qat_means_no_still_and_no_crash(tmp_path, no_screen):
    assert SessionMedia(tmp_path).take("gap-0") == ""


# --- video -----------------------------------------------------------------

def test_video_says_why_when_it_cannot_film(tmp_path, monkeypatch):
    """"No video" and "ffmpeg is not installed here" are different problems."""
    monkeypatch.setattr("shutil.which", lambda _: None)
    media = SessionMedia(tmp_path)
    media.start_video()
    assert not media.filming
    assert "ffmpeg is not installed" in media.video_note


def test_video_says_why_when_there_is_no_display(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.delenv("DISPLAY", raising=False)
    media = SessionMedia(tmp_path)
    media.start_video()
    assert not media.filming
    assert "no DISPLAY" in media.video_note


def test_video_can_be_turned_off(tmp_path):
    media = SessionMedia(tmp_path, record_video=False)
    media.start_video()
    assert not media.filming
    assert "turned off" in media.video_note


def test_stopping_a_video_that_never_started_is_harmless(tmp_path):
    SessionMedia(tmp_path).stop_video()


# --- what may be fetched ---------------------------------------------------

def test_only_files_inside_the_session_are_served(tmp_path):
    """The name arrives over HTTP, so it is checked rather than trusted."""
    media = SessionMedia(tmp_path, qat_module=FakeQat())
    media.take("gap-0")
    (tmp_path.parent / "secrets.txt").write_text("not yours")

    assert media.path_of("gap-0.png") is not None
    assert media.path_of("../secrets.txt") is None
    assert media.path_of("../../etc/passwd") is None
    assert media.path_of("nothing.png") is None


def test_the_video_is_served_from_the_session_root(tmp_path):
    media = SessionMedia(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    media.video.write_bytes(b"mp4")
    assert media.path_of("session.mp4") == media.video.resolve()
    assert media.describe()["video"] == "session.mp4"


# --- through the controller ------------------------------------------------

def test_every_gap_is_photographed(controller, tmp_path):     # noqa: F811
    """The reason says why the recorder failed. The picture says what the
    operator was looking at, which is the part nobody can reconstruct."""
    controller.media = SessionMedia(tmp_path, qat_module=FakeQat(),
                                    record_video=False)
    _record_a_gap(controller)
    controller.media.settle()

    drop, = controller.recording.drops
    assert drop.shot, "the gap has no picture of the screen"
    assert (tmp_path / "shots" / drop.shot).exists()


def test_a_gap_is_photographed_once(controller, tmp_path):    # noqa: F811
    controller.media = SessionMedia(tmp_path, qat_module=FakeQat(),
                                    record_video=False, capture_steps=False)
    _record_a_gap(controller)
    first = controller.recording.drops[0].shot
    controller.poll()
    controller.poll()
    controller.media.settle()
    assert controller.recording.drops[0].shot == first
    assert len(controller.media.stills()) == 1


def test_a_failed_screenshot_does_not_stop_the_recording(controller, tmp_path, no_screen):  # noqa: F811
    controller.media = SessionMedia(tmp_path, qat_module=FakeQat(works=False),
                                    record_video=False)
    _record_a_gap(controller)
    assert controller.state is State.RECORDING
    assert len(controller.recording.actions) > 1, "the session kept recording"

    # The name was handed out before the camera was asked -- that is what keeps
    # the recorder from waiting on it. Stopping settles the queue and forgets
    # the names that never became files, rather than leaving a broken picture
    # in the panel and a lie in recording.json.
    controller.stop()
    assert controller.recording.drops[0].shot == ""


def test_the_operator_can_ask_for_a_still(controller, tmp_path):  # noqa: F811
    controller.media = SessionMedia(tmp_path, qat_module=FakeQat(),
                                    record_video=False)
    controller.start()
    assert controller.take_screenshot()["shot"].endswith(".png")


def test_asking_for_a_still_after_the_application_closed_is_refused(controller, tmp_path):  # noqa: F811
    controller.media = SessionMedia(tmp_path, qat_module=FakeQat(),
                                    record_video=False)
    controller.start()
    controller.stop()
    with pytest.raises(ControllerError):
        controller.take_screenshot()


def test_a_session_keeping_no_media_still_records(controller):  # noqa: F811
    """The CLI path: no agent, no session directory, no media."""
    assert controller.media is None
    _record_a_gap(controller)
    assert controller.recording.drops[0].shot == ""


# --- not once a second, forever --------------------------------------------

def test_a_gap_that_cannot_be_photographed_is_not_retried(controller, tmp_path, no_screen):  # noqa: F811
    """The log spam.

    Retrying anything without a picture on every tick of the pump meant a
    failing screenshot was attempted several times a second for the length of
    the session, logging each time.
    """
    camera = FakeQat(works=False)
    controller.media = SessionMedia(tmp_path, qat_module=camera,
                                    record_video=False, capture_steps=False)
    _record_a_gap(controller)
    controller.media.settle()
    attempts = len(camera.asked)

    for _ in range(20):
        controller.poll()
    controller.media.settle()

    assert len(camera.asked) == attempts == 1, (
        "the camera was asked again for a gap it had already failed on")


def test_every_gap_gets_its_own_picture(tmp_path):
    """No sharing, whatever the interval. A gap's still is the record of a
    screen nobody can get back to, and lending it to the next gap makes both
    of them wrong."""
    media = SessionMedia(tmp_path, qat_module=FakeQat(), record_video=False)
    first = media.take_for_gap("gap-0")
    second = media.take_for_gap("gap-1")
    media.settle()

    assert first and second and first != second
    assert len(media.stills()) == 2


def test_the_camera_stops_at_the_ceiling(tmp_path, monkeypatch):
    """A session is minutes long and nobody is watching the disk."""
    monkeypatch.setattr("qat_recorder.media.MAX_STILLS", 3)
    media = SessionMedia(tmp_path, qat_module=FakeQat(), record_video=False)
    clock = [1000.0]
    monkeypatch.setattr("qat_recorder.media.time.monotonic", lambda: clock[0])

    taken = []
    for index in range(8):
        clock[0] += 60.0
        taken.append(media.take_for_gap(f"gap-{index}"))

    assert len([name for name in taken if name]) == 3
    assert "stopped after 3 stills" in media.describe()["still_note"]
    assert "still recorded" in media.describe()["still_note"]


def test_scrolling_is_never_photographed(tmp_path):
    """Because scrolling is not a gap, nothing asks for a picture of it.

    Built with a tree that actually has a list to scroll -- otherwise the wheel
    lands on nothing, which is a real gap and rightly does get photographed.
    """
    from qat_recorder.ui.controller import RecorderController
    from tests.test_capture import event
    from tests.test_no_geometry import sliders
    from tests.test_ui_controller import FakeQat as ControllerQat, FakeReceiver

    backend, _ = sliders()
    receiver = FakeReceiver()
    from tests.fixtures import real_paths
    app, lib = real_paths(tmp_path)
    controller = RecorderController(
        ControllerQat(), lib_path=lib, app_path=app,
        app_name="sample", backend=backend, receiver=receiver)

    camera = FakeQat()
    controller.media = SessionMedia(tmp_path, qat_module=camera,
                                    record_video=False)
    controller.start()
    receiver.push(*[event("wheel", 1000 + n * 40, "QListWidget", "torrentList",
                          dy=-120) for n in range(25)])
    controller.poll()

    assert camera.asked == [], "scrolling a list photographed the screen"
    assert controller.recording.drops == []


def test_the_screen_is_photographed_without_any_external_programme(tmp_path):
    """The one that has to work on a locked-down VM.

    ffmpeg is in a third-party repository on RHEL, ImageMagick and scrot are
    packages somebody has to install, and Qat can only photograph the widget
    tree it is attached to -- which is exactly not the pop-up over it. mss ships
    in the wheel and calls XGetImage through ctypes, so a still is the whole
    screen wherever there is a display at all.
    """
    import os
    if not (os.environ.get("DISPLAY") or os.name == "nt"):
        pytest.skip("no display to photograph")

    media = SessionMedia(tmp_path, record_video=False)   # no Qat at all
    name = media.take("proof")
    assert name, "the screen could not be photographed without a helper"

    data = (tmp_path / "shots" / name).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(data) > 5000, "a real screen, not a placeholder"
    assert media.still_note == "", "nothing to apologise for when it worked"


# --- a picture for every step ----------------------------------------------

def test_every_step_is_photographed(controller, tmp_path):    # noqa: F811
    """What a step did is half of what a reviewer needs. The other half is what
    was in front of the operator when they did it."""
    controller.media = SessionMedia(tmp_path, qat_module=FakeQat(),
                                    record_video=False)
    controller.start()
    controller._test["receiver"].push(*click_pair(100, "QPushButton", "loginButton"))
    controller.poll()
    controller.media.settle()

    steps = [a for a in controller.recording.actions if a.kind.value != "launch"]
    assert steps and all(step.shot for step in steps)
    assert all((tmp_path / "shots" / step.shot).exists() for step in steps)


def test_the_launch_is_not_photographed(controller, tmp_path):  # noqa: F811
    """There is nothing on screen yet."""
    controller.media = SessionMedia(tmp_path, qat_module=FakeQat(),
                                    record_video=False)
    controller.start()
    controller._test["receiver"].push(*click_pair(100, "QPushButton", "loginButton"))
    controller.poll()
    assert controller.recording.actions[0].kind.value == "launch"
    assert controller.recording.actions[0].shot == ""


def test_the_camera_never_makes_the_pump_wait(tmp_path):
    """A recorder that stutters while somebody is working is worse than one that
    takes no pictures, so the grab happens on a worker and the queue is bounded.
    """
    import time as clock

    media = SessionMedia(tmp_path, qat_module=FakeQat(), record_video=False)
    started = clock.perf_counter()
    names = [media.capture_async(f"step-{n:03d}") for n in range(40)]
    queued_in = clock.perf_counter() - started

    assert queued_in < 0.5, f"queueing 40 stills blocked for {queued_in:.2f}s"
    assert any(names), "nothing was captured at all"
    assert len([n for n in names if n]) < 40, (
        "the queue is unbounded, so a slow camera would drag the recorder down")
    media.settle()


def test_per_step_capture_can_be_turned_off(tmp_path):
    media = SessionMedia(tmp_path, qat_module=FakeQat(), record_video=False,
                         capture_steps=False)
    assert media.capture_async("step-000") == ""
    assert media.stills() == []
    # Gaps are a different question and still get one.
    assert media.take_for_gap("gap-0")


# --- small copies, so the panel is not sent the screen fifty times ---------

def test_a_still_is_written_with_a_small_copy_beside_it(tmp_path):
    """The actions table shows one per step at seventy pixels wide.

    Sending the full screen for that is what made the panel crawl: a fifty-step
    session was seven megabytes of thumbnails, fetched over a tunnel and decoded
    at full size to be drawn tiny.
    """
    import os
    if not (os.environ.get("DISPLAY") or os.name == "nt"):
        pytest.skip("no display to photograph")

    media = SessionMedia(tmp_path, record_video=False)
    name = media.take("one")
    assert name

    full = (tmp_path / "shots" / name)
    thumb = (tmp_path / "shots" / name.replace(".png", ".thumb.png"))
    assert thumb.exists(), "no small copy was written"
    assert thumb.stat().st_size < full.stat().st_size / 5, (
        "the small copy is not appreciably smaller")
    assert thumb.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_listing_does_not_repeat_the_small_copies(tmp_path):
    import os
    if not (os.environ.get("DISPLAY") or os.name == "nt"):
        pytest.skip("no display to photograph")

    media = SessionMedia(tmp_path, record_video=False)
    media.take("one")
    assert media.stills() == ["one.png"]


def test_two_gaps_close_together_get_their_own_pictures(tmp_path):
    """Sharing one made the mapping a lie: the second gap showed the screen the
    first one happened on, which is the thing a still exists to settle."""
    media = SessionMedia(tmp_path, qat_module=FakeQat(), record_video=False)
    first = media.take_for_gap("gap-0")
    second = media.take_for_gap("gap-1")
    assert first and second and first != second


def test_a_gap_still_is_queued_rather_than_taken_in_the_poll_loop(tmp_path):
    """This runs inside the poll loop, which holds the session lock, and every
    HTTP request waits on that lock.

    Taking the picture here stalled the panel's whole connection -- including
    the socket feeding it the steps -- for the better part of a second per gap.
    """
    import time as clock

    media = SessionMedia(tmp_path, qat_module=FakeQat(), record_video=False)
    started = clock.perf_counter()
    name = media.take_for_gap("gap-0")
    elapsed = clock.perf_counter() - started

    assert name, "the gap got no picture at all"
    assert elapsed < 0.1, (
        f"take_for_gap blocked the poll loop for {elapsed * 1000:.0f}ms")
    media.settle()
    assert media.stills() == [name], "and the picture was never actually written"


def test_gap_stills_are_taken_even_when_step_capture_is_off(tmp_path):
    """They answer different questions: one is a convenience, the other is the
    only record of a screen nobody can get back to."""
    media = SessionMedia(tmp_path, qat_module=FakeQat(), record_video=False,
                         capture_steps=False)
    assert media.capture_async("step-000") == ""
    assert media.take_for_gap("gap-0")
