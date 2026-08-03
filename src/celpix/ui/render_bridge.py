"""The render bridge: the single seam that turns indices into pixels.

The model, pipeline, and plugins are Qt-free and produce *indices* — an
:class:`~celpix.core.index_grid.IndexGrid` — never pixels. Turning that into
something on screen is this component's job, and it is the only place index→color
happens (``docs/design/overview.md`` §4).

An index grid renders to a ``QImage.Format_Indexed8`` whose color table *is* the
palette window: the stored index byte maps straight to a color, so a palette or
subpalette change is just a new color table, no re-rasterization. A direct-color
grid (:class:`~celpix.core.argb_grid.ArgbGrid`) skips the palette entirely — its
buffer is already ``Format_ARGB32``'s layout, so it is wrapped, not converted.

One image carries one colour table, so a view with **pinned palette regions**
(:mod:`celpix.core.paletteregions`) — where different tiles render through
different subpalette rows — cannot express the row in the table. There the row
travels in the indices instead and the table is the plain palette:
:func:`render_pinned`.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QImage, QPainter

from celpix.core.palette import Palette

TRANSPARENT = 0x00000000

# What a position the map does not draw is painted with. Black rather than the
# canvas surround, because it stands for the *picture's* own background — the
# colour the authoring tool's screen showed through an undrawn position, and part
# of the exported file — where the surround stands for "no file here" and stops
# at the canvas. Opaque, so an export carries it rather than a hole.
HIDDEN_BACKGROUND = QColor(0x00, 0x00, 0x00)


def _clear_zeros(table: list[int], stride: int) -> list[int]:
    """``table`` with every palette row's index 0 made fully transparent.

    The console's rule, applied at the one seam where indices become pixels: index
    0 of a BG palette row paints nothing. Doing it in the **colour table** rather
    than to the grid is what keeps it free — no second rasterization, and the same
    image object as before — and it is also the only place that can, since the
    indices themselves have to stay editable as the numbers the file holds.

    ``stride`` is the width of one palette row in colours (``2**bpp``), because
    which entries mean "index 0" depends on whether the row is folded into the
    indices: at ``stride`` 256 only entry 0 is one (:func:`render`, where the row
    lives in the table's offset), and at 16 every sixteenth is
    (:func:`render_pinned`, where it lives in the indices).
    """
    out = list(table)
    for at in range(0, len(out), max(1, stride)):
        out[at] = TRANSPARENT
    return out


def render(
    grid,
    palette: Palette,
    subpalette_base: int = 0,
    *,
    transparent_zero: bool = False,
) -> QImage:
    """Rasterize ``grid`` to a QImage.

    An index grid resolves through ``palette`` (offset by ``subpalette_base``, so a
    tile drawn for palette row *n* renders correctly, ``base = n * 2**bpp``). A
    direct-color :class:`~celpix.core.argb_grid.ArgbGrid` already carries ARGB and
    is blitted straight to ``Format_ARGB32``, ignoring the palette.

    ``transparent_zero`` clears index 0 (:func:`_clear_zeros`). Only entry 0 is
    cleared here: the whole table is already one palette row, offset by
    ``subpalette_base``, so index 0 of *this* row is the only index 0 there is.
    """
    if grid.bytes_per_pixel == 4:
        return _render_argb(grid)
    # QRgb is 0xAARRGGBB — exactly what Palette stores — so colors pass straight
    # through. A too-short palette yields the magenta sentinel per Palette.color.
    table = [palette.color(subpalette_base + i) for i in range(256)]
    if transparent_zero:
        table = _clear_zeros(table, 256)
    return indexed_image(grid, table)


def render_pinned(
    grid,
    palette: Palette,
    row_stride: int = 0,
    *,
    transparent_zero: bool = False,
) -> QImage:
    """Rasterize ``grid`` when its indices already carry their subpalette row.

    The counterpart of :func:`render` for a view with pinned palette regions
    (:mod:`celpix.core.paletteregions`). There the row cannot live in the colour
    table, because one image has one table and the whole point is that different
    tiles render through different rows — so the row is folded into the *indices*
    upstream (``IndexGrid.shifted``) and the table becomes the palette itself,
    unoffset. An unpinned tile is shifted by the view's own row, so the two paths
    agree pixel for pixel wherever nothing is pinned.

    ``row_stride`` is one palette row in colours, and is needed **only** for
    ``transparent_zero``: with the row in the indices, every ``row_stride``-th
    entry is some row's index 0, and clearing entry 0 alone would leave every
    other row's blank pixels opaque.

    A direct-colour grid never carries indices, so it renders exactly as
    :func:`render` would.
    """
    if grid.bytes_per_pixel == 4:
        return _render_argb(grid)
    table = [palette.color(i) for i in range(256)]
    if transparent_zero:
        table = _clear_zeros(table, row_stride or 256)
    return indexed_image(grid, table)


def indexed_image(grid, color_table: list[int]) -> QImage:
    """Build a ``Format_Indexed8`` QImage from an index grid + ARGB color table.

    The seam :func:`render` uses for the live view (a 256-entry subpalette table)
    and export reuses for a compact, exactly-sized table (one entry per index the
    format can produce). ``color_table`` is a list of ``0xAARRGGBB`` ints; any
    entry with alpha < 255 makes Qt emit a ``tRNS`` chunk when the image is saved
    to PNG, so a palette that carries alpha round-trips.
    """
    w, h = grid.width, grid.height
    if w == 0 or h == 0:
        return QImage()

    # Format_Indexed8 rows must be 32-bit aligned; pad each row to a 4-byte stride.
    stride = (w + 3) & ~3
    src = grid.data
    if stride == w:
        buf = bytes(src)
    else:
        padded = bytearray(stride * h)
        for y in range(h):
            padded[y * stride : y * stride + w] = src[y * w : (y + 1) * w]
        buf = bytes(padded)

    image = QImage(buf, w, h, stride, QImage.Format.Format_Indexed8)
    image.setColorTable(color_table)
    # QImage does not copy the Python buffer; return an owning copy so ``buf`` can
    # be freed safely.
    return image.copy()


def paint_hidden(image: QImage, rects: tuple[tuple[int, int, int, int], ...]) -> QImage:
    """``image`` with the map's undrawn positions filled in the background colour.

    The last step of rendering a tilemap, and the only one that touches pixels
    the composer did not put there. It is a *paint* rather than an index the
    composer could have written, because the composed grid's every index belongs
    to the palette and none of them is reserved: a map using all sixteen rows
    leaves nothing to mean "nothing here", and taking an index anyway would
    silently repaint whichever colour lost the draw
    (``docs/design/tilemap-entry.md`` §6).

    Both the canvas and PNG export call it on the output of one
    :func:`~celpix.pipeline.pipeline.tilemap_image`, so what is exported is what
    is on screen — the rectangles are computed once, in the pipeline, and this
    end only fills them.

    An image with nothing hidden comes back **untouched and still indexed**,
    which is every document but a stamp layout that uses the bit: the conversion
    below is what a fill costs, since Qt cannot paint onto ``Format_Indexed8``,
    and a map that needs no fill should not pay a format change for it.
    """
    if not rects or image.isNull():
        return image
    out = image.convertToFormat(QImage.Format.Format_ARGB32)
    painter = QPainter(out)
    for x, y, w, h in rects:
        painter.fillRect(x, y, w, h, HIDDEN_BACKGROUND)
    painter.end()
    return out


def _render_argb(grid) -> QImage:
    """Blit a direct-color ArgbGrid straight to Format_ARGB32 (no palette)."""
    w, h = grid.width, grid.height
    if w == 0 or h == 0:
        return QImage()
    # The grid stores little-endian ARGB (B,G,R,A per pixel) = Format_ARGB32's layout;
    # rows are 4-byte-aligned already (4 bytes/pixel). copy() so we own the buffer.
    image = QImage(bytes(grid.data), w, h, w * 4, QImage.Format.Format_ARGB32)
    return image.copy()
