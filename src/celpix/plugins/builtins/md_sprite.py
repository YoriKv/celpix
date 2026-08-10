"""The Mega Drive sprite-mappings record: a rectangle of tiles at a pixel offset.

The fourth sprite-object codec, and the first whose subsprites are **not square**.
A Mega Drive sprite carries the VDP's own size nibble, ``(w - 1) << 2 | (h - 1)``,
so one record covers anything from one tile to a 4x4 block — and its tiles run
**down each column** before starting the next, which is the opposite of the
row-major walk every grid format takes. Those two facts are what put this in code
rather than in a parameter table for the packed engine
(``docs/design/tilemap-entry.md`` §6).

Everything else about the record is the console's:

=====  =====================================================================
byte   meaning
=====  =====================================================================
0      Y offset from the object's origin, **signed**
1      size: ``(w - 1) << 2 | (h - 1)``
2-3    the sprite attribute word — 11-bit tile, h-flip, v-flip, 2-bit palette
       row, priority. The same field layout the ``md-bg`` cell uses
4..    X offset, **signed**: one byte, or two under ``x_bytes = 2``
last   the X offset of the **mirrored** frame, when ``mirror_x`` is set
=====  =====================================================================

The mirror byte is the field worth knowing about, because it is what tells the
two six-byte members of this family apart: a record with a byte X *and* a mirror
byte is the same length as one with a signed word X, and reading either as the
other scatters the pieces across the screen rather than failing. Where it is
present it is redundant — always ``-(x + w * 8)`` — so a reader can check it, and
:func:`mirrors_x` is that check: a run that fails it is probably the other
member, and :meth:`MdSpriteCodec.frames` says so rather than drawing a plausible
object with its pieces in the wrong places.

**The run is headed by a count in the files seen so far, and this codec does not
read it.** A count is structure rather than payload: a buffer that kept it would
have no fixed cell stride, and every offset after it would be wrong by the header
(the same reason ``ys-spr``'s container lifts its counts out). So a slice starts
past the header and its own length says how many records there are.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.notices import warn
from celpix.core.sprite import Frame, Subsprite
from celpix.core.tilemap import Cell, CellOp
from celpix.plugins.base import PluginInfo

MD_SPRITE_ENGINE = "codec.tilemap.md-sprite"

_INDEX = 0x7FF
_FLIP_H = 0x0800
_FLIP_V = 0x1000
_ROW = 0x6000
_ROW_SHIFT = 13
_PRIORITY = 0x8000

_FIXED = 4  # y, size, and the two-byte attribute word


def _signed(value: int, width: int) -> int:
    """``value`` read as a signed integer ``width`` bytes wide."""
    limit = 1 << (width * 8)
    return value - limit if value >= limit // 2 else value


def _layout(params: dict[str, Any]) -> tuple[int, bool, str]:
    """``(x width in bytes, is there a mirror byte, attribute byte order)``.

    The three things that vary across the family, all defaulted to the form that
    has been read out of a real file. Out-of-range values fall back rather than
    raising — a hand-edited preset should still draw something.
    """
    x_bytes = 2 if params.get("x_bytes") == 2 else 1
    mirror = bool(params.get("mirror_x", True))
    order = params.get("endian", "big")
    return x_bytes, mirror, "little" if order == "little" else "big"


def record_size(params: dict[str, Any]) -> int:
    """How many bytes one record takes under ``params``."""
    x_bytes, mirror, _order = _layout(params)
    return _FIXED + x_bytes + (1 if mirror else 0)


def mirrors_x(cells: list[Cell], params: dict[str, Any]) -> bool:
    """Whether every record's mirror field is the flipped X its size implies.

    ``-(x + w * 8)`` for each, which is what the field means. Not consulted by
    the read — a format is what the preset says it is — but it is how a file's
    dialect is told apart, since the mirrored form and the signed-word-X form are
    the same six bytes. False for a run with no mirror field to check.
    """
    _x_bytes, mirror, _order = _layout(params)
    if not mirror:
        return False
    return all(_mirror_of(cell) == -(_x_of(cell) + _across(cell) * 8) for cell in cells)


# The three geometry fields ride in ``Cell.flags``, packed in file order so the
# encode is a straight read back: y, size, x and the mirror byte.
#
# **X gets sixteen bits whatever the record spends on it**, sign-extended into
# them at decode time. A field only as wide as the narrow variant would silently
# fold the signed-word variant's offsets into a byte — and that variant is
# precisely what a reader is told to try when a run comes out scattered, so the
# fix would look like a second misreading.
def _y_of(cell: Cell) -> int:
    return _signed(cell.flags >> 32 & 0xFF, 1)


def _size_of(cell: Cell) -> int:
    return cell.flags >> 24 & 0xFF


def _x_of(cell: Cell) -> int:
    return _signed(cell.flags >> 8 & 0xFFFF, 2)


def _mirror_of(cell: Cell) -> int:
    return _signed(cell.flags & 0xFF, 1)


def _across(cell: Cell) -> int:
    return (_size_of(cell) >> 2 & 3) + 1


class MdSpriteCodec:
    """The record above, on the tilemap codec surface."""

    info = PluginInfo(
        id=MD_SPRITE_ENGINE,
        name="Mega Drive sprite mappings record",
        stage=Stage.INTERPRET_TILEMAP,
    )

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[Cell]:
        """bytes -> one cell per record, its geometry carried in ``flags``.

        Y, the size nibble, X and the mirror field have nowhere to live on a
        :class:`~celpix.core.tilemap.Cell`, so they travel whole. Dropping them
        would zero the object's layout on the next save, and :meth:`frames` is
        what reads them back out.
        """
        x_bytes, mirror, order = _layout(params)
        size = record_size(params)
        cells: list[Cell] = []
        for at in range(0, len(data) - size + 1, size):
            record = data[at : at + size]
            word = int.from_bytes(record[2:4], order)
            x = int.from_bytes(record[4 : 4 + x_bytes], "big", signed=True)
            cells.append(
                Cell(
                    index=word & _INDEX,
                    palette_row=(word & _ROW) >> _ROW_SHIFT,
                    priority=1 if word & _PRIORITY else 0,
                    flip_h=bool(word & _FLIP_H),
                    flip_v=bool(word & _FLIP_V),
                    flags=(
                        record[0] << 32
                        | record[1] << 24
                        | (x & 0xFFFF) << 8
                        | (record[-1] if mirror else 0)
                    ),
                )
            )
        return cells

    def encode(
        self, cells: list[Cell], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        """cells -> bytes, the exact inverse of :meth:`decode`.

        Every field is masked rather than checked: a cell can arrive from a paste
        out of a wider format, and refusing the write would leave the object
        unsaveable over one of them.
        """
        x_bytes, mirror, order = _layout(params)
        out = bytearray()
        for cell in cells:
            word = (
                cell.index & _INDEX
                | (cell.palette_row & 3) << _ROW_SHIFT
                | (_FLIP_H if cell.flip_h else 0)
                | (_FLIP_V if cell.flip_v else 0)
                | (_PRIORITY if cell.priority else 0)
            )
            flags = cell.flags
            out.append(flags >> 32 & 0xFF)
            out.append(flags >> 24 & 0xFF)
            out += word.to_bytes(2, order)
            out += (_x_of(cell) & ((1 << (x_bytes * 8)) - 1)).to_bytes(x_bytes, "big")
            if mirror:
                out.append(flags & 0xFF)
        return bytes(out)

    def bytes_per_cell(self, params: dict[str, Any]) -> int:
        return record_size(params)

    def cell_tiles(self, params: dict[str, Any]) -> tuple[int, int]:
        # A record covers anything up to 4x4, so there is no one answer; the
        # frame renderer asks each subsprite instead. One is the honest reply to
        # a question about cells in general.
        return (1, 1)

    def frames(
        self, cells: list[Cell], params: dict[str, Any], ctx: PipelineContext
    ) -> list[Frame]:
        """The records as one frame of subsprites — what the view draws.

        **One frame**, because a bare run is what this reads: the formats that
        hold several drawings say so with counts or fixed slots, and a run whose
        extent is the slice's has nowhere to state a boundary. An object stored
        as several runs is several slices.

        No drawn bit either — a record that is present is drawn. The console's
        own way of not drawing one is to leave it out of the count.

        The mirror field is **checked** on the way past, because the one way this
        format is misread has no other symptom: the byte-X-plus-mirror record and
        the signed-word-X one are the same six bytes, and the wrong reading draws
        a plausible object with its pieces in the wrong places rather than
        failing. A run whose mirror fields are not the flipped X they should be
        says so, and names the parameter to try.
        """
        if cells and not mirrors_x(cells, params):
            warn(
                ctx,
                "this run's mirror offsets do not match its X offsets",
                "The record may be the variant whose X is a signed word rather\n"
                "than a byte followed by the mirrored frame's X. Try x_bytes = 2\n"
                "with mirror_x off if the pieces are in the wrong places.",
                source=MD_SPRITE_ENGINE,
            )
        return [
            tuple(
                Subsprite(
                    x=_x_of(cell),
                    y=_y_of(cell),
                    index=cell.index,
                    palette_row=cell.palette_row,
                    priority=cell.priority,
                    flip_h=cell.flip_h,
                    flip_v=cell.flip_v,
                    across=_across(cell),
                    down=(_size_of(cell) & 3) + 1,
                    column_major=True,
                )
                for cell in cells
            )
        ]

    def has_palette_rows(self, params: dict[str, Any]) -> bool:
        return True

    def index_limit(self, params: dict[str, Any]) -> int:
        return _INDEX

    def palette_row_limit(self, params: dict[str, Any]) -> int:
        return 3

    def transform_cell(
        self, cell: Cell, op: CellOp, params: dict[str, Any]
    ) -> Cell | None:
        """Both mirrors, and no turn — the attribute word has no rotation bit.

        The record's mirror-X field is deliberately **not** recomputed: it is the
        offset this piece takes when the *whole object* is drawn flipped, so it
        belongs to the object's layout rather than to this subsprite's own bits.
        Flipping one piece in place leaves the mirrored layout as the file wrote
        it, which is the answer that loses nothing.
        """
        mirror = {CellOp.FLIP_H: Cell.flipped_h, CellOp.FLIP_V: Cell.flipped_v}.get(op)
        return mirror(cell) if mirror is not None else None
