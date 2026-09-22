"""The animated-GIF writer, checked against Qt's own GIF reader."""

from __future__ import annotations

import random

from celpix.core import gif


def test_gif_frames_round_trip_through_a_real_decoder(qtbot) -> None:
    """Every frame decodes to the pixels it was given - transparency, a blank
    frame, and enough noise to fill the LZW table and force a clear code, which
    is where a code-width slip shows up."""
    from PySide6.QtCore import QBuffer, QByteArray
    from PySide6.QtGui import QImage, QImageReader

    rng = random.Random(7)
    colours = [0] + [0xFF000000 | rng.randrange(1 << 24) for _ in range(40)]
    w, h = 96, 80
    noise = [rng.choice(colours) for _ in range(w * h)]
    frames = [(noise, 5), (None, 3), ([colours[1]] * (w * h), 7)]

    data = QByteArray(gif.encode(w, h, frames))
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer, b"gif")
    for pixels, _ in frames:
        image = reader.read().convertToFormat(QImage.Format.Format_ARGB32)
        got = [image.pixel(i % w, i // w) for i in range(w * h)]
        want = pixels or [0] * (w * h)
        assert [0 if g >> 24 == 0 else g for g in got] == [
            0 if p >> 24 == 0 else p for p in want
        ]

    # Five ticks at 60 Hz is 8.33 cs: rounded on the running total, the run keeps
    # its length instead of losing a third of a centisecond a step.
    assert sum(gif.delays_cs([5] * 6, 60)) == 50


def test_repeating_a_frame_is_the_same_bytes_as_rebuilding_it() -> None:
    """A step naming a frame already seen is keyed and compressed once, shared by
    the identity of the pixels it was handed - so holding a frame has to write
    exactly what rebuilding it writes."""
    pixels = [0xFF204060] * 64 + [0] * 64
    shared = gif.encode(8, 16, [(pixels, 4), (None, 2), (pixels, 4), (None, 2)])
    apart = gif.encode(8, 16, [(pixels, 4), (None, 2), (list(pixels), 4), (None, 2)])
    assert shared == apart


def test_a_sequence_exports_the_pixels_the_strip_holds(qtbot, tmp_path) -> None:
    """The GIF export cuts its frames from the player's strip and reads them off
    the buffer, so a padded row must not shift the picture - and a step naming a
    frame the file does not hold leaves a transparent one."""
    from PySide6.QtCore import QBuffer, QByteArray, QRect
    from PySide6.QtGui import QImage, QImageReader

    from celpix.core.animation import Sequence, Step
    from celpix.ui import export

    # 5 wide, so an ARGB32 row is not a whole number of anything but words, and
    # the two frames sit side by side as the player's strip lays them out.
    strip = QImage(10, 3, QImage.Format.Format_ARGB32)
    strip.fill(0xFF102030)
    strip.setPixel(0, 0, 0xFF405060)
    strip.setPixel(9, 2, 0xFF708090)
    rects = [QRect(0, 0, 5, 3), QRect(5, 0, 5, 3)]
    sequence = Sequence((Step(4, 0), Step(4, 1), Step(4, 0), Step(4, 7)))
    path = tmp_path / "flap.gif"
    export.save_sequence_gif(strip, rects, sequence, 60, str(path))

    data = QByteArray(path.read_bytes())
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer, b"gif")
    for step in sequence.steps:
        got = reader.read().convertToFormat(QImage.Format.Format_ARGB32)
        want = (
            strip.copy(rects[step.frame]).convertToFormat(QImage.Format.Format_ARGB32)
            if step.frame < len(rects)
            else None
        )
        pixels = [got.pixel(x, y) for y in range(3) for x in range(5)]
        if want is None:
            assert all(p >> 24 == 0 for p in pixels)
        else:
            assert pixels == [want.pixel(x, y) for y in range(3) for x in range(5)]
