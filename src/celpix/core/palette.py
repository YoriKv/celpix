"""The palette model: a list of colors the pixel indices resolve through.

A :class:`Palette` is just an ordered list of 32-bit ``0xAARRGGBB`` values — the
color-codec output on the palette pathway, and what the render bridge reads to
turn indices into on-screen color. It is Qt-free; the ``ui`` side converts these
ints to ``QColor``/``QImage`` color tables.

Native palette encodings (BGR555, RGB888, …) live in the color codec; by the
time colors reach this class they are already normalised to ARGB.
"""

from __future__ import annotations

import colorsys

# Rendered for any index that falls outside the loaded palette, so a short or
# missing palette shows an obvious sentinel instead of crashing the canvas.
MISSING_COLOR = 0xFFFF00FF  # opaque magenta

# A full editable palette: 16 rows of the 16-wide swatch grid. Forking a Custom
# palette off the generated default expands to this, so the user gets every
# subpalette row to edit rather than only the current format's index space
# (docs/design/palette-editing.md).
FULL_PALETTE_COUNT = 256

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
    """An ordered list of ``0xAARRGGBB`` colors."""

    __slots__ = ("_colors",)

    def __init__(self, colors: list[int] | None = None) -> None:
        self._colors: list[int] = list(colors) if colors is not None else []

    @property
    def colors(self) -> list[int]:
        """The backing color list (mutable — palette editing lands here)."""
        return self._colors

    def __len__(self) -> int:
        return len(self._colors)

    def color(self, index: int) -> int:
        """ARGB for ``index``, or :data:`MISSING_COLOR` if out of range."""
        if 0 <= index < len(self._colors):
            return self._colors[index]
        return MISSING_COLOR

    def copy(self) -> Palette:
        """An independent copy — the basis of every palette edit.

        Undo snapshots hold a :class:`Palette` by reference, so an editing
        command must never mutate the one it captured; it copies, edits the
        copy, and swaps that in.
        """
        return Palette(self._colors)

    def with_color(self, index: int, argb: int) -> Palette:
        """A copy with entry ``index`` set to ``argb``.

        Out-of-range indices are ignored rather than extending the palette:
        the grid only ever offers entries that exist, and silently growing a
        palette would change its byte length under the codec that writes it.
        """
        result = self.copy()
        if 0 <= index < len(result._colors):
            result._colors[index] = argb & 0xFFFFFFFF
        return result

    def resized(self, count: int) -> Palette:
        """A copy padded to ``count`` entries with the generated default tail.

        Growing a palette (Custom-from-default expands to
        :data:`FULL_PALETTE_COUNT`) keeps the existing colors and fills the new
        entries from the same deterministic generator :meth:`default` uses, so
        the added rows are distinguishable rather than a block of black.
        """
        if count <= len(self._colors):
            return Palette(self._colors[:count])
        colors = list(self._colors)
        colors.extend(_tail_color(i) for i in range(len(colors), count))
        return Palette(colors)

    @staticmethod
    def default(count: int) -> Palette:
        """The fallback for viewing pixels before a real palette is loaded:
        black, white, then contrasting colors. Deterministic in ``count``."""
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
