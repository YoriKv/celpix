"""The sprite-object cell codec: 6-byte part records, and the frames they make.

A tilemap codec by protocol — bytes to a flat list of
:class:`~celpix.core.tilemap.Cell` and back — but the cells are **sprite parts**
rather than grid positions, and the grid they would lay out in does not exist:
a part carries a signed pixel offset, and those offsets are not tile-aligned
(:mod:`celpix.core.sprite`). So this engine has a second output the generic
packed engine has no need for, :meth:`frames`, which is what the view actually
draws. The cells stay the file's own records, in the file's own order, so a
write puts back exactly what was read.

The record, 6 bytes and hardware-shaped:

===== =========================================================
byte  meaning
===== =========================================================
0     bit 7 the part is drawn, bit 0 which of the two sizes
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
from celpix.core.sprite import SIZE_PAIRS, Frame, Part
from celpix.core.tilemap import Cell
from celpix.plugins.base import PluginInfo

OBJECT_ENGINE = "codec.scgcad-object"

RECORD = 6  # bytes per part
PARTS_PER_FRAME = 64  # every frame has room for this many, used or not

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
    """The object's two sprite sizes, which the file does not record.

    A game set this in a register, so a reader has to be told; the preset says
    which pair and the corpus gives no way to recover it from the bytes. Out of
    range falls back to the first pair rather than raising — a hand-edited
    preset should draw something.
    """
    at = int(params.get("size_pair", 0))
    return SIZE_PAIRS[at] if 0 <= at < len(SIZE_PAIRS) else SIZE_PAIRS[0]


def parts_per_frame(params: dict[str, Any]) -> int:
    return max(1, int(params.get("parts_per_frame", PARTS_PER_FRAME)))


class ObjectCodec:
    info = PluginInfo(
        id=OBJECT_ENGINE,
        name="Sprite object part",
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

    def bytes_per_cell(self, params: dict[str, Any]) -> int:
        return RECORD

    def cell_tiles(self, params: dict[str, Any]) -> tuple[int, int]:
        # A part covers 1, 4, 16 or 64 tiles depending on its own size bit, so
        # there is no one answer; the frame renderer asks each part instead.
        # One is the honest reply to a question about cells in general.
        return (1, 1)

    def size_pair(self, params: dict[str, Any]) -> tuple[int, int]:
        """The two sprite sizes a part's size bit chooses between."""
        return size_pair(params)

    def frames(self, cells: list[Cell], params: dict[str, Any]) -> list[Frame]:
        """The cells regrouped into frames of parts — what the view draws.

        The extra half of this engine, and the reason it is not the generic
        packed one. Undrawn parts are dropped here rather than carried as
        invisible ones: they are the file's empty slots, all 94% of them, and
        nothing downstream would have anything to do with them.
        """
        per_frame = parts_per_frame(params)
        return [
            tuple(
                part
                for cell in cells[at : at + per_frame]
                if (part := _part(cell)) is not None
            )
            for at in range(0, len(cells), per_frame)
        ]


def _part(cell: Cell) -> Part | None:
    """One decoded part, or None when the file says this slot is not drawn."""
    flags = cell.flags
    if not (flags >> 24) & _DRAWN:
        return None
    return Part(
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


def _signed(value: int) -> int:
    """A byte offset as the signed number it is: parts sit around an origin."""
    return value - 0x100 if value > 0x7F else value
