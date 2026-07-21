"""The palette model: a list of colours the pixel indices resolve through.

A :class:`Palette` is just an ordered list of 32-bit ``0xAARRGGBB`` values — the
colour-codec output on the palette pathway, and what the render bridge reads to
turn indices into on-screen colour. It is Qt-free; the ``ui`` side converts these
ints to ``QColor``/``QImage`` colour tables.

Native palette encodings (BGR555, RGB888, …) live in the colour codec; by the
time colours reach this class they are already normalised to ARGB.
"""

from __future__ import annotations

# Rendered for any index that falls outside the loaded palette, so a short or
# missing palette shows an obvious sentinel instead of crashing the canvas.
MISSING_COLOR = 0xFFFF00FF  # opaque magenta


class Palette:
    """An ordered list of ``0xAARRGGBB`` colours."""

    __slots__ = ("_colors",)

    def __init__(self, colors: list[int] | None = None) -> None:
        self._colors: list[int] = list(colors) if colors is not None else []

    @property
    def colors(self) -> list[int]:
        """The backing colour list (mutable — palette editing lands here)."""
        return self._colors

    def __len__(self) -> int:
        return len(self._colors)

    def color(self, index: int) -> int:
        """ARGB for ``index``, or :data:`MISSING_COLOR` if out of range."""
        if 0 <= index < len(self._colors):
            return self._colors[index]
        return MISSING_COLOR

    @staticmethod
    def grayscale(count: int) -> Palette:
        """A fallback ramp for viewing pixels before a real palette is loaded."""
        if count <= 0:
            return Palette([])
        if count == 1:
            return Palette([0xFF000000])
        step = 255 // (count - 1)
        return Palette(
            [
                0xFF000000 | (v << 16) | (v << 8) | v
                for v in (i * step for i in range(count))
            ]
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Palette):
            return NotImplemented
        return self._colors == other._colors

    def __repr__(self) -> str:
        return f"Palette({len(self._colors)} colors)"
