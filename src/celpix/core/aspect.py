"""Non-square pixels: the shape one pixel is drawn at, and what it does to a zoom.

A pixel is not always square. A PC-8801's 640x200 screen puts a pixel that is
twice as tall as it is wide, so a 32x16 tile is a square on the machine and reads
as a squashed rectangle at 1:1; a console's 256-wide mode on a 4:3 television is
the other way about, slightly wider than tall. Drawing every file at 1:1 is
drawing half of them at the wrong shape, and the mistake is invisible — the art
simply looks a bit off, which reads as the art being a bit off.

**A ratio, not a size.** :data:`PixelAspect` is one pixel's width and height as
whole numbers — ``(1, 2)`` for that PC-8801 screen — so it says nothing about how
big the picture is and survives every zoom. It is a **display** property: what
the bytes are is unchanged by it, so nothing downstream of the screen (an export,
a hit test's answer in image pixels, the geometry a codec is asked for) is
allowed to be shaped by it.

**The axis that would shrink is stretched instead** (:func:`scale`). A 1:2 pixel
could as well be drawn half as wide as twice as tall, and at zoom 1 half a pixel
is nothing: a picture would come back with every other column dropped, which is a
worse lie than the square one. So the wide axis of the ratio is what moves, both
factors are at least 1, and one of them is exactly 1.

Qt-free, like everything in :mod:`celpix.core` — the ratio is model state (a
container publishes one and a project stores one) and turning it into device
pixels is the surfaces' job (:class:`~celpix.ui.widgets.PanZoomSurface`).
"""

from __future__ import annotations

#: One pixel's ``(width, height)``, in whatever unit they share — only the ratio
#: between them means anything, so ``(2, 1)`` and ``(8, 4)`` are one aspect.
PixelAspect = tuple[int, int]

#: The ordinary pixel, and what everything falls back to.
SQUARE: PixelAspect = (1, 1)

#: The ratios on offer, in the order the picker lists them: the aspect, its name,
#: and where a user meets it. Fixed rather than free-form because a ratio is a
#: property of the hardware a file was drawn for, and the hardware celPix reads
#: has a handful of answers between it — an arbitrary one would be a number to
#: get wrong rather than a fact to state.
PRESETS: tuple[tuple[PixelAspect, str, str], ...] = (
    (SQUARE, "Square", "One image pixel to one screen pixel."),
    (
        (1, 2),
        "Tall (1:2)",
        "A pixel twice as tall as it is wide, which is what a\n"
        "200-line screen at 640 across draws (PC-8801, PC-9801).\n"
        "A 16x8 tile is a square on the machine.",
    ),
    (
        (2, 1),
        "Wide (2:1)",
        "A pixel twice as wide as it is tall — a 256-wide mode\n"
        "shown at the same width as a 512-wide one, and the\n"
        "high-resolution modes' partner.",
    ),
    (
        (8, 7),
        "Slightly wide (8:7)",
        "The console pixel on a 4:3 television: a 256-wide\n"
        "screen fills a frame that is a little wider than the\n"
        "pixel count implies.",
    ),
    (
        (7, 8),
        "Slightly tall (7:8)",
        "The same correction the other way, for a mode whose\n"
        "horizontal count is the one that was doubled.",
    ),
)


def parse(value: object) -> PixelAspect | None:
    """``value`` as an aspect, or ``None`` for anything that is not one.

    ``None`` rather than :data:`SQUARE` for a miss, because the two say different
    things where this is read: a project file that names no aspect has never been
    asked (and is still open to a container's hint), where one naming ``1:1`` has
    been answered. Written for that reader
    (:func:`~celpix.project.projectfile.load_project`) and used by the context
    hint for the same reason — a container publishing nonsense should be ignored,
    not obeyed.

    Zero and negative sides are rejected rather than clamped: they are not a
    shape, and a ratio with a zero in it would divide by it in :func:`scale`.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        width, height = int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def scale(aspect: PixelAspect) -> tuple[float, float]:
    """``aspect`` as the per-axis factors a surface multiplies its zoom by.

    Both are at least 1 and one of them is exactly 1 — see the module docstring
    for why the stretch goes on the axis that would otherwise shrink. Square
    comes back as ``(1.0, 1.0)``, which is what leaves every existing surface
    drawing exactly as it did.
    """
    width, height = aspect
    if width <= 0 or height <= 0:
        return (1.0, 1.0)
    return (max(1.0, width / height), max(1.0, height / width))


def name(aspect: PixelAspect) -> str:
    """What to call ``aspect`` in a menu or a status line.

    A preset's own name where it is one, and the bare ratio otherwise — a project
    file can hold a ratio this build has no preset for (an older or newer list, a
    hand-edited file), and it still draws correctly, so it still has to be
    nameable.
    """
    for preset, label, _tip in PRESETS:
        if preset == aspect:
            return label
    return f"{aspect[0]}:{aspect[1]}"
