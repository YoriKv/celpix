"""The direct-color framebuffer: a grid of packed ARGB pixels.

Most pixel codecs decode to palette **indices** (an
:class:`~celpix.core.index_grid.IndexGrid`) rendered through a palette.
*Direct-color* codecs skip the palette and produce a color per pixel; this is their
output — one ``0xAARRGGBB`` per pixel, row-major.

It shares :class:`~celpix.core.grid.PixelGrid` with ``IndexGrid`` so the
arrangement compositor and the render bridge handle both with no special-casing.
The backing buffer stores each pixel little-endian (bytes ``B, G, R, A``), which is
exactly ``QImage.Format_ARGB32``'s layout, so the render bridge hands it straight to
Qt with no repack.
"""

from __future__ import annotations

from celpix.core.grid import PixelGrid


class ArgbGrid(PixelGrid):
    """A row-major grid of packed ``0xAARRGGBB`` pixels (4 bytes each)."""

    __slots__ = ()

    bytes_per_pixel = 4

    def get(self, x: int, y: int) -> int:
        off = (y * self._width + x) * 4
        d = self._data
        return d[off] | d[off + 1] << 8 | d[off + 2] << 16 | d[off + 3] << 24

    def set(self, x: int, y: int, argb: int) -> None:
        off = (y * self._width + x) * 4
        self._data[off : off + 4] = (argb & 0xFFFFFFFF).to_bytes(4, "little")
