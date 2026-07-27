"""The shape shared by every pixel buffer in the model layer.

:class:`~celpix.core.index_grid.IndexGrid` (one byte per pixel) and
:class:`~celpix.core.argb_grid.ArgbGrid` (four) differ only in what a pixel *is*.
Everything downstream — the arrangement compositor, the transforms, the render
bridge — is written against the common shape below and works on either, so that
shape lives in one place rather than being mirrored by hand in two classes.

Subclasses supply ``bytes_per_pixel`` and the ``get``/``set`` pair; those stay
concrete per class because they sit in the per-pixel path.
"""

from __future__ import annotations


class PixelGrid:
    """A row-major buffer of fixed-width pixels."""

    __slots__ = ("_width", "_height", "_data")

    # Overridden by every concrete grid; declared here so the shared __init__ can
    # size the buffer without knowing which one it is building.
    bytes_per_pixel = 0

    def __init__(
        self, width: int, height: int, data: bytearray | bytes | None = None
    ) -> None:
        name = type(self).__name__
        if width < 0 or height < 0:
            raise ValueError(f"{name} dimensions must be non-negative")
        self._width = width
        self._height = height
        n = width * height * self.bytes_per_pixel
        if data is None:
            self._data = bytearray(n)
        else:
            if len(data) != n:
                raise ValueError(f"data length {len(data)} != {n}")
            self._data = bytearray(data)

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def data(self) -> bytearray:
        """The backing pixel buffer, row-major. Mutable by design (editing)."""
        return self._data

    def copy(self):
        """An independent grid with the same pixels — one buffer copy.

        The snapshot every undoable edit takes before it paints, so it is on the
        per-stroke-sample path: the constructor already copies the buffer, so
        handing it the live one is the whole of it.
        """
        return type(self)(self._width, self._height, self._data)

    def get(self, x: int, y: int) -> int:
        raise NotImplementedError

    def set(self, x: int, y: int, value: int) -> None:
        raise NotImplementedError

    def __eq__(self, other: object) -> bool:
        # Exact type match: an index grid and a direct-color grid of the same
        # dimensions are never the same picture, whatever their bytes say.
        if type(self) is not type(other):
            return NotImplemented
        return (
            self._width == other._width
            and self._height == other._height
            and self._data == other._data
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._width}x{self._height})"
