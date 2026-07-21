"""The render bridge: the single seam that turns indices into pixels.

The model, pipeline, and plugins are Qt-free and produce *indices* — an
:class:`~celpix.core.index_grid.IndexGrid` — never pixels. Turning that into
something on screen is this component's job, and it is the only place index→colour
happens (``docs/design/overview.md`` §4).

The MVP renders to a ``QImage.Format_Indexed8`` whose colour table *is* the
palette window: the stored index byte maps straight to a colour, so a palette or
subpalette change is just a new colour table, no re-rasterization. Pixmap caching
and per-region invalidation are the documented next step here, not built yet.
"""

from __future__ import annotations

from PySide6.QtGui import QImage

from celpix.core.index_grid import IndexGrid
from celpix.core.palette import Palette


def render(grid: IndexGrid, palette: Palette, subpalette_base: int = 0) -> QImage:
    """Rasterize ``grid`` through ``palette`` (offset by ``subpalette_base``).

    ``subpalette_base`` shifts which palette window indices resolve through, so a
    tile drawn for palette row *n* renders correctly (``base = n * 2**bpp``).
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
    # QRgb is 0xAARRGGBB — exactly what Palette stores — so colours pass straight
    # through. A too-short palette yields the magenta sentinel per Palette.color.
    image.setColorTable([palette.color(subpalette_base + i) for i in range(256)])
    # QImage does not copy the Python buffer; return an owning copy so ``buf`` can
    # be freed safely.
    return image.copy()
