"""The inverse of the two record-driven sprite engines: placing a piece.

``place_subsprite`` writes a piece's offsets and size into the record's own bits
and ``assign_frames`` moves a headed run's frame marks; both have to leave every
bit they were not asked about exactly where the file had it, since a sprite
record carries fields celPix has no meaning for (``docs/design/sprite-map.md``
§8). Model-layer only.
"""

from __future__ import annotations

import pytest

from celpix.core.context import PipelineContext
from celpix.plugins.builtins.md_sprite import MdSpriteCodec, mirrors_x
from celpix.plugins.builtins.sprite_record import SpriteRecordCodec

MD = {"layout": "sprite", "subsprite_size": "stated", "x_bytes": 1, "mirror_x": True}


def _md_record(*, x=0, y=0, tile=0, across=1, down=1, size_hi=0) -> bytes:
    size = size_hi << 4 | (across - 1) << 2 | (down - 1)
    mirror = -(x + across * 8)
    return (
        bytes((y & 0xFF, size))
        + tile.to_bytes(2, "big")
        + bytes((x & 0xFF, mirror & 0xFF))
    )


def _piece(frames, record):
    return next(p for f in frames for p in f if p.record == record)


def test_md_sprite_place_reads_back_and_keeps_the_mirror_true() -> None:
    """A move and a resize each re-derive the mirror, so the dialect check the
    read makes stays true; the size byte's high bits and the attribute word ride
    through byte for byte."""
    codec, ctx = MdSpriteCodec(), PipelineContext()
    raw = _md_record(x=-8, y=4, tile=0x8123, across=2, down=1, size_hi=0xA)
    raw += _md_record(x=16, y=-4, tile=0x0042)
    cells = codec.decode(raw, MD, ctx)

    moved = codec.place_subsprite(cells[0], MD, ctx, x=-40, y=-100)
    sized = codec.place_subsprite(cells[1], MD, ctx, size=(4, 3))
    edited = [moved, sized]
    assert mirrors_x(edited, MD)
    (frame,) = codec.frames(edited, MD, ctx)
    assert [(p.record, p.x, p.y, p.size()) for p in frame] == [
        (0, -40, -100, (2, 1)),
        (1, 16, -4, (4, 3)),
    ]
    out = codec.encode(edited, MD, ctx)
    assert out == (
        _md_record(x=-40, y=-100, tile=0x8123, across=2, down=1, size_hi=0xA)
        + _md_record(x=16, y=-4, tile=0x0042, across=4, down=3)
    )
    # A y-only nudge does not touch the mirror byte, nor anything but byte 0.
    nudged = codec.encode([codec.place_subsprite(cells[0], MD, ctx, y=5)], MD, ctx)
    assert nudged[1:] == raw[1:6] and nudged[0] == 5

    limits = codec.subsprite_limits(MD, ctx)
    assert (limits.structure, limits.frames, limits.per_frame) == ("run", 1, None)
    assert limits.x_range == range(-128, 128) and len(limits.size) == 16
    wide = codec.subsprite_limits(MD | {"x_bytes": 2, "mirror_x": False}, ctx)
    assert wide.x_range == range(-32768, 32768)


def test_md_sprite_place_refuses_what_the_record_cannot_hold() -> None:
    codec, ctx = MdSpriteCodec(), PipelineContext()
    (cell,) = codec.decode(_md_record(), MD, ctx)
    with pytest.raises(ValueError, match=r"-128\.\.127"):
        codec.place_subsprite(cell, MD, ctx, x=200)
    with pytest.raises(ValueError, match=r"-128\.\.127"):
        codec.place_subsprite(cell, MD, ctx, y=-129)
    with pytest.raises(ValueError, match=r"1\.\.4"):
        codec.place_subsprite(cell, MD, ctx, size=(5, 1))
    with pytest.raises(ValueError, match=r"1\.\.4"):
        codec.place_subsprite(cell, MD, ctx, size="large")
    # x = 120 fits the x byte, but a 2-wide piece's mirror -(120 + 16) does not
    # fit its own.
    with pytest.raises(ValueError, match="mirrored x"):
        codec.place_subsprite(cell, MD, ctx, x=120, size=(2, 1))


# A Mega Drive-shaped record stated as fields: a carried mirror word, a pad byte,
# and unclaimed bits in the size byte and the attribute word.
_CR = {
    "layout": "sprite",
    "subsprite_size": "stated",
    "record": [
        {"name": "x", "type": "s16"},
        {"name": "x_mirror", "type": "s16"},
        {"name": "y", "type": "s16"},
        {"name": "pad", "type": "u8"},
        {"name": "size", "type": "u8", "bits": "....ccrr"},
        {"name": "attr", "type": "u16", "bits": "oppv hiii iiii iiii"},
    ],
}


def _cr_record(x, y, size_byte, attr=0x2345) -> bytes:
    return (
        x.to_bytes(2, "big", signed=True)
        + b"\x12\x34"
        + y.to_bytes(2, "big", signed=True)
        + b"\x5a"
        + bytes((size_byte,))
        + attr.to_bytes(2, "big")
    )


def test_sprite_record_place_by_cr_bits_keeps_every_carried_bit() -> None:
    engine, ctx = SpriteRecordCodec(), PipelineContext()
    raw = _cr_record(-3, 7, 0xB4)  # c = 1, r = 0, high nibble 0xB unclaimed
    (cell,) = engine.decode(raw, _CR, ctx)
    placed = engine.place_subsprite(cell, _CR, ctx, x=-300, y=1000, size=(4, 3))
    assert (placed.index, placed.palette_row, placed.flip_h) == (
        cell.index,
        cell.palette_row,
        cell.flip_h,
    )
    ((piece,),) = engine.frames([placed], _CR, ctx)
    assert (piece.record, piece.x, piece.y, piece.size()) == (0, -300, 1000, (4, 3))
    assert engine.encode([placed], _CR, ctx) == _cr_record(-300, 1000, 0xBE)

    limits = engine.subsprite_limits(_CR, ctx)
    assert (limits.structure, limits.frames) == ("run", 1)
    assert limits.x_range == range(-32768, 32768)
    assert set(limits.size) == {(a, d) for a in range(1, 5) for d in range(1, 5)}
    with pytest.raises(ValueError, match=r"-32768\.\.32767"):
        engine.place_subsprite(cell, _CR, ctx, x=40000)
    with pytest.raises(ValueError, match="not 'small'"):
        engine.place_subsprite(cell, _CR, ctx, size="small")


def _packed(pos: str, kind: str = "s16", **over):
    """Both offsets and the size bits packed into one word, then a tile byte."""
    return {
        "layout": "sprite",
        "record": [
            {"name": "pos", "type": kind, "bits": pos},
            {"name": "tile", "type": "u8", "bits": "iiii iiii"},
        ],
        **over,
    }


def test_sprite_record_place_by_size_code_and_by_size_bit() -> None:
    """The two spellings of a size the record does not state as a rectangle: a
    code into ``sizes``, and a lone bit naming small or large — never a shape,
    since the bit resolves against a pair the file does not hold."""
    engine, ctx = SpriteRecordCodec(), PipelineContext()
    coded = _packed(
        pos="ssxx xxxx .yyy yyyy",
        sizes=[[1, 1], [2, 2], [4, 2], [4, 4]],
        subsprite_size="stated",
    )
    (cell,) = engine.decode(b"\x00\x80\x07", coded, ctx)  # the '.' bit set
    limits = engine.subsprite_limits(coded, ctx)
    assert limits.size == ((1, 1), (2, 2), (4, 2), (4, 4))
    assert limits.x_range == range(-32, 32) and limits.y_range == range(-64, 64)
    placed = engine.place_subsprite(cell, coded, ctx, x=-32, y=63, size=(4, 2))
    ((piece,),) = engine.frames([placed], coded, ctx)
    assert (piece.x, piece.y, piece.size(), piece.index) == (-32, 63, (4, 2), 7)
    assert engine.encode([placed], coded, ctx) == bytes((0b1010_0000, 0xBF, 0x07))
    with pytest.raises(ValueError, match="not \\(3, 3\\)"):
        engine.place_subsprite(cell, coded, ctx, size=(3, 3))
    with pytest.raises(ValueError, match=r"-32\.\.31"):
        engine.place_subsprite(cell, coded, ctx, x=32)

    bit = _packed(pos="sxxx xxxx yyyy yyyy", size_pair=[1, 4], subsprite_size="stated")
    (cell,) = engine.decode(b"\x00\x00\x01", bit, ctx)
    assert engine.subsprite_limits(bit, ctx).size == ("small", "large")
    large = engine.place_subsprite(cell, bit, ctx, size="large")
    assert engine.frames([large], bit, ctx)[0][0].size() == (4, 4)
    small = engine.place_subsprite(large, bit, ctx, size="small")
    assert small == cell
    with pytest.raises(ValueError, match="small, large"):
        engine.place_subsprite(cell, bit, ctx, size=(4, 4))

    # On an unsigned field the same bits are 0..2^n - 1.
    unsigned = _packed(pos="xxxx xxxx yyyy yyyy", kind="u16")
    assert engine.subsprite_limits(unsigned, ctx).x_range == range(0, 256)


_OAM = {
    "layout": "sprite",
    "subsprite_size": "stated",
    "record": [
        {"name": "y", "type": "s8"},
        {"name": "tile", "type": "u8", "bits": "iiii iiii"},
        {"name": "attr", "type": "u8", "bits": "vho. ..pp"},
        {"name": "x", "type": "s8"},
    ],
}


def test_sprite_record_writes_a_whole_field_offset_and_refuses_a_fixed_size() -> None:
    """The ``nes-oam`` shape: x and y are whole fields with no bits, written by
    the whole-field writer at the type's range; the attribute byte's three
    unused bits ride through; and a record whose size the preset fixes takes no
    size at all."""
    engine, ctx = SpriteRecordCodec(), PipelineContext()
    raw = bytes((0x10, 0x42, 0b0011_1101, 0xF8))
    (cell,) = engine.decode(raw, _OAM, ctx)
    placed = engine.place_subsprite(cell, _OAM, ctx, x=-128, y=127)
    assert engine.encode([placed], _OAM, ctx) == bytes((0x7F, 0x42, 0x3D, 0x80))
    limits = engine.subsprite_limits(_OAM, ctx)
    assert limits.size is None and limits.x_range == range(-128, 128)
    with pytest.raises(ValueError, match=r"-128\.\.127"):
        engine.place_subsprite(cell, _OAM, ctx, x=128)
    with pytest.raises(ValueError, match="fixes"):
        engine.place_subsprite(cell, _OAM, ctx, size=(1, 1))

    # No y anywhere: a piece cannot move on that axis, and says so.
    flat = {**_OAM, "record": _OAM["record"][1:]}
    (cell,) = engine.decode(raw[1:], flat, ctx)
    assert engine.subsprite_limits(flat, ctx).y_range == range(0, 1)
    assert engine.place_subsprite(cell, flat, ctx, y=0) == cell
    with pytest.raises(ValueError, match="no y field"):
        engine.place_subsprite(cell, flat, ctx, y=1)


_HEADED = {
    "layout": "sprite",
    "record": [
        {"name": "x", "type": "s8"},
        {"name": "y", "type": "s8"},
        {"name": "tile", "type": "u8", "bits": "iiii iiii"},
    ],
    "frame_header": {"bytes": 3, "count_at": 2, "count_type": "u8"},
}


def test_headed_assign_frames_moves_the_header_with_the_frame() -> None:
    """A frame's header rides on its first cell, so reordering, emptying and
    populating frames is a matter of where the marks sit — and the encode's
    counts then follow the cells, the header's other bytes with them."""
    engine, ctx = SpriteRecordCodec(), PipelineContext()
    raw = (
        b"\xaa\xbb\x02" + b"\x00\x00\x01" + b"\x08\x00\x02"
        + b"\xcc\xdd\x01" + b"\x00\x08\x03"
    )  # fmt: skip
    a, b, c = engine.decode(raw, _HEADED, ctx)
    limits = engine.subsprite_limits(_HEADED, ctx)
    assert (limits.structure, limits.per_frame, limits.frames) == ("headed", 255, None)
    frames = engine.frames([a, b, c], _HEADED, ctx)
    assert [[p.record for p in f] for f in frames] == [[0, 1], [2]]

    # Bring the second piece of frame 0 to the front: the header follows the
    # frame, not the cell.
    swapped = engine.assign_frames([b, a, c], [0, 0, 1], _HEADED, ctx)
    assert engine.encode(swapped, _HEADED, ctx) == (
        b"\xaa\xbb\x02" + b"\x08\x00\x02" + b"\x00\x00\x01"
        + b"\xcc\xdd\x01" + b"\x00\x08\x03"
    )  # fmt: skip

    # Move frame 1's only piece into frame 0, emptying frame 1.
    merged = engine.assign_frames([a, b, c], [0, 0, 0], _HEADED, ctx)
    assert (
        engine.encode(merged, _HEADED, ctx)
        == raw[:3].replace(b"\x02", b"\x03") + raw[3:9] + raw[12:]
    )

    # Split frame 0's second piece off into a new frame of its own: a zero,
    # count-only header opens it.
    split = engine.assign_frames([a, b, c], [0, 1, 2], _HEADED, ctx)
    assert engine.encode(split, _HEADED, ctx) == (
        b"\xaa\xbb\x01" + b"\x00\x00\x01"
        + b"\x00\x00\x01" + b"\x08\x00\x02"
        + b"\xcc\xdd\x01" + b"\x00\x08\x03"
    )  # fmt: skip

    with pytest.raises(ValueError, match="backwards"):
        engine.assign_frames([a, b, c], [0, 1, 0], _HEADED, ctx)
    # A header-less run has no marks to move.
    run = {k: v for k, v in _HEADED.items() if k != "frame_header"}
    cells = engine.decode(raw, run, ctx)
    assert engine.assign_frames(cells, [0] * len(cells), run, ctx) == cells


def test_a_headed_frame_keeps_its_header_when_its_front_record_goes() -> None:
    """The header rides on a frame's front cell, so the ops stage the leaving
    cell behind the rest of its frame before deleting or moving it: the frame
    keeps its header bytes, and the frame a record moves into keeps its own."""
    from celpix.core.sprite_edit import move_subsprite, remove_subsprites

    engine, ctx = SpriteRecordCodec(), PipelineContext()
    raw = (
        b"\xaa\xbb\x02" + b"\x00\x00\x01" + b"\x08\x00\x02"
        + b"\xcc\xdd\x01" + b"\x00\x08\x03"
    )  # fmt: skip
    cells = engine.decode(raw, _HEADED, ctx)
    frames = engine.frames(cells, _HEADED, ctx)
    limits = engine.subsprite_limits(_HEADED, ctx)
    ops = {
        "place": lambda cell, **kw: engine.place_subsprite(cell, _HEADED, ctx, **kw),
        "assign": lambda c, f: engine.assign_frames(c, f, _HEADED, ctx),
    }

    removed = remove_subsprites(cells, frames, limits, [0], **ops)
    assert engine.encode(removed, _HEADED, ctx) == (
        b"\xaa\xbb\x01" + b"\x08\x00\x02" + b"\xcc\xdd\x01" + b"\x00\x08\x03"
    )

    moved, at = move_subsprite(cells, frames, limits, 0, 1, **ops)
    assert at == 2
    assert engine.encode(moved, _HEADED, ctx) == (
        b"\xaa\xbb\x01" + b"\x08\x00\x02"
        + b"\xcc\xdd\x02" + b"\x00\x08\x03" + b"\x00\x00\x01"
    )  # fmt: skip
