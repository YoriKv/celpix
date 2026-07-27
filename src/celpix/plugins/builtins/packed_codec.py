"""Data-driven packed (linear) pixel codec — one kernel, order flags are parameters.

In a packed format each pixel index is a sub-byte **field** stored directly, with
no planes: an 8-pixel row is ``8 / pixels_per_byte`` bytes, each holding
``pixels_per_byte`` adjacent pixels. The kernel walks pixels left-to-right and two
per-format knobs place each field
(``docs/graphics-formats-reference/implementation-guide.md`` §2, "Packed / linear"):

- **``msb_first``** — is pixel 0 of a byte in its **high** field or its low one?
  High: Genesis/MSX 4bpp (high nibble left), Neo Geo Pocket 2bpp. Low: GBA 4bpp,
  Virtual Boy 2bpp.
- **``reverse_bytes``** — read the row's bytes right-to-left, the Neo Geo Pocket
  byte order in which the odd byte drives the left pixels.

So GBA, Genesis/X68000/MSX 4bpp, Virtual Boy and both Neo Geo Pocket orderings are
each a two-flag parameter set.

A third knob widens the family past one byte per group of pixels:

- **``nibble_stride``** — the index is **split in half across two bytes**, the
  first supplying its high ``bpp / 2`` bits and the byte ``nibble_stride`` further
  on the low ones. The halves are two streams interleaved in runs of
  ``nibble_stride`` bytes, so ``1`` alternates them byte by byte — what a two-part
  region join (:mod:`celpix.plugins.builtins.split_planes`) leaves behind when a
  board wires each half-index to its own ROM — while a stride of half the tile puts
  every high-half byte before every low-half one. ``0``, the default, is off.

  Each byte still packs ``8 / (bpp / 2)`` pixels' worth of *its* half, so tile size
  is unchanged and only the source byte of a pixel's bits moves. The other two
  flags keep their meaning, applying to the half-index fields.

Like the planar engine this is the 8-pixel-wide case with a fixed 8×8 tile, and it
shares that engine's whole-buffer walk: a row byte sits at a fixed offset inside
every tile, so one strided slice collects it from all of them and a 256-entry table
(:mod:`celpix.plugins.builtins._bits`) unpacks the lot.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.index_grid import IndexGrid
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._bits import (
    field_expansion,
    field_packing,
    or_all,
    or_bytes,
)
from celpix.plugins.builtins._tile import (
    flatten_tiles,
    require_whole_tiles,
    tiles_from_rows,
)


class PackedCodec:
    """Generic packed tile codec; behaviour comes entirely from ``params``."""

    info = PluginInfo(
        id="codec.packed",
        name="Packed (linear) codec",
        stage=Stage.INTERPRET_PIXEL,
    )

    TILE = 8  # the kernel's per-row layout is specific to 8-pixel rows (fixed 8×8)
    # How many pixels a byte holds is a question about the *byte*, not the tile:
    # the two 8s are unrelated, and only this one is what bpp has to divide.
    BITS_PER_BYTE = 8

    @classmethod
    def _geometry(
        cls, params: dict[str, Any]
    ) -> tuple[int, int, bool, bool, int, int, int]:
        bpp = int(params["bpp"])
        stride = int(params.get("nibble_stride", 0))
        if bpp <= 0:
            raise ValueError(f"packed bpp must be positive: got {bpp}")
        if stride < 0:
            raise ValueError(f"nibble_stride cannot be negative: got {stride}")
        if stride and bpp % 2:
            raise ValueError(f"nibble_stride needs an even bpp to halve: got {bpp}")
        # A split index puts only *half* of itself in a byte field, so it is the
        # half — not bpp — that has to divide a byte. That is what makes 8bpp a
        # packed format here: two nibble fields rather than one 8-bit one.
        field_bpp = bpp // 2 if stride else bpp
        if cls.BITS_PER_BYTE % field_bpp != 0:
            raise ValueError(
                f"packed field width must divide {cls.BITS_PER_BYTE}: got {field_bpp}"
            )
        msb_first = bool(params.get("msb_first", False))
        reverse = bool(params.get("reverse_bytes", False))
        pixels_per_byte = cls.BITS_PER_BYTE // field_bpp
        tile_bytes = cls.TILE * cls.TILE * bpp // cls.BITS_PER_BYTE
        # Half the tile is the high-half stream; runs have to divide it evenly or
        # the last run would straddle the tile boundary.
        if stride and (tile_bytes // 2) % stride != 0:
            raise ValueError(
                f"nibble_stride must divide the half-tile "
                f"({tile_bytes // 2} bytes): got {stride}"
            )
        return bpp, field_bpp, msb_first, reverse, pixels_per_byte, stride, tile_bytes

    @staticmethod
    def _pair_offset(index: int, stride: int) -> int:
        """Byte offset of half-index field ``index``; its partner is ``stride`` on.

        The two half-index streams interleave in runs of ``stride`` bytes, so run
        ``index // stride`` starts at twice its own length into the tile.
        """
        return (index // stride) * 2 * stride + index % stride

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        *_rest, tile_bytes = self._geometry(params)
        return tile_bytes

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        return self.TILE, self.TILE

    @classmethod
    def _row_byte_order(cls, ppb: int, reverse: bool) -> list[int]:
        """Which row byte supplies each left-to-right group of ``ppb`` pixels.

        Plain order, or right-to-left under ``reverse_bytes``.
        """
        bytes_per_row = cls.TILE // ppb
        return [(bytes_per_row - 1 - i) if reverse else i for i in range(bytes_per_row)]

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]:
        """Unpack every tile's row bytes at once, one pixel row at a time."""
        _bpp, field_bpp, msb_first, reverse, ppb, stride, tile_bytes = self._geometry(
            params
        )
        tile = self.TILE
        require_whole_tiles(len(data), tile_bytes)
        if not data:
            return []
        fields_per_row = tile // ppb
        table = field_expansion(ppb, field_bpp, msb_first)
        high = field_expansion(ppb, field_bpp, msb_first, field_bpp) if stride else ()
        order = self._row_byte_order(ppb, reverse)
        count = len(data) // tile_bytes
        rows = []
        for y in range(tile):
            # Row y of every tile, back to back. Each source byte unpacks to its
            # ppb pixels for all tiles at once; the strided writes interleave
            # those groups back into pixel order.
            row_buf = bytearray(count * tile)
            for i, fi in enumerate(order):
                index = y * fields_per_row + fi
                if stride:
                    off = self._pair_offset(index, stride)
                    chunk = or_bytes(
                        b"".join(map(high.__getitem__, data[off::tile_bytes])),
                        b"".join(
                            map(table.__getitem__, data[off + stride :: tile_bytes])
                        ),
                    )
                else:
                    chunk = b"".join(map(table.__getitem__, data[index::tile_bytes]))
                for j in range(ppb):
                    row_buf[i * ppb + j :: tile] = chunk[j::ppb]
            rows.append(bytes(row_buf))
        return tiles_from_rows(rows, tile, tile, count)

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        """The inverse: pack each row byte across every tile in one pass."""
        _bpp, field_bpp, msb_first, reverse, ppb, stride, tile_bytes = self._geometry(
            params
        )
        tile = self.TILE
        pixels = flatten_tiles(tiles, tile, tile)
        out = bytearray(len(tiles) * tile_bytes)
        if not tiles:
            return bytes(out)
        fields_per_row = tile // ppb
        order = self._row_byte_order(ppb, reverse)
        tile_pixels = tile * tile  # one tile's pixels
        # A split index has to be masked into halves; an unsplit one has no other
        # half to keep out, so it keeps the overflow into the next field.
        field_mask = (1 << field_bpp) - 1 if stride else 0xFF

        def pack(y: int, i: int, source_shift: int) -> bytes:
            """Row ``y``'s field ``i``, for every tile — the bits above
            ``source_shift`` of each index it covers."""
            return or_all(
                [
                    pixels[y * tile + i * ppb + j :: tile_pixels].translate(
                        field_packing(
                            j, ppb, field_bpp, msb_first, source_shift, field_mask
                        )
                    )
                    for j in range(ppb)
                ]
            )

        for y in range(tile):
            for i, fi in enumerate(order):
                index = y * fields_per_row + fi
                if stride:
                    off = self._pair_offset(index, stride)
                    out[off::tile_bytes] = pack(y, i, field_bpp)
                    out[off + stride :: tile_bytes] = pack(y, i, 0)
                else:
                    out[index::tile_bytes] = pack(y, i, 0)
        return bytes(out)
