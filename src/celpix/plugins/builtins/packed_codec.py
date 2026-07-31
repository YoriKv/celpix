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

At ``bpp = 8`` a field *is* a whole byte, so the pixel never shares one and the
decode is a straight copy: that is the layout hardware documentation calls
**chunky** and tile editors call 8bpp linear, and it is this engine at its
degenerate depth rather than a format of its own (SNES Mode 7, Nintendo DS 2D,
the Super FX bitmap). Wider pixels carry *colour* rather than an index and belong
to :mod:`celpix.plugins.builtins.direct_color_codec`.

**Tile size is a parameter** (``tile_width``/``tile_height``, both 8 by default).
Unlike the planar engine — whose kernel hard-codes "pixel *x* is bit ``7 - x``" and
so is stuck at eight-pixel rows — nothing here is tied to a width: a packed row is
just ``width / pixels_per_byte`` bytes read left to right, and the field rule
inside a byte never mentions the tile. That covers the 16-, 24- and 32-wide packed
tiles common on arcade boards, which are otherwise the same format
(``docs/graphics-formats-reference/mame-formats.md`` §1.3).

This shares the planar engine's whole-buffer walk: a row byte sits at a fixed
offset inside every tile, so one strided slice collects it from all of them and a
256-entry table (:mod:`celpix.plugins.builtins._bits`) unpacks the lot.
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
        id="codec.pixel.packed",
        name="Packed (linear) codec",
        stage=Stage.INTERPRET_PIXEL,
    )

    # How many pixels a byte holds is a question about the *byte*, not the tile:
    # it is what bpp has to divide, and it is unrelated to the tile's own size.
    BITS_PER_BYTE = 8

    @classmethod
    def _geometry(
        cls, params: dict[str, Any]
    ) -> tuple[int, int, bool, bool, int, int, int, int, int]:
        bpp = int(params["bpp"])
        stride = int(params.get("nibble_stride", 0))
        width = int(params.get("tile_width", 8))
        height = int(params.get("tile_height", 8))
        if bpp <= 0:
            raise ValueError(f"packed bpp must be positive: got {bpp}")
        if width <= 0 or height <= 0:
            raise ValueError(f"packed tile size must be positive: {width}x{height}")
        if stride < 0:
            raise ValueError(f"nibble_stride cannot be negative: got {stride}")
        if stride and bpp % 2:
            raise ValueError(f"nibble_stride needs an even bpp to halve: got {bpp}")
        # A split index puts only *half* of itself in a byte field, so it is the
        # half — not bpp — that has to divide a byte. Unsplit, an 8bpp field fills
        # the byte exactly, which is the one-byte-per-pixel ("chunky") layout.
        field_bpp = bpp // 2 if stride else bpp
        if cls.BITS_PER_BYTE % field_bpp != 0:
            raise ValueError(
                f"packed field width must divide {cls.BITS_PER_BYTE}: got {field_bpp}"
            )
        pixels_per_byte = cls.BITS_PER_BYTE // field_bpp
        # A row is whole bytes, so the width has to be a whole number of them.
        # Every other size follows from that, tile_bytes included.
        if width % pixels_per_byte:
            raise ValueError(
                f"packed tile_width must be a multiple of the {pixels_per_byte} "
                f"pixels a byte holds at {bpp}bpp: got {width}"
            )
        msb_first = bool(params.get("msb_first", False))
        reverse = bool(params.get("reverse_bytes", False))
        tile_bytes = width * height * bpp // cls.BITS_PER_BYTE
        # Half the tile is the high-half stream; runs have to divide it evenly or
        # the last run would straddle the tile boundary.
        if stride and (tile_bytes // 2) % stride != 0:
            raise ValueError(
                f"nibble_stride must divide the half-tile "
                f"({tile_bytes // 2} bytes): got {stride}"
            )
        return (
            bpp,
            field_bpp,
            msb_first,
            reverse,
            pixels_per_byte,
            stride,
            tile_bytes,
            width,
            height,
        )

    @staticmethod
    def _pair_offset(index: int, stride: int) -> int:
        """Byte offset of half-index field ``index``; its partner is ``stride`` on.

        The two half-index streams interleave in runs of ``stride`` bytes, so run
        ``index // stride`` starts at twice its own length into the tile.
        """
        return (index // stride) * 2 * stride + index % stride

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        return self._geometry(params)[6]

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        _bpp, _fb, _msb, _rev, _ppb, _stride, _tb, width, height = self._geometry(
            params
        )
        return width, height

    @staticmethod
    def _row_byte_order(width: int, ppb: int, reverse: bool) -> list[int]:
        """Which row byte supplies each left-to-right group of ``ppb`` pixels.

        Plain order, or right-to-left under ``reverse_bytes``.
        """
        bytes_per_row = width // ppb
        return [(bytes_per_row - 1 - i) if reverse else i for i in range(bytes_per_row)]

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]:
        """Unpack every tile's row bytes at once, one pixel row at a time."""
        (
            _bpp,
            field_bpp,
            msb_first,
            reverse,
            ppb,
            stride,
            tile_bytes,
            width,
            height,
        ) = self._geometry(params)
        require_whole_tiles(len(data), tile_bytes)
        if not data:
            return []
        fields_per_row = width // ppb
        table = field_expansion(ppb, field_bpp, msb_first)
        high = field_expansion(ppb, field_bpp, msb_first, field_bpp) if stride else ()
        order = self._row_byte_order(width, ppb, reverse)
        count = len(data) // tile_bytes
        rows = []
        for y in range(height):
            # Row y of every tile, back to back. Each source byte unpacks to its
            # ppb pixels for all tiles at once; the strided writes interleave
            # those groups back into pixel order.
            row_buf = bytearray(count * width)
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
                    row_buf[i * ppb + j :: width] = chunk[j::ppb]
            rows.append(bytes(row_buf))
        return tiles_from_rows(rows, width, height, count)

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        """The inverse: pack each row byte across every tile in one pass."""
        (
            _bpp,
            field_bpp,
            msb_first,
            reverse,
            ppb,
            stride,
            tile_bytes,
            width,
            height,
        ) = self._geometry(params)
        pixels = flatten_tiles(tiles, width, height)
        out = bytearray(len(tiles) * tile_bytes)
        if not tiles:
            return bytes(out)
        fields_per_row = width // ppb
        order = self._row_byte_order(width, ppb, reverse)
        tile_pixels = width * height  # one tile's pixels
        # A split index has to be masked into halves; an unsplit one has no other
        # half to keep out, so it keeps the overflow into the next field.
        field_mask = (1 << field_bpp) - 1 if stride else 0xFF

        def pack(y: int, i: int, source_shift: int) -> bytes:
            """Row ``y``'s field ``i``, for every tile — the bits above
            ``source_shift`` of each index it covers."""
            return or_all(
                [
                    pixels[y * width + i * ppb + j :: tile_pixels].translate(
                        field_packing(
                            j, ppb, field_bpp, msb_first, source_shift, field_mask
                        )
                    )
                    for j in range(ppb)
                ]
            )

        for y in range(height):
            for i, fi in enumerate(order):
                index = y * fields_per_row + fi
                if stride:
                    off = self._pair_offset(index, stride)
                    out[off::tile_bytes] = pack(y, i, field_bpp)
                    out[off + stride :: tile_bytes] = pack(y, i, 0)
                else:
                    out[index::tile_bytes] = pack(y, i, 0)
        return bytes(out)
