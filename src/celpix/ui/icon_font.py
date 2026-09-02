"""The icon set as a *font*: one bundled face, glyphs rasterized on demand.

celPix's icons are drawn from ``resources/fonts/material-symbols-subset.ttf``
rather than shipped as bitmaps. Every size is rasterized from outlines, so a
20 px tool button and the same mark on a 200% display are both crisp, where a
single PNG could only be stretched — and one file covers the whole UI.

**The face is a variable font, and celPix draws it light.** Material Symbols
carries a ``wght`` axis, so the icons' stroke weight is a number here rather
than a property of which file was shipped: :data:`_WEIGHT` sets it once for the
app. A solid-weight icon set reads as heavy furniture next to the art on the
canvas, which is the thing meant to hold the eye; at the file list's 13x16 the
difference is legibility rather than taste, since a dense mark at that size
fills in to a blob. The other axes (``FILL``, ``GRAD``, ``opsz``) are left at
their defaults — pinning ``opsz`` to its small-size end measurably *worsened*
the 13 px markers, washing them out where the default holds them crisp.

The file ships **subset to the codepoints celPix actually draws** — 19 KB of a
10.6 MB upstream face — which is why a new :class:`~celpix.ui.glyphs.Glyph` has
to be followed by ``tools/subset_icon_font.py``: the glyph is not in the bundled
font until it is.

The face is registered from **bytes**, not a path: in a frozen build the
resources live inside the bundle, where a path-based load has nothing to open.
Registration needs a live QApplication, so it happens on the first icon rather
than at import, and is retried if that first attempt came too early.

What comes back is a **mask** — the glyph as ink on transparency — because that
is what the rest of the app already knows how to finish: every icon in celPix is
stamped with a palette color so it tracks the theme, and several are stamped
twice (a rail button bakes its own disabled shade). See
:func:`~celpix.ui.widgets.stamped`.

Glyphs are fitted by their **ink**, not their font metrics. An icon font's line
box is sized for text — full ascent and descent, the same for every glyph — so
drawing at ``pixelSize = box`` leaves a wide icon floating in a third of the
square it was given. Measuring the tight bounding rect and scaling to that fills
the box the way the bitmaps it replaced did, and centres on the mark rather than
on the baseline.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QPointF, QRect, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QImage,
    QPainter,
    QPixmap,
)

from celpix import resources
from celpix.ui.glyphs import Glyph
from celpix.ui.widgets import stamped

_FONT_FILE = ("fonts", "material-symbols-subset.ttf")

# The face's ``wght`` axis, which runs 100-700 with 400 the upstream default.
# See the module docstring for why celPix sits below it.
_WEIGHT = 300.0

# How much of its box a glyph is allowed to fill. Fitting the ink to the *whole*
# box puts an antialiased edge on the boundary pixel, which reads as an icon that
# has been cut off - worst in the file list, whose 13x16 box is the tightest in
# the app and whose rows sit close enough together to compare. The margin is the
# one the bitmaps these replaced had built into their own art, restored here
# where it can be one number instead of a property of ten PNGs.
_FILL = 0.86

# How many render-and-measure passes the fit may take. Two normally land it; the
# cap is what guarantees the loop ends on a face whose rasterizer never settles.
_FIT_PASSES = 5

# The family name Qt reports for the registered face, resolved once. Kept as
# module state rather than an lru_cache so a failed load (no QApplication yet)
# is retried rather than remembered.
_family: str | None = None


def icon_font_family() -> str | None:
    """The registered icon-font family, or ``None`` if it could not load.

    ``None`` is a face celPix will draw nothing from — callers fall back to an
    empty mask rather than to the system font, which would render every one of
    these private-use codepoints as a tofu box.
    """
    global _family
    if _family is None:
        font_id = QFontDatabase.addApplicationFontFromData(
            QByteArray(resources.read_bytes(*_FONT_FILE))
        )
        families = QFontDatabase.applicationFontFamilies(font_id)
        # -1 (no application yet, or a corrupt face) yields an empty list.
        _family = families[0] if families else None
    return _family


def glyph_mask(glyph: Glyph, box: QSize) -> QPixmap:
    """``glyph`` as white ink on transparency, fitted and centred in ``box``.

    ``box`` is in **device** pixels: the caller decides the resolution, since it
    is the one that knows the display's scale and has to stamp the ratio onto
    the finished pixmap. White because only the alpha shape survives the stamp.

    The glyph is measured **off its own pixels**, not off the font's metrics.
    ``QFontMetricsF.tightBoundingRect`` reports a bounding box that a variable
    face at a non-default axis position does not honour — with the weight axis
    at 300 it over-reported some glyphs' height by a third, and centring on that
    left the transform bar's flip arrows visibly riding high. Rasterizing and
    finding the alpha bounds is exact by construction, for any face and any axis,
    and the finished mark is *copied* into place rather than re-drawn, so what
    was measured is what lands.
    """
    mask = QPixmap(box)
    mask.fill(Qt.GlobalColor.transparent)
    family = icon_font_family()
    if family is None or box.width() <= 0 or box.height() <= 0:
        return mask
    room = QSize(round(box.width() * _FILL), round(box.height() * _FILL))
    drawn = _fitted_ink(family, glyph.value, room)
    if drawn is None:  # a codepoint this face doesn't map
        return mask
    painter = QPainter(mask)
    painter.drawImage(
        (box.width() - drawn.width()) // 2,
        (box.height() - drawn.height()) // 2,
        drawn,
    )
    painter.end()
    return mask


def _fitted_ink(family: str, text: str, room: QSize) -> QImage | None:
    """``text`` rasterized as large as it fits ``room``, cropped to its ink.

    ``None`` when the glyph puts no pixels down at all — a codepoint missing
    from the face, which is what a member added to :class:`Glyph` without
    re-running ``tools/subset_icon_font.py`` looks like from here.

    Converges rather than solving: each pass renders, measures what it actually
    got, and rescales by the shortfall. The rasterizer rounds outlines onto the
    pixel grid, so the relation between pixel size and ink size is only nearly
    linear and a single division would land a pixel out either way; two passes
    are normally enough, and :data:`_FIT_PASSES` caps it.
    """
    size = max(1, room.height())
    best: QImage | None = None
    for _ in range(_FIT_PASSES):
        drawn = _render(family, text, size)
        if drawn is None:
            return None
        if drawn.width() <= room.width() and drawn.height() <= room.height():
            best = drawn
            # Room to spare in both directions means the next pass grows it; a
            # pass that cannot grow (the scale rounds back to this size) is the
            # fixed point, so stop there rather than rendering it again.
            scale = min(room.width() / drawn.width(), room.height() / drawn.height())
            grown = max(1, int(size * scale))
            if grown <= size:
                break
            size = grown
        else:
            # Over its room: shrink by the overshoot, at least a pixel, so a
            # rounding that lands on the same size cannot loop.
            scale = min(room.width() / drawn.width(), room.height() / drawn.height())
            size = max(1, min(int(size * scale), size - 1))
    return best if best is not None else drawn


def _render(family: str, text: str, size: int) -> QImage | None:
    """``text`` at ``size``, cropped to the pixels it actually inked.

    The scratch is twice the pixel size square with the baseline placed a half
    size in, which is room enough for any glyph in an icon face (their marks are
    drawn to roughly the em box) without measuring one first.
    """
    scratch = QImage(size * 2, size * 2, QImage.Format.Format_ARGB32_Premultiplied)
    scratch.fill(Qt.GlobalColor.transparent)
    painter = QPainter(scratch)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setPen(QColor(Qt.GlobalColor.white))
    painter.setFont(_icon_font(family, size))
    painter.drawText(QPointF(size * 0.5, size * 1.5), text)
    painter.end()
    bounds = _ink_bounds(scratch)
    return None if bounds is None else scratch.copy(bounds)


def _ink_bounds(image: QImage) -> QRect | None:
    """The tightest rectangle holding every non-transparent pixel, or ``None``.

    Read a row of alpha at a time rather than pixel by pixel: this runs for every
    icon the app bakes, and ``pixelColor`` per pixel over even a small scratch is
    the difference between imperceptible and a visible hitch on a theme switch.
    """
    alpha = image.convertToFormat(QImage.Format.Format_Alpha8)
    top, bottom, left, right = None, None, alpha.width(), -1
    for y in range(alpha.height()):
        row = bytes(alpha.constScanLine(y))[: alpha.width()]
        if not any(row):
            continue
        top = y if top is None else top
        bottom = y
        left = min(left, next(x for x, a in enumerate(row) if a))
        right = max(
            right, len(row) - 1 - next(x for x, a in enumerate(reversed(row)) if a)
        )
    if top is None:
        return None
    return QRect(left, top, right - left + 1, bottom - top + 1)


def _icon_font(family: str, pixel_size: int) -> QFont:
    """The icon face at ``pixel_size``, on celPix's weight."""
    font = QFont(family)
    font.setPixelSize(pixel_size)
    font.setVariableAxis(QFont.Tag("wght"), _WEIGHT)
    return font


def glyph_pixmap(glyph: Glyph, color: QColor, box: QSize, ratio: float) -> QPixmap:
    """``glyph`` in ``color``, ``box`` **logical** units rendered at ``ratio``.

    The one call a widget needs to put a themed icon on screen: it decides the
    box and hands over the palette color it wants, and gets back a pixmap that
    measures that box in layout units however many device pixels it holds.
    """
    mask = glyph_mask(
        glyph, QSize(round(box.width() * ratio), round(box.height() * ratio))
    )
    tinted = stamped(mask, color)
    tinted.setDevicePixelRatio(ratio)
    return tinted


def glyph_icon(
    glyph: Glyph, color: QColor, size: int = 16, ratio: float = 1.0
) -> QIcon:
    """``glyph`` as a square :class:`QIcon` in ``color`` — for a button's face.

    The button counterpart to :func:`glyph_pixmap`: a one-pixmap icon is all a
    button needs, since Qt derives the greyed form from it itself. ``size``
    defaults to 16 because that is the icon size the styles give a button that
    never asked for one, which is every caller so far; a button that sets its own
    ``iconSize`` has to say so here too, or Qt scales this pixmap to fit.

    The art is *baked* rather than styled, so a caller has to re-bake it when the
    theme or the device scale changes — the window's ``_rebake_icons``.
    """
    return QIcon(glyph_pixmap(glyph, color, QSize(size, size), ratio))
