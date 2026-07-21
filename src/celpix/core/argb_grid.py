"""The direct-colour framebuffer: a grid of packed ARGB pixels.

Most pixel codecs decode to palette **indices** (an
:class:`~celpix.core.index_grid.IndexGrid`) rendered through a palette.
*Direct-colour* codecs skip the palette and produce a colour per pixel; this is their
output — one ``0xAARRGGBB`` per pixel, row-major.

It deliberately mirrors ``IndexGrid``'s shape (``width``/``height``/``data``,
``bytes_per_pixel``, an ``(x, y)`` accessor, a ``(w, h[, data])`` constructor) so the
arrangement compositor and the render bridge handle both with no special-casing. The
backing buffer stores each pixel little-endian (bytes ``B, G, R, A``), which is
exactly ``QImage.Format_ARGB32``'s layout, so the render bridge hands it straight to
Qt with no repack.
"""

from __future__ import annotations


class ArgbGrid:
    """A row-major grid of packed ``0xAARRGGBB`` pixels (4 bytes each)."""

    __slots__ = ("_width", "_height", "_data")

    bytes_per_pixel = 4

    def __init__(
        self, width: int, height: int, data: bytearray | bytes | None = None
    ) -> None:
        if width < 0 or height < 0:
            raise ValueError("ArgbGrid dimensions must be non-negative")
        self._width = width
        self._height = height
        n = width * height * 4
        if data is None:
            self._data = bytearray(n)
        else:
            if len(data) != n:
                raise ValueError(f"data length {len(data)} != width*height*4 {n}")
            self._data = bytearray(data)

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def data(self) -> bytearray:
        """The backing pixel buffer (little-endian ARGB32), row-major. Mutable."""
        return self._data

    def get(self, x: int, y: int) -> int:
        off = (y * self._width + x) * 4
        return int.from_bytes(self._data[off : off + 4], "little")

    def set(self, x: int, y: int, argb: int) -> None:
        off = (y * self._width + x) * 4
        self._data[off : off + 4] = (argb & 0xFFFFFFFF).to_bytes(4, "little")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ArgbGrid):
            return NotImplemented
        return (
            self._width == other._width
            and self._height == other._height
            and self._data == other._data
        )

    def __repr__(self) -> str:
        return f"ArgbGrid({self._width}x{self._height})"
