"""The sprite-record inverse of the S-CG-CAD-family codecs: OBJ/OBX, OBZ and SPR.

``place_subsprite`` writes the fields a Cell has no attribute for into ``flags``
at the format's own positions, and has to be exactly :meth:`frames`' inverse
while leaving every bit nobody documents alone. SPR additionally carries its
frame membership on the cells, so a structural edit survives a re-read and an
undo of the cells alone (``docs/design/sprite-map.md`` §8.3, §9).
"""

from __future__ import annotations

import pytest

from celpix.core.context import KEY_TILEMAP_FRAME_SIZES, PipelineContext
from celpix.core.tilemap import CellOp
from celpix.plugins.base import ReadSource, WriteTarget
from celpix.plugins.builtins.object_codec import ObjectCodec, ObzCodec, SprCodec
from celpix.plugins.builtins.ys_spr import FRAMES, TRAILER, SprContainer

# -- record builders, every opaque bit settable --------------------------------


def _obj(x=0, y=0, *, large=False, drawn=True, spare=0x7E, group=0xFF) -> bytes:
    """An OBJ record; ``spare`` is byte 0's bits 1-6, which nothing documents."""
    b0 = (0x80 if drawn else 0) | (0x01 if large else 0) | spare
    attr = 0x1FF | (5 << 9) | (2 << 12)
    return bytes([b0, group, y & 0xFF, x & 0xFF]) + attr.to_bytes(2, "big")


def _obz(x=0, y=0, *, large=False, drawn=True, spare=0x30, group=0xFF) -> bytes:
    """An OBZ record; ``spare`` is byte 0's bits 4-5, plus attribute bit 0 set."""
    tile = 0xABC
    b0 = (0x80 if drawn else 0) | (0x40 if large else 0) | spare | (tile >> 8)
    attr = (5 << 1) | (2 << 4) | 1
    return bytes([b0, tile & 0xFF, x & 0xFF, y & 0xFF, attr, group])


def _spr(x=0, y=0, *, large=False, tile=0x42, size_spare=0xFD) -> bytes:
    """An SPR record with attribute bit 0 and the size byte's other bits set."""
    attr = (5 << 1) | (2 << 4) | 1
    size = (0x02 if large else 0) | size_spare
    return (
        (x & 0xFFFF).to_bytes(2, "big")
        + (y & 0xFFFF).to_bytes(2, "big")
        + tile.to_bytes(2, "big")
        + bytes([attr, size])
    )


def _spr_file(frames: list[bytes]) -> bytes:
    out = bytearray()
    for at in range(FRAMES):
        records = frames[at] if at < len(frames) else b""
        out.append(len(records) // 8)
        out += records
    return bytes(out) + bytes(TRAILER) + b"SIG"


_CODECS = [
    pytest.param(ObjectCodec(), _obj, True, id="obj"),
    pytest.param(ObzCodec(), _obz, True, id="obz"),
    pytest.param(SprCodec(), _spr, False, id="spr"),
]
_SLOTS = [
    pytest.param(ObjectCodec(), _obj, id="obj"),
    pytest.param(ObzCodec(), _obz, id="obz"),
]


# -- the inverse ---------------------------------------------------------------


@pytest.mark.parametrize(("codec", "record", "has_drawn"), _CODECS)
def test_a_placed_piece_reads_back_as_placed_and_keeps_every_other_bit(
    codec, record, has_drawn
) -> None:
    """The second record is the one placed, so ``record`` has to be 1, not a
    position among the drawn pieces; and what the save writes differs from the
    original in exactly the fields named — the group byte, the byte-0 spares and
    the attribute bit 0 all survive."""
    ctx = PipelineContext()
    raw = record(3, 4, large=True) + record(10, -20, large=True)
    cells = codec.decode(raw, ctx)

    moved = codec.place_subsprite(cells[1], ctx, x=-5, y=7, size="small")
    if has_drawn:
        moved = codec.place_subsprite(moved, ctx, drawn=True)  # already drawn
    cells[1] = moved

    pieces = [sub for frame in codec.frames(cells, ctx) for sub in frame]
    assert [sub.record for sub in pieces] == [0, 1]
    assert (pieces[1].x, pieces[1].y, pieces[1].size()) == (-5, 7, (1, 1))
    assert (pieces[1].index, pieces[1].palette_row) == (cells[1].index, 5)
    assert codec.encode(cells, ctx) == record(3, 4, large=True) + record(
        -5, 7, large=False
    )


@pytest.mark.parametrize(("codec", "record", "has_drawn"), _CODECS)
def test_a_value_the_field_cannot_hold_is_refused_with_its_range(
    codec, record, has_drawn
) -> None:
    """Never clamped: a clamped piece is somewhere the user did not put it."""
    ctx = PipelineContext()
    (cell,) = codec.decode(record(), ctx)
    reach = codec.subsprite_limits(ctx).x_range
    text = f"{reach.start}..{reach.stop - 1}"
    with pytest.raises(ValueError, match=text):
        codec.place_subsprite(cell, ctx, x=reach.stop)
    with pytest.raises(ValueError, match=text):
        codec.place_subsprite(cell, ctx, x=0, y=reach.start - 1)
    with pytest.raises(ValueError, match="small and large"):
        codec.place_subsprite(cell, ctx, size=(2, 2))


@pytest.mark.parametrize(("codec", "record"), _SLOTS)
def test_clearing_the_drawn_bit_empties_the_slot_and_setting_it_fills_it(
    codec, record
) -> None:
    ctx = PipelineContext()
    raw = record(1, 1) + record(2, 2)
    cells = codec.decode(raw, ctx)
    cells[0] = codec.place_subsprite(cells[0], ctx, drawn=False)
    (frame,) = codec.frames(cells, ctx)
    assert [sub.record for sub in frame] == [1]

    cells[0] = codec.place_subsprite(cells[0], ctx, drawn=True)
    assert codec.encode(cells, ctx) == raw


@pytest.mark.parametrize(("codec", "record", "has_drawn"), _CODECS)
def test_a_piece_mirrors_through_its_own_bits_and_never_turns(
    codec, record, has_drawn
) -> None:
    ctx = PipelineContext()
    (cell,) = codec.decode(record(), ctx)
    flipped = codec.transform_cell(cell, CellOp.FLIP_V)
    assert flipped.flip_v and not flipped.flip_h
    (back,) = codec.decode(codec.encode([flipped], ctx), ctx)
    assert back.flip_v
    assert codec.transform_cell(cell, CellOp.ROTATE_CW) is None


# -- SPR frame membership ------------------------------------------------------


def _read(raw: bytes):
    codec = SprCodec()
    ctx = PipelineContext()
    cells = codec.decode(SprContainer().read(ReadSource(data=raw), ctx), ctx)
    return codec, ctx, cells


def test_a_decoded_pattern_carries_its_frames_without_the_context() -> None:
    """The re-read case (§3.5 item 2): the container republishes the sizes from
    the file, so the cells' own stamps must be enough to frame them — and the
    record stays an index into the whole list."""
    codec, ctx, cells = _read(_spr_file([_spr(1) + _spr(2), b"", _spr(3)]))
    framed = codec.frames(cells, ctx)
    unaided = codec.frames(cells, PipelineContext())
    assert unaided == framed
    assert len(unaided) == FRAMES
    assert [[s.record for s in f] for f in unaided[:4]] == [[0, 1], [], [2], []]
    assert all(len(codec.encode([cell], ctx)) == 8 for cell in cells)


def test_moving_a_piece_to_another_frame_writes_the_new_counts() -> None:
    """The container cuts the file where the context's sizes say, so
    ``assign_frames`` has to republish them; and the frame it empties is an
    ordinary count of 0, not a frame dropped and the rest renumbered."""
    raw = _spr_file([_spr(1), _spr(2) + _spr(3)])
    codec, ctx, cells = _read(raw)

    # Cell 0 (frame 0's only piece) moves to frame 2; listed out of frame order.
    moved = codec.assign_frames(cells, [2, 1, 1], ctx)
    assert [c.index for c in moved] == [cells[1].index, cells[2].index, cells[0].index]
    assert ctx.get(KEY_TILEMAP_FRAME_SIZES)[:4] == (0, 2, 1, 0)

    encoded = codec.encode(moved, ctx)
    written = SprContainer().write(encoded, WriteTarget(existing=raw), ctx)
    assert written == _spr_file([b"", _spr(2) + _spr(3), _spr(1)])

    # And an undo that restores the old cells alone restores the old counts.
    assert (
        SprContainer().write(codec.encode(cells, ctx), WriteTarget(existing=raw), ctx)
        == raw
    )


def test_a_frame_the_file_does_not_have_is_refused() -> None:
    codec, ctx, cells = _read(_spr_file([_spr(1)]))
    with pytest.raises(ValueError, match="0..31"):
        codec.assign_frames(cells, [FRAMES], ctx)
