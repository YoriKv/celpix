"""Indexed color codec — a fixed hardware palette (NES, EGA, MSX).

Where a console's "color" is really an index into fixed silicon colors, a palette
entry is one byte selecting an ARGB from a precomputed table
(``docs/graphics-formats-reference/implementation-guide.md`` §4, indexed
palettes). Decode is a table lookup; encode is **nearest entry by Manhattan RGB
distance**, the table having no inverse since several slots can share a color.
The table is the preset's data (``colors``, a list of ``0xRRGGBB``), so a new
fixed palette is a data file.

**Shared entries.** ``shared_every = N`` says the hardware draws entry 0 in
place of every *N*-th entry — the NES draws colour 0 of every background row
from its one backdrop slot, and its first sprite slot *is* that slot
(``docs/rom-mapping/console-nes.md`` §3). Decode shows what the screen shows;
the stored bytes at those positions are real but never displayed, so they are
left exactly as they are. An edit to any of the tied entries lands on entry 0
(:meth:`IndexedColorCodec.shared_entries`), the one byte that decides them all.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.palette import MISSING_COLOR, Palette
from celpix.plugins.base import PluginInfo


class IndexedColorCodec:
    """Fixed-palette color codec; the ARGB table comes from ``params``."""

    info = PluginInfo(
        id="codec.palette.indexed",
        name="Indexed (fixed hardware palette) color codec",
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
        colors = [table[b] if b < n else MISSING_COLOR for b in data]
        every = self._shared_every(params)
        if every and colors:
            for i in range(every, len(colors), every):
                colors[i] = colors[0]
        return Palette(colors)

    def encode(
        self, palette: Palette, params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        table = self._table(params)
        return bytes(self._nearest(table, argb) for argb in palette.colors)

    def shared_entries(
        self, index: int, params: dict[str, Any], count: int
    ) -> tuple[int, ...]:
        """Every entry that shows the same colour as ``index``, owner first.

        ``(index,)`` for an ordinary entry. For one of the tied entries it is
        all of them, entry 0 first: the colour an edit gives one of them is
        what the screen shows at all of them, and entry 0 is the byte that says
        so. The stored bytes of the others are never written through here.
        """
        every = self._shared_every(params)
        if not every or index % every:
            return (index,)
        return tuple(range(0, max(count, index + 1), every))

    @staticmethod
    def _shared_every(params: dict[str, Any]) -> int:
        every = int(params.get("shared_every", 0) or 0)
        if every < 0:
            raise ValueError(f"shared_every must be 0 or positive, got {every}")
        return every

    def bytes_per_entry(self, params: dict[str, Any]) -> int:
        # Always one byte: an entry *is* an index into the master table, and both
        # directions walk the data a byte at a time. A preset declaring anything
        # else would be silently mis-read, so the size is not a parameter.
        return 1

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
