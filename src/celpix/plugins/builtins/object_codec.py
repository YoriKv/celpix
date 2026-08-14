"""The sprite-object cell codecs: subsprite records, and their frames.

A tilemap codec by protocol — bytes to a flat list of
:class:`~celpix.core.tilemap.Cell` and back — but the cells are **subsprites**
rather than grid positions, and the grid they would lay out in does not exist:
a subsprite carries a signed pixel offset, and those offsets are not tile-aligned
(:mod:`celpix.core.sprite`). So these engines have a second output the generic
packed engine has no need for, :meth:`frames`, which is what the view actually
draws. The cells stay the file's own records, in the file's own order, so a
write puts back exactly what was read.

Three records, all frames-of-subsprites, agreeing on nothing below that — which
is why they are three engines rather than one with a field table. The object
record (:class:`ObjectCodec`) wraps the console's own sprite attribute word; the
transfer record (:class:`ObzCodec`) spreads the same information across single
bytes, widens the character number to 12 bits and swaps X with Y
(``docs/graphics-formats-reference/scgcad-formats.md`` §9); the sprite-pattern
record (:class:`SprCodec`) comes from a different tool altogether, is 8 bytes
rather than 6, and is the one whose frames are *counted* rather than slotted
(``docs/graphics-formats-reference/ys-sprite-patterns.md``).

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

from celpix.core.context import (
    KEY_TILEMAP_ENDIAN,
    KEY_TILEMAP_FRAME_SIZES,
    KEY_TILEMAP_SUBSPRITES_PER_FRAME,
    PipelineContext,
)
from celpix.core.sprite import DEFAULT_SUBSPRITE_TILES, Frame, Subsprite
from celpix.core.tilemap import Cell
from celpix.plugins.formats import FormatInfo

OBJECT_FORMAT = "format.tilemap.scgcad-object"
OBZ_FORMAT = "format.tilemap.scgcad-obz"
SPR_FORMAT = "format.tilemap.ys-spr"

# What all three tell the **host**, and the whole of what a preset for them ever
# said that was not a codec parameter (:class:`~celpix.plugins.formats.FormatInfo`).
#
# `layout` says the cells are freely-placed subsprites grouped into frames rather
# than positions in a grid, which decides how the entry is read before a byte is
# decoded — so the bar can describe an object with nothing yet bound to it
# (`docs/design/tilemap-entry.md` §6).
#
# `palette_row_base` says a sprite's 3-bit palette field counts from CGRAM row 8,
# the console keeping OBJ palettes in the upper half (`snes-hardware-notes.md`
# §6); without it an object draws through the background's colours. It is the
# last word rather than the first — a bound bank that states its own base wins
# (`scgcad-formats.md` §8.5).
_SPRITE_DECLARES = {"layout": "sprite", "palette_row_base": 8}

RECORD = 6  # bytes per subsprite
SUBSPRITES_PER_FRAME = 64  # every frame has room for this many, used or not

_DRAWN = 0x80  # byte 0
_LARGE = 0x01


def _endian(default: str, ctx: PipelineContext) -> str:
    """Which way round the attribute word is, the file's answer preferred.

    The container reads the build marker and publishes what it found; ``default``
    is only what to assume when nothing did. A wrong order here does not
    degrade — it turns every tile number and palette row into a different one —
    so the file's own statement has to win.
    """
    order = str(ctx.get(KEY_TILEMAP_ENDIAN) or default)
    if order not in ("little", "big"):
        raise ValueError(f"endian must be 'little' or 'big', got {order!r}")
    return order


def size_pair(raw: object) -> tuple[int, int]:
    """The object's two subsprite sizes **in tiles**, which the file does not record.

    A game set this in a register, so a reader has to be told, and the corpus gives
    no way to recover it from the bytes. Two plain multiples of the tile size rather
    than a pick from an enumeration: the console's own list is six square pairs, but
    what a subsprite is built from is a square of *tiles*, and saying so leaves the
    quantity meaningful against any tile size and any pair the user needs.

    Non-positive or malformed values fall back to the default rather than
    raising — a hand-edited pair should draw something.
    """
    try:
        small, large = (int(raw[0]), int(raw[1]))
    except (TypeError, ValueError, IndexError, KeyError):
        return DEFAULT_SUBSPRITE_TILES
    small = small if small > 0 else DEFAULT_SUBSPRITE_TILES[0]
    large = large if large > 0 else DEFAULT_SUBSPRITE_TILES[1]
    return small, large


def subsprites_per_frame(default: int, ctx: PipelineContext | None = None) -> int:
    """How many subsprite slots one frame holds, the file's answer preferred.

    The same shape as :func:`_endian`, and for a sharper reason. A sprite object
    has **two forms with the same payload size** that divide it differently — the
    ordinary one into 32 frames of 64 slots, the extended one into 64 frames of
    **128** (``scgcad-formats.md`` §8.1, from the build's own ``eobjcnvX.c``) — so
    the stride cannot be derived from the byte count, and one preset serving both
    would mis-frame whichever it was not written for. Getting it wrong parses every
    record correctly and cuts the frames in the wrong places, which is why a
    byte-exact round trip never noticed.

    The container is what knows: deciding which form it is holding is how it found
    the signature. So it publishes, and ``default`` is only what to assume when
    nothing did.
    """
    stated = ctx.get(KEY_TILEMAP_SUBSPRITES_PER_FRAME) if ctx is not None else None
    if stated:
        return max(1, int(stated))
    return max(1, int(default))


class _SubspriteCodec:
    """What the subsprite records share: everything above the byte layout.

    All three group into frames and answer the same three questions about a cell
    the same way. Only :meth:`decode`, :meth:`encode` and ``_subsprite`` differ,
    which is the whole of the subclassing — plus, for the one format that counts
    its frames rather than slotting them, :meth:`frames`.

    All three are **formats** rather than engines: each is one record layout and
    not a parameterisation of anything (``docs/design/plugin-system.md``). What a
    subclass would have taken as parameters is a class attribute below, and what
    the *host* has to be told rides in :attr:`FormatInfo.declares`.
    """

    #: Fallback stride when no container states one, and the pair a record's size
    #: bit chooses between. Class attributes rather than preset parameters: they
    #: are this record layout's own answers, and the file overrides both where it
    #: can say (:func:`subsprites_per_frame`).
    per_frame = SUBSPRITES_PER_FRAME
    tile_pair = DEFAULT_SUBSPRITE_TILES

    def bytes_per_cell(self) -> int:
        return RECORD

    def cell_tiles(self) -> tuple[int, int]:
        # A subsprite covers 1, 4, 16 or 64 tiles depending on its own size bit,
        # so there is no one answer; the frame renderer asks each of them instead.
        # One is the honest reply to a question about cells in general.
        return (1, 1)

    def size_pair(self) -> tuple[int, int]:
        """The two sizes a subsprite's own size bit chooses between."""
        return size_pair(self.tile_pair)

    def frames(self, cells: list[Cell], ctx: PipelineContext) -> list[Frame]:
        """The cells regrouped into frames of subsprites — what the view draws.

        The extra half of these engines, and the reason they are not the generic
        packed one. Undrawn subsprites are dropped here rather than carried as
        invisible ones: they are the file's empty slots, all 94% of them, and
        nothing downstream would have anything to do with them.

        ``ctx`` carries the two things a *file* can settle about its own framing
        that a preset cannot. A format whose frames are **not** a fixed stride
        states their boundaries (:class:`SprCodec`); and a format with two forms
        that slot the same payload differently states the stride
        (:func:`subsprites_per_frame`), which is the sprite object's case and the
        one that cannot be worked out from the byte count.

        The size **pair** is resolved here rather than carried to the renderer:
        these records hold a size bit, and what a subsprite hands over is a
        shape (:class:`~celpix.core.sprite.Subsprite`). Which is also why a
        change of pair is a re-read and not a repaint.
        """
        per_frame = subsprites_per_frame(self.per_frame, ctx)
        pair = size_pair(self.tile_pair)
        return [
            tuple(
                sub
                for cell in cells[at : at + per_frame]
                if (sub := self._subsprite(cell, pair)) is not None
            )
            for at in range(0, len(cells), per_frame)
        ]

    @staticmethod
    def _subsprite(cell: Cell, pair: tuple[int, int]) -> Subsprite | None:
        raise NotImplementedError

    @staticmethod
    def _side(large: bool, pair: tuple[int, int]) -> int:
        """The square side, in tiles, the record's size bit picks out."""
        return max(1, pair[1] if large else pair[0])


class ObjectCodec(_SubspriteCodec):
    info = FormatInfo(
        id=OBJECT_FORMAT,
        name="Sprite object subsprite (OBJ/OBX)",
        category="Authoring tools",
        declares=_SPRITE_DECLARES,
    )

    #: Only the fallback: 26 of the corpus's 1,341 objects come from a later
    #: build that stores the attribute word the other way round, and the
    #: container reads that out of the file's own build marker (:func:`_endian`).
    endian = "big"

    def decode(self, data: bytes, ctx: PipelineContext) -> list[Cell]:
        order = _endian(self.endian, ctx)
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

    def encode(self, cells: list[Cell], ctx: PipelineContext) -> bytes:
        order = _endian(self.endian, ctx)
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
    def _subsprite(cell: Cell, pair: tuple[int, int]) -> Subsprite | None:
        """One decoded subsprite, or None when this slot says it is not drawn."""
        flags = cell.flags
        if not (flags >> 24) & _DRAWN:
            return None
        side = _SubspriteCodec._side(bool((flags >> 24) & _LARGE), pair)
        return Subsprite(
            x=_signed(flags & 0xFF),
            y=_signed((flags >> 8) & 0xFF),
            index=cell.index,
            palette_row=cell.palette_row,
            priority=cell.priority,
            flip_h=cell.flip_h,
            flip_v=cell.flip_v,
            across=side,
            down=side,
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

    info = FormatInfo(
        id=OBZ_FORMAT,
        name="Transfer object subsprite (OBZ)",
        category="Authoring tools",
        declares=_SPRITE_DECLARES,
    )

    def decode(self, data: bytes, ctx: PipelineContext) -> list[Cell]:
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

    def encode(self, cells: list[Cell], ctx: PipelineContext) -> bytes:
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
    def _subsprite(cell: Cell, pair: tuple[int, int]) -> Subsprite | None:
        flags = cell.flags
        if not (flags >> _OBZ_DRAWN) & 1:
            return None
        side = _SubspriteCodec._side(bool((flags >> _OBZ_LARGE) & 1), pair)
        return Subsprite(
            x=_signed((flags >> _OBZ_X) & 0xFF),
            y=_signed((flags >> _OBZ_Y) & 0xFF),
            index=cell.index,
            palette_row=cell.palette_row,
            priority=cell.priority,
            flip_h=cell.flip_h,
            flip_v=cell.flip_v,
            across=side,
            down=side,
            group=flags & 0xFF,
        )


# The sprite-pattern record, read as one 64-bit big-endian number for the reason
# the transfer record is — every bit the tool does not use travels in
# `Cell.flags` without being enumerated:
#
#   byte 0-1  X offset, signed 16-bit
#   byte 2-3  Y offset, signed 16-bit
#   byte 4-5  character number, 16 bits wide and 12 bits used
#   byte 6    the console's attribute bits as a loose byte, `vhoopppN`
#   byte 7    the size bit, stored already shifted up one
#
# The attribute byte lands one byte up from the bottom of the record, which is
# where the transfer record's is too, so its four fields sit at the same shifts.
SPR_RECORD = 8
_SPR_INDEX = 16  # 16 bits: bytes 4-5 whole
_SPR_PALETTE = 9
_SPR_PRIORITY = 12
_SPR_FLIP_H = 14
_SPR_FLIP_V = 15
_SPR_X = 48
_SPR_Y = 32
_SPR_LARGE = 1  # the size byte is `size << 1`, so the bit is one up
# Every bit celPix gives a Cell field to. The complement rides in `flags`: both
# offsets, the size byte, and **the attribute byte's bit 0**, which is the one
# that has to be carried rather than recomputed. The tool derives that bit from
# the character number's bit 8, and its own files disagree with it — 1,054 of the
# corpus's 7,078 records, nearly 40% of those written by the earliest build. A
# reader that recomputes it corrupts every one of them
# (``docs/graphics-formats-reference/ys-sprite-patterns.md`` §3).
_SPR_MODELLED = (0xFFFF << _SPR_INDEX) | (0x7 << _SPR_PALETTE) | (0xF << _SPR_PRIORITY)


class SprCodec(_SubspriteCodec):
    """The sprite-pattern record: 8 bytes, and frames the file *counts*.

    The third subsprite record and the one that is not a fixed grid of slots.
    Where the other two give every frame the same 64 slots and mark the used
    ones, this format writes a count byte ahead of each frame's records and has
    no drawn bit at all — a record that is present is drawn. So the frame
    boundaries are file structure rather than payload, and they arrive on the
    context after the container has taken them out
    (:data:`~celpix.core.context.KEY_TILEMAP_FRAME_SIZES`).

    Its offsets are a signed **16 bits** where an object's are one byte, and its
    character number 16 bits where an object's is nine — both wider than anything
    the corpus puts in them, and both carried at their full width so a write puts
    back what was read.
    """

    info = FormatInfo(
        id=SPR_FORMAT,
        name="Yoshi's Island sprite pattern subsprite (SPR)",
        category="Nintendo",
        declares=_SPRITE_DECLARES,
    )

    def bytes_per_cell(self) -> int:
        return SPR_RECORD

    def decode(self, data: bytes, ctx: PipelineContext) -> list[Cell]:
        cells: list[Cell] = []
        for at in range(0, len(data) - SPR_RECORD + 1, SPR_RECORD):
            word = int.from_bytes(data[at : at + SPR_RECORD], "big")
            cells.append(
                Cell(
                    index=(word >> _SPR_INDEX) & 0xFFFF,
                    palette_row=(word >> _SPR_PALETTE) & 0x7,
                    priority=(word >> _SPR_PRIORITY) & 0x3,
                    flip_h=bool((word >> _SPR_FLIP_H) & 1),
                    flip_v=bool((word >> _SPR_FLIP_V) & 1),
                    flags=word & ~_SPR_MODELLED,
                )
            )
        return cells

    def encode(self, cells: list[Cell], ctx: PipelineContext) -> bytes:
        out = bytearray()
        for cell in cells:
            word = (
                (cell.flags & ~_SPR_MODELLED)
                | ((cell.index & 0xFFFF) << _SPR_INDEX)
                | ((cell.palette_row & 0x7) << _SPR_PALETTE)
                | ((cell.priority & 0x3) << _SPR_PRIORITY)
                | ((1 << _SPR_FLIP_H) if cell.flip_h else 0)
                | ((1 << _SPR_FLIP_V) if cell.flip_v else 0)
            )
            out += word.to_bytes(SPR_RECORD, "big")
        return bytes(out)

    def index_limit(self) -> int | None:
        """The character number's own width — two whole bytes of it."""
        return 0xFFFF

    def frames(self, cells: list[Cell], ctx: PipelineContext) -> list[Frame]:
        """The cells cut into frames at the boundaries the file counted.

        The counts are the file's, so a frame here is however many subsprites the
        artist put in it — there are no empty slots to drop, which is why nothing
        below tests a drawn bit.

        **Falls back to fixed slots when nothing published counts**, which happens
        when this format is chosen by hand for bytes that came through another
        container: no reading of an unframed buffer is right, so it degrades to
        the one the other two sprite records use rather than inventing a third.
        A short count list stops early and a long one runs out of cells, both
        without raising: a truncated file should draw the frames it has.
        """
        sizes = ctx.get(KEY_TILEMAP_FRAME_SIZES)
        if not sizes:
            return super().frames(cells, ctx)
        pair = size_pair(self.tile_pair)
        out: list[Frame] = []
        at = 0
        for size in sizes:
            out.append(
                tuple(self._subsprite(cell, pair) for cell in cells[at : at + size])
            )
            at += size
        return out

    @staticmethod
    def _subsprite(cell: Cell, pair: tuple[int, int]) -> Subsprite:
        """One decoded subsprite — always one: the format has no undrawn slot.

        No group byte either, so that field of the model stays 0 rather than
        being fed a byte this record does not have.
        """
        flags = cell.flags
        side = _SubspriteCodec._side(bool((flags >> _SPR_LARGE) & 1), pair)
        return Subsprite(
            x=_signed16((flags >> _SPR_X) & 0xFFFF),
            y=_signed16((flags >> _SPR_Y) & 0xFFFF),
            index=cell.index,
            palette_row=cell.palette_row,
            priority=cell.priority,
            flip_h=cell.flip_h,
            flip_v=cell.flip_v,
            across=side,
            down=side,
        )


def _signed(value: int) -> int:
    """A byte offset as the signed number it is: subsprites sit around an origin."""
    return value - 0x100 if value > 0x7F else value


def _signed16(value: int) -> int:
    """The same for a format that spends two bytes on an offset it never fills."""
    return value - 0x10000 if value > 0x7FFF else value
