"""The sprite-object cell codecs: 6-byte subsprite records, and their frames.

A tilemap codec by protocol — bytes to a flat list of
:class:`~celpix.core.tilemap.Cell` and back — but the cells are **subsprites**
rather than grid positions, and the grid they would lay out in does not exist:
a subsprite carries a signed pixel offset, and those offsets are not tile-aligned
(:mod:`celpix.core.sprite`). So these engines have a second output the generic
packed engine has no need for, :meth:`frames`, which is what the view actually
draws. The cells stay the file's own records, in the file's own order, so a
write puts back exactly what was read.

Two records, both 6 bytes and both frames-of-subsprites, but they agree on nothing
below that — which is why they are two engines rather than one with a field
table. The object record (:class:`ObjectCodec`) wraps the console's own sprite
attribute word; the transfer record (:class:`ObzCodec`) spreads the same
information across single bytes, widens the character number to 12 bits and
swaps X with Y (``docs/graphics-formats-reference/scgcad-formats.md`` §9).

The object record, 6 bytes and hardware-shaped:

===== =========================================================
byte  meaning
===== =========================================================
0     bit 7 this subsprite is drawn, bit 0 which of the two sizes
1     the authoring tool's group byte — carried, not interpreted
2     Y offset, signed
3     X offset, signed
4-5   the console's own sprite attribute word: ``vhoocccT``
      ``TTTTTTTT`` — flips, priority, palette row, 9-bit tile
===== =========================================================

Byte 4-5 is **big-endian** in the common build and little-endian in the later one
marked ``F``, which is why the order is a preset parameter here as it is for a
tilemap cell (``docs/graphics-formats-reference/scgcad-formats.md`` §8). The four
bytes celPix has no field for ride in ``Cell.flags``, uninterpreted and intact,
and :meth:`frames` reads the geometry back out of them — which is why the layout
is stated once, here, rather than split between a preset table and a resolver.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import KEY_TILEMAP_ENDIAN, PipelineContext
from celpix.core.errors import Stage
from celpix.core.sprite import DEFAULT_SUBSPRITE_TILES, Frame, Subsprite
from celpix.core.tilemap import Cell
from celpix.plugins.base import PluginInfo

OBJECT_ENGINE = "codec.scgcad-object"
OBZ_ENGINE = "codec.scgcad-obz"

RECORD = 6  # bytes per subsprite
SUBSPRITES_PER_FRAME = 64  # every frame has room for this many, used or not

_DRAWN = 0x80  # byte 0
_LARGE = 0x01


def _endian(params: dict[str, Any], ctx: PipelineContext) -> str:
    """Which way round the attribute word is, the file's answer preferred.

    The container reads the build marker and publishes what it found; the preset
    only says what to assume when nothing did. A wrong order here does not
    degrade — it turns every tile number and palette row into a different one —
    so the file's own statement has to win.
    """
    order = str(ctx.get(KEY_TILEMAP_ENDIAN) or params.get("endian", "big"))
    if order not in ("little", "big"):
        raise ValueError(f"endian must be 'little' or 'big', got {order!r}")
    return order


def size_pair(params: dict[str, Any]) -> tuple[int, int]:
    """The object's two subsprite sizes **in tiles**, which the file does not record.

    A game set this in a register, so a reader has to be told, and the corpus gives
    no way to recover it from the bytes. Two plain multiples of the tile size rather
    than a pick from an enumeration: the console's own list is six square pairs, but
    what a subsprite is built from is a square of *tiles*, and saying so leaves the
    quantity meaningful against any tile size and any pair the user needs.

    Non-positive or missing values fall back to the default rather than raising — a
    hand-edited preset should draw something.
    """
    raw = params.get("subsprite_tiles", DEFAULT_SUBSPRITE_TILES)
    try:
        small, large = (int(raw[0]), int(raw[1]))
    except (TypeError, ValueError, IndexError, KeyError):
        return DEFAULT_SUBSPRITE_TILES
    small = small if small > 0 else DEFAULT_SUBSPRITE_TILES[0]
    large = large if large > 0 else DEFAULT_SUBSPRITE_TILES[1]
    return small, large


def subsprites_per_frame(params: dict[str, Any]) -> int:
    return max(1, int(params.get("subsprites_per_frame", SUBSPRITES_PER_FRAME)))


class _SubspriteCodec:
    """What the two subsprite records share: everything above the byte layout.

    Both are 6 bytes, both group into frames of a fixed size, and both answer the
    same three questions about a cell the same way. Only :meth:`decode`,
    :meth:`encode` and ``_subsprite`` differ, which is the whole of the subclassing.
    """

    def bytes_per_cell(self, params: dict[str, Any]) -> int:
        return RECORD

    def cell_tiles(self, params: dict[str, Any]) -> tuple[int, int]:
        # A subsprite covers 1, 4, 16 or 64 tiles depending on its own size bit,
        # so there is no one answer; the frame renderer asks each of them instead.
        # One is the honest reply to a question about cells in general.
        return (1, 1)

    def size_pair(self, params: dict[str, Any]) -> tuple[int, int]:
        """The two sizes a subsprite's own size bit chooses between."""
        return size_pair(params)

    def frames(self, cells: list[Cell], params: dict[str, Any]) -> list[Frame]:
        """The cells regrouped into frames of subsprites — what the view draws.

        The extra half of these engines, and the reason they are not the generic
        packed one. Undrawn subsprites are dropped here rather than carried as
        invisible ones: they are the file's empty slots, all 94% of them, and
        nothing downstream would have anything to do with them.
        """
        per_frame = subsprites_per_frame(params)
        return [
            tuple(
                sub
                for cell in cells[at : at + per_frame]
                if (sub := self._subsprite(cell)) is not None
            )
            for at in range(0, len(cells), per_frame)
        ]

    @staticmethod
    def _subsprite(cell: Cell) -> Subsprite | None:
        raise NotImplementedError


class ObjectCodec(_SubspriteCodec):
    info = PluginInfo(
        id=OBJECT_ENGINE,
        name="Sprite object subsprite",
        stage=Stage.INTERPRET_TILEMAP,
    )

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[Cell]:
        order = _endian(params, ctx)
        cells: list[Cell] = []
        for at in range(0, len(data) - RECORD + 1, RECORD):
            record = data[at : at + RECORD]
            attr = int.from_bytes(record[4:6], order)
            cells.append(
                Cell(
                    index=attr & 0x1FF,
                    palette_row=(attr >> 9) & 0x7,
                    priority=(attr >> 12) & 0x3,
                    flip_h=bool(attr & 0x4000),
                    flip_v=bool(attr & 0x8000),
                    flags=int.from_bytes(record[:4], "big"),
                )
            )
        return cells

    def encode(
        self, cells: list[Cell], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        order = _endian(params, ctx)
        out = bytearray()
        for cell in cells:
            attr = (
                (cell.index & 0x1FF)
                | ((cell.palette_row & 0x7) << 9)
                | ((cell.priority & 0x3) << 12)
                | (0x4000 if cell.flip_h else 0)
                | (0x8000 if cell.flip_v else 0)
            )
            out += (cell.flags & 0xFFFFFFFF).to_bytes(4, "big")
            out += attr.to_bytes(2, order)
        return bytes(out)

    @staticmethod
    def _subsprite(cell: Cell) -> Subsprite | None:
        """One decoded subsprite, or None when this slot says it is not drawn."""
        flags = cell.flags
        if not (flags >> 24) & _DRAWN:
            return None
        return Subsprite(
            x=_signed(flags & 0xFF),
            y=_signed((flags >> 8) & 0xFF),
            index=cell.index,
            palette_row=cell.palette_row,
            priority=cell.priority,
            flip_h=cell.flip_h,
            flip_v=cell.flip_v,
            large=bool((flags >> 24) & _LARGE),
            group=(flags >> 16) & 0xFF,
        )


# The transfer record, read as one 48-bit big-endian number so that every bit the
# tool does not use travels in `Cell.flags` without being enumerated:
#
#   byte 0    bit 7 drawn, bit 6 which of the two sizes, bits 0-3 character high
#   byte 1    character low byte - the pair make a 12-bit number, not the
#             object record's 9-bit one
#   byte 2    X offset, signed   <- the object record has Y here
#   byte 3    Y offset, signed
#   byte 4    the attribute byte unpacked from the console's word: `vhoocccx`
#   byte 5    the tool's group byte
#
# Bit shifts into that 48-bit number rather than per-byte masks, so decode and
# encode state each field's position once.
_OBZ_INDEX = 32  # 12 bits, spanning byte 0's low nibble and all of byte 1
_OBZ_PALETTE = 9
_OBZ_PRIORITY = 12
_OBZ_FLIP_H = 14
_OBZ_FLIP_V = 15
_OBZ_DRAWN = 47
_OBZ_LARGE = 46
_OBZ_X = 24
_OBZ_Y = 16
# Every bit celPix gives a Cell field to. The complement is what rides in
# `flags`: the drawn and size bits, both offsets, the group byte, and the
# attribute byte's unused bit 0 — the character bit an on-console sprite word
# keeps there, which this record has no use for because its character number is
# already 12 bits wide. Set in none of the corpus's 606,208 slots, and carried
# rather than assumed anyway.
_OBZ_MODELLED = (0xFFF << _OBZ_INDEX) | (0x7 << _OBZ_PALETTE) | (0xF << _OBZ_PRIORITY)


class ObzCodec(_SubspriteCodec):
    """The transfer container's subsprite record — the same idea, none of the bits.

    A separate engine rather than a parameterised :class:`ObjectCodec` because
    nothing survives the change: the character number is a different width in
    different bytes, the attributes are a loose byte rather than the console's
    word, and X and Y are the other way round. What the two do share is the
    frame grouping, which is in :class:`_SubspriteCodec`.
    """

    info = PluginInfo(
        id=OBZ_ENGINE,
        name="Sprite object subsprite (transfer)",
        stage=Stage.INTERPRET_TILEMAP,
    )

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[Cell]:
        cells: list[Cell] = []
        for at in range(0, len(data) - RECORD + 1, RECORD):
            word = int.from_bytes(data[at : at + RECORD], "big")
            cells.append(
                Cell(
                    index=(word >> _OBZ_INDEX) & 0xFFF,
                    palette_row=(word >> _OBZ_PALETTE) & 0x7,
                    priority=(word >> _OBZ_PRIORITY) & 0x3,
                    flip_h=bool((word >> _OBZ_FLIP_H) & 1),
                    flip_v=bool((word >> _OBZ_FLIP_V) & 1),
                    flags=word & ~_OBZ_MODELLED,
                )
            )
        return cells

    def encode(
        self, cells: list[Cell], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        out = bytearray()
        for cell in cells:
            word = (
                (cell.flags & ~_OBZ_MODELLED)
                | ((cell.index & 0xFFF) << _OBZ_INDEX)
                | ((cell.palette_row & 0x7) << _OBZ_PALETTE)
                | ((cell.priority & 0x3) << _OBZ_PRIORITY)
                | ((1 << _OBZ_FLIP_H) if cell.flip_h else 0)
                | ((1 << _OBZ_FLIP_V) if cell.flip_v else 0)
            )
            out += word.to_bytes(RECORD, "big")
        return bytes(out)

    @staticmethod
    def _subsprite(cell: Cell) -> Subsprite | None:
        flags = cell.flags
        if not (flags >> _OBZ_DRAWN) & 1:
            return None
        return Subsprite(
            x=_signed((flags >> _OBZ_X) & 0xFF),
            y=_signed((flags >> _OBZ_Y) & 0xFF),
            index=cell.index,
            palette_row=cell.palette_row,
            priority=cell.priority,
            flip_h=cell.flip_h,
            flip_v=cell.flip_v,
            large=bool((flags >> _OBZ_LARGE) & 1),
            group=flags & 0xFF,
        )


def _signed(value: int) -> int:
    """A byte offset as the signed number it is: subsprites sit around an origin."""
    return value - 0x100 if value > 0x7F else value
