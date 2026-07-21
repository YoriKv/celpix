"""The palette model: a list of colours the pixel indices resolve through.

A :class:`Palette` is just an ordered list of 32-bit ``0xAARRGGBB`` values — the
colour-codec output on the palette pathway, and what the render bridge reads to
turn indices into on-screen colour. It is Qt-free; the ``ui`` side converts these
ints to ``QColor``/``QImage`` colour tables.

Native palette encodings (BGR555, RGB888, …) live in the colour codec; by the
time colours reach this class they are already normalised to ARGB.
"""

from __future__ import annotations

import colorsys

# Rendered for any index that falls outside the loaded palette, so a short or
# missing palette shows an obvious sentinel instead of crashing the canvas.
MISSING_COLOR = 0xFFFF00FF  # opaque magenta

# Head of the default palette: hand-picked so every prefix stays maximally
# distinguishable — 1bpp gets black/white, 2bpp adds red/green, and so on.
_DEFAULT_HEAD = (
    0xFF000000,  # black
    0xFFFFFFFF,  # white
    0xFFFF0000,  # red
    0xFF00FF00,  # green
    0xFF0000FF,  # blue
    0xFFFFFF00,  # yellow
    0xFFFF00FF,  # magenta
    0xFF00FFFF,  # cyan
    0xFFFF8000,  # orange
    0xFF8000FF,  # purple
    0xFF00FF80,  # spring green
    0xFF800000,  # maroon
    0xFF008080,  # teal
    0xFF804000,  # brown
    0xFF808080,  # mid gray
    0xFFFF80C0,  # pink
)

# Tail generation: neighbouring entries must differ in more than hue, so the
# golden-ratio hue walk cycles through saturation/value tiers as well.
_TAIL_TIERS = ((0.9, 1.0), (0.6, 1.0), (0.9, 0.55), (0.45, 0.8))


def _tail_color(i: int) -> int:
    h = (i * 0.61803398875) % 1.0
    s, v = _TAIL_TIERS[i % len(_TAIL_TIERS)]
    r, g, b = (round(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))
    return 0xFF000000 | (r << 16) | (g << 8) | b


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
    def default(count: int) -> Palette:
        """The fallback for viewing pixels before a real palette is loaded:
        black, white, then contrasting colours. Deterministic in ``count``."""
        if count <= 0:
            return Palette([])
        colors = list(_DEFAULT_HEAD[:count])
        colors.extend(_tail_color(i) for i in range(len(colors), count))
        return Palette(colors)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Palette):
            return NotImplemented
        return self._colors == other._colors

    def __repr__(self) -> str:
        return f"Palette({len(self._colors)} colors)"
