"""The interpreted pixel model: a grid of palette indices.

An :class:`IndexGrid` is the codec-neutral "framebuffer" the whole editor works
in — one **palette index per pixel**, row-major, decoupled from any palette (see
``docs/graphics-formats-reference/implementation-guide.md`` §1). Decoding bytes
produces index grids; rendering turns indices into colour on the ``ui`` side;
editing paints indices; saving encodes indices back to bytes.

The same class serves as both a single tile (e.g. 8x8) and a composed image made
of many tiles, so tile codecs and the canvas share one type. It is deliberately
Qt-free.
"""

from __future__ import annotations


class IndexGrid:
    """A row-major grid of 8-bit palette indices.

    Indices are plain ints in ``0..255``; the meaningful range for a given
    interpretation is ``0..2**bpp - 1``, but the grid itself does not enforce a
    bit depth — that is the codec's concern.
    """

    __slots__ = ("_width", "_height", "_data")

    def __init__(
        self, width: int, height: int, data: bytearray | bytes | None = None
    ) -> None:
        if width < 0 or height < 0:
            raise ValueError("IndexGrid dimensions must be non-negative")
        self._width = width
        self._height = height
        if data is None:
            self._data = bytearray(width * height)
        else:
            if len(data) != width * height:
                raise ValueError(
                    f"data length {len(data)} != width*height {width * height}"
                )
            self._data = bytearray(data)

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def data(self) -> bytearray:
        """The backing index buffer, row-major. Mutable by design (editing)."""
        return self._data

    # One byte per pixel — the constant that lets arrangement/render treat this and
    # the 4-byte :class:`~celpix.core.argb_grid.ArgbGrid` uniformly.
    bytes_per_pixel = 1

    def get(self, x: int, y: int) -> int:
        return self._data[y * self._width + x]

    def set(self, x: int, y: int, value: int) -> None:
        # Indices are one byte; mask so a caller passing a wider int can't corrupt
        # neighbouring pixels via bytearray's range check.
        self._data[y * self._width + x] = value & 0xFF

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, IndexGrid):
            return NotImplemented
        return (
            self._width == other._width
            and self._height == other._height
            and self._data == other._data
        )

    def __repr__(self) -> str:
        return f"IndexGrid({self._width}x{self._height})"
