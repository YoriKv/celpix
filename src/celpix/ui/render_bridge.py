"""The render bridge: the single seam that turns indices into pixels.

The model, pipeline, and plugins are Qt-free and produce *indices* — an
:class:`~celpix.core.index_grid.IndexGrid` — never pixels. Turning that into
something on screen is this component's job, and it is the only place index→color
happens (``docs/design/overview.md`` §4).

The MVP renders to a ``QImage.Format_Indexed8`` whose color table *is* the
palette window: the stored index byte maps straight to a color, so a palette or
subpalette change is just a new color table, no re-rasterization. Pixmap caching
and per-region invalidation are the documented next step here, not built yet.
"""

from __future__ import annotations

from PySide6.QtGui import QImage

from celpix.core.palette import Palette


def render(grid, palette: Palette, subpalette_base: int = 0) -> QImage:
    """Rasterize ``grid`` to a QImage.

    An index grid resolves through ``palette`` (offset by ``subpalette_base``, so a
    tile drawn for palette row *n* renders correctly, ``base = n * 2**bpp``). A
    direct-color :class:`~celpix.core.argb_grid.ArgbGrid` already carries ARGB and
    is blitted straight to ``Format_ARGB32``, ignoring the palette.
    """
    if getattr(grid, "bytes_per_pixel", 1) == 4:
        return _render_argb(grid)
    # QRgb is 0xAARRGGBB — exactly what Palette stores — so colors pass straight
    # through. A too-short palette yields the magenta sentinel per Palette.color.
    table = [palette.color(subpalette_base + i) for i in range(256)]
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


def _render_argb(grid) -> QImage:
    """Blit a direct-color ArgbGrid straight to Format_ARGB32 (no palette)."""
    w, h = grid.width, grid.height
    if w == 0 or h == 0:
        return QImage()
    # The grid stores little-endian ARGB (B,G,R,A per pixel) = Format_ARGB32's layout;
    # rows are 4-byte-aligned already (4 bytes/pixel). copy() so we own the buffer.
    image = QImage(bytes(grid.data), w, h, w * 4, QImage.Format.Format_ARGB32)
    return image.copy()
