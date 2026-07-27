"""The interpreted pixel model: a grid of palette indices.

An :class:`IndexGrid` is the codec-neutral "framebuffer" the whole editor works
in — one **palette index per pixel**, row-major, decoupled from any palette (see
``docs/graphics-formats-reference/implementation-guide.md`` §1). Decoding bytes
produces index grids; rendering turns indices into color on the ``ui`` side;
editing paints indices; saving encodes indices back to bytes.

The same class serves as both a single tile (e.g. 8x8) and a composed image made
of many tiles, so tile codecs and the canvas share one type. It is deliberately
Qt-free.
"""

from __future__ import annotations

from celpix.core.grid import PixelGrid


class IndexGrid(PixelGrid):
    """A row-major grid of 8-bit palette indices.

    Indices are plain ints in ``0..255``; the meaningful range for a given
    interpretation is ``0..2**bpp - 1``, but the grid itself does not enforce a
    bit depth — that is the codec's concern.
    """

    __slots__ = ()

    bytes_per_pixel = 1

    def get(self, x: int, y: int) -> int:
        return self._data[y * self._width + x]

    def set(self, x: int, y: int, value: int) -> None:
        # Indices are one byte; mask so a caller passing a wider int can't corrupt
        # neighbouring pixels via bytearray's range check.
        self._data[y * self._width + x] = value & 0xFF
