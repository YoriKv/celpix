"""Data-driven packed (linear) pixel codec — one kernel, order flags are parameters.

In a packed format each pixel index is a sub-byte **field** stored directly (no
planes): an 8-pixel row is `8 / pixels_per_byte` bytes, each byte holding
`pixels_per_byte` adjacent pixels. The universal kernel walks pixels left-to-right;
two per-format knobs place each field
(``docs/graphics-formats-reference/implementation-guide.md`` §2, "Packed / linear"):

- **``msb_first``** — is pixel 0 of a byte in its **high** field or its low field?
  High → GBA's opposite (Genesis/MSX 4bpp high-nibble-left, NGP 2bpp high-bits-first);
  low → GBA 4bpp (low-nibble-left), Virtual Boy 2bpp (low-bits-first).
- **``reverse_bytes``** — read the row's bytes right-to-left. Covers the YY-CHR
  Neo Geo Pocket byte-swap (odd byte drives the left pixels).

So GBA, Genesis/X68000/MSX 4bpp, Virtual Boy, and both Neo Geo Pocket orderings are
each a two-flag parameter set, not code.

Like the planar engine this handles the 8-pixel-wide case (fixed 8×8 tile); wider or
odd-width packed tiles are a later bespoke codec with their own walk. It shares that
engine's whole-buffer walk too: a row byte sits at a fixed offset inside every tile,
so one strided slice collects it from all of them and a 256-entry table
(:mod:`celpix.plugins.builtins._bits`) unpacks the lot.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.index_grid import IndexGrid
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._bits import field_expansion, field_packing, merge_planes
from celpix.plugins.builtins._tile import check_tile_size, require_whole_tiles


class PackedCodec:
    """Generic packed tile codec; behaviour comes entirely from ``params``."""

    info = PluginInfo(
        id="codec.packed",
        name="Packed (linear) codec",
        stage=Stage.INTERPRET_PIXEL,
    )

    TILE = 8  # the kernel's per-row layout is specific to 8-pixel rows (fixed 8×8)

    @classmethod
    def _geometry(cls, params: dict[str, Any]) -> tuple[int, bool, bool, int, int]:
        bpp = int(params["bpp"])
        if bpp <= 0 or cls.TILE % bpp != 0:
            raise ValueError(f"packed bpp must divide {cls.TILE}: got {bpp}")
        msb_first = bool(params.get("msb_first", False))
        reverse = bool(params.get("reverse_bytes", False))
        pixels_per_byte = cls.TILE // bpp
        tile_bytes = cls.TILE * cls.TILE * bpp // 8
        return bpp, msb_first, reverse, pixels_per_byte, tile_bytes

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        return self._geometry(params)[4]

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        return self.TILE, self.TILE

    @classmethod
    def _row_bytes(cls, ppb: int, reverse: bool) -> list[int]:
        """Which row byte supplies each left-to-right group of ``ppb`` pixels.

        Plain order, or right-to-left under ``reverse_bytes`` — the Neo Geo Pocket
        byte-swap, where the odd byte drives the left pixels.
        """
        count = cls.TILE // ppb
        return [(count - 1 - i) if reverse else i for i in range(count)]

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]:
        """Unpack every tile's row bytes at once, one pixel row at a time."""
        bpp, msb_first, reverse, ppb, tile_bytes = self._geometry(params)
        tile = self.TILE
        require_whole_tiles(len(data), tile_bytes)
        if not data:
            return []
        bytes_per_row = tile // ppb
        table = field_expansion(ppb, bpp, msb_first)
        order = self._row_bytes(ppb, reverse)
        count = len(data) // tile_bytes
        rows = []
        for y in range(tile):
            # Row y of every tile, back to back. Each source byte unpacks to its
            # ppb pixels for all tiles at once; the strided writes interleave
            # those groups back into pixel order.
            row_buf = bytearray(count * tile)
            for i, bi in enumerate(order):
                chunk = b"".join(
                    map(table.__getitem__, data[y * bytes_per_row + bi :: tile_bytes])
                )
                for j in range(ppb):
                    row_buf[i * ppb + j :: tile] = chunk[j::ppb]
            rows.append(bytes(row_buf))
        return [
            IndexGrid(tile, tile, b"".join(row[base : base + tile] for row in rows))
            for base in range(0, count * tile, tile)
        ]

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        """The inverse: pack each row byte across every tile in one pass."""
        bpp, msb_first, reverse, ppb, tile_bytes = self._geometry(params)
        tile = self.TILE
        for t, grid in enumerate(tiles):
            check_tile_size(grid, tile, tile, t)
        out = bytearray(len(tiles) * tile_bytes)
        if not tiles:
            return bytes(out)
        bytes_per_row = tile // ppb
        order = self._row_bytes(ppb, reverse)
        pixels = b"".join(bytes(grid.data) for grid in tiles)
        stride = tile * tile  # one tile's pixels
        for y in range(tile):
            for i, bi in enumerate(order):
                out[y * bytes_per_row + bi :: tile_bytes] = merge_planes(
                    [
                        pixels[y * tile + i * ppb + j :: stride].translate(
                            field_packing(j, ppb, bpp, msb_first)
                        )
                        for j in range(ppb)
                    ]
                )
        return bytes(out)
