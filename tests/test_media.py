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


def test_a_screenshot_that_fails_is_not_an_exception(tmp_path):
    """A recording continues whatever the screen is doing."""
    media = SessionMedia(tmp_path, qat_module=FakeQat(works=False))
    assert media.take("gap-0") == ""
    assert media.stills() == []


def test_no_qat_means_no_still_and_no_crash(tmp_path):
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

    drop, = controller.recording.drops
    assert drop.shot, "the gap has no picture of the screen"
    assert (tmp_path / "shots" / drop.shot).exists()


def test_a_gap_is_photographed_once(controller, tmp_path):    # noqa: F811
    controller.media = SessionMedia(tmp_path, qat_module=FakeQat(),
                                    record_video=False)
    _record_a_gap(controller)
    first = controller.recording.drops[0].shot
    controller.poll()
    controller.poll()
    assert controller.recording.drops[0].shot == first
    assert len(controller.media.stills()) == 1


def test_a_failed_screenshot_does_not_stop_the_recording(controller, tmp_path):  # noqa: F811
    controller.media = SessionMedia(tmp_path, qat_module=FakeQat(works=False),
                                    record_video=False)
    _record_a_gap(controller)
    assert controller.recording.drops[0].shot == ""
    assert controller.state is State.RECORDING
    assert len(controller.recording.actions) > 1, "the session kept recording"


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

def test_a_gap_that_cannot_be_photographed_is_not_retried(controller, tmp_path):  # noqa: F811
    """The log spam.

    Retrying anything without a picture on every tick of the pump meant a
    failing screenshot was attempted several times a second for the length of
    the session, logging each time.
    """
    camera = FakeQat(works=False)
    controller.media = SessionMedia(tmp_path, qat_module=camera,
                                    record_video=False)
    _record_a_gap(controller)
    attempts = len(camera.asked)

    for _ in range(20):
        controller.poll()

    assert len(camera.asked) == attempts == 1, (
        "the camera was asked again for a gap it had already failed on")


def test_gaps_in_the_same_second_share_one_picture(tmp_path):
    """Six unnamed check boxes in one dialog are six gaps looking at one screen."""
    media = SessionMedia(tmp_path, qat_module=FakeQat(), record_video=False)
    names = [media.take_for_gap(f"gap-{n}") for n in range(6)]

    assert len(set(names)) == 1, "six identical photographs of the same screen"
    assert len(media.stills()) == 1


def test_a_gap_much_later_gets_its_own_picture(tmp_path, monkeypatch):
    media = SessionMedia(tmp_path, qat_module=FakeQat(), record_video=False)
    clock = [1000.0]
    monkeypatch.setattr("qat_recorder.media.time.monotonic", lambda: clock[0])

    first = media.take_for_gap("gap-0")
    clock[0] += 60.0                      # a minute later, a different screen
    second = media.take_for_gap("gap-1")

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
    controller = RecorderController(
        ControllerQat(), lib_path="/tmp/lib.so", app_path="/tmp/app",
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
