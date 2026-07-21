"""Indexed colour codec — a fixed hardware palette (NES, EGA, MSX).

Where the console's "colour" is really an index into fixed silicon colours, a
palette entry is one byte selecting an ARGB from a precomputed table
(``docs/graphics-formats-reference/implementation-guide.md`` §4, indexed palettes).
Decode is a table lookup; encode is **nearest entry by Manhattan RGB distance**
(the table has no inverse — several slots can share a colour). The table is the
preset's data (``colors`` = a list of ``0xRRGGBB``), so a new fixed palette is a
data file, not code.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.palette import MISSING_COLOR, Palette
from celpix.plugins.base import PluginInfo


class IndexedColorCodec:
    """Fixed-palette colour codec; the ARGB table comes from ``params``."""

    info = PluginInfo(
        id="codec.color-indexed",
        name="Indexed (fixed hardware palette) colour codec",
        stage=Stage.INTERPRET_PALETTE,
    )

    @staticmethod
    def _table(params: dict[str, Any]) -> list[int]:
        colors = params["colors"]
        if not colors:
            raise ValueError("indexed palette needs a non-empty 'colors' table")
        # Stored as 0xRRGGBB; render as opaque ARGB.
        return [0xFF000000 | (int(c) & 0xFFFFFF) for c in colors]

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> Palette:
        table = self._table(params)
        n = len(table)
        return Palette([table[b] if b < n else MISSING_COLOR for b in data])

    def encode(
        self, palette: Palette, params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        table = self._table(params)
        return bytes(self._nearest(table, argb) for argb in palette.colors)

    def bytes_per_entry(self, params: dict[str, Any]) -> int:
        # Always one byte in practice; the param keeps the preset self-contained.
        return int(params.get("bytes_per_entry", 1))

    @staticmethod
    def _nearest(table: list[int], argb: int) -> int:
        r, g, b = (argb >> 16) & 0xFF, (argb >> 8) & 0xFF, argb & 0xFF
        best_i, best_d = 0, None
        for i, c in enumerate(table):
            d = (
                abs(r - ((c >> 16) & 0xFF))
                + abs(g - ((c >> 8) & 0xFF))
                + abs(b - (c & 0xFF))
            )
            if best_d is None or d < best_d:
                best_i, best_d = i, d
        return best_i
