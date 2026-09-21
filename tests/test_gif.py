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
