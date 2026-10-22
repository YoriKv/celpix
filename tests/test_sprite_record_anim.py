"""The sprite-record engine's ``animation`` block: tables of ``(frame pointer,
duration)`` steps stored beside a run of frames, read by address.

Two things carry the risk: the pointer arithmetic that turns a step into a
frame (low bits replaced, unknown frames kept, another run's tables skipped,
the hold and loop terminators shaping the sequence), and the rule that keeps
every frame's record count while the tables address the frames
(``docs/design/sprite-map.md`` §4). Model-layer only.
"""

from __future__ import annotations

import pytest

from celpix.core.animation import Sequence, Step
from celpix.core.context import KEY_INPUTS, KEY_TILEMAP_ANIMATIONS, PipelineContext
from celpix.core.sprite_edit import remove_subsprites, reorder_subsprite
from celpix.plugins.builtins.sprite_record import (
    UNKNOWN_FRAME,
    AnimationLayout,
    SpriteRecordCodec,
    animation_sequences,
)

# A count byte, then that many four-byte parts, little-endian, as FF6's menu
# module stores them; the tables' pointers are the frames' low 16 bits.
PARAMS = {
    "layout": "sprite",
    "subsprite_size": "stated",
    "endian": "little",
    "size_pair": [1, 2],
    "record": [
        {"name": "pos", "type": "u8", "bits": "sxxx xxxx"},
        {"name": "y", "type": "u8"},
        {"name": "obj", "type": "u16", "bits": "vhoo pppi iiii iiii"},
    ],
    "frame_header": {"bytes": 1, "count_at": 0, "count_type": "u8"},
    "animation": {"pointer": "u16", "duration": "u8", "hold": 0xFE, "loop": 0xFF},
}
ADDRESS = 0xC2_1000
# Frame 0 at offset 0 (two parts), frame 1 at offset 9 (one part).
RUN = (
    b"\x02" + b"\x00\x00\x01\x00" + b"\x88\x00\x02\x00"
    + b"\x01" + b"\x00\x08\x03\x00"
)  # fmt: skip


def _step(pointer: int, duration: int) -> bytes:
    return pointer.to_bytes(2, "little") + bytes((duration,))


TABLES = (
    # loops: the $FF step is a placeholder, never drawn
    _step(0x1000, 4) + _step(0x1009, 6) + _step(0x1009, 0xFF)
    # holds frame 0 for good; its middle step names a frame the run lacks
    + _step(0x1009, 3) + _step(0x2000, 5) + _step(0x1000, 0xFE)
    # another run's table: names none of these frames
    + _step(0x3000, 2) + _step(0x3004, 0xFF)
)  # fmt: skip


def test_tables_read_by_address_into_sequences_and_published_on_decode() -> None:
    assert animation_sequences(RUN, TABLES, ADDRESS, PARAMS) == (
        Sequence((Step(4, 0), Step(6, 1)), loop=0),
        Sequence((Step(3, 1), Step(5, UNKNOWN_FRAME), Step(0xFE, 0)), loop=2),
    )
    engine = SpriteRecordCodec()
    ctx = PipelineContext()
    ctx.set(KEY_INPUTS, {"tables": TABLES, "address": ADDRESS})
    engine.decode(RUN, PARAMS, ctx)
    assert len(ctx.get(KEY_TILEMAP_ANIMATIONS)) == 2
    bare = PipelineContext()
    assert len(engine.decode(RUN, PARAMS, bare)) == 3
    assert bare.get(KEY_TILEMAP_ANIMATIONS) is None


def test_frames_keep_their_record_counts_under_animation() -> None:
    engine, ctx = SpriteRecordCodec(), PipelineContext()
    cells = engine.decode(RUN, PARAMS, ctx)
    a, b, c = cells
    # A permutation within a frame keeps every frame's length.
    swapped = engine.assign_frames([b, a, c], [0, 0, 1], PARAMS, ctx)
    assert engine.encode(swapped, PARAMS, ctx) == (
        b"\x02" + RUN[5:9] + RUN[1:5] + RUN[9:]
    )
    with pytest.raises(ValueError, match="keeps its record count"):
        engine.assign_frames([a, b, b, c], [0, 0, 0, 1], PARAMS, ctx)  # an add
    with pytest.raises(ValueError, match="keeps its record count"):
        engine.assign_frames([a, b, c], [0, 1, 1], PARAMS, ctx)  # across frames

    # Through the ops: a reorder lands, and a remove - which demotes and
    # assigns once with the counts intact, then deletes and assigns again -
    # is refused before anything changes.
    frames = engine.frames(cells, PARAMS, ctx)
    limits = engine.subsprite_limits(PARAMS, ctx)

    def assign(cs, fs):
        return engine.assign_frames(cs, fs, PARAMS, ctx)

    def place(cell, **kw):
        return engine.place_subsprite(cell, PARAMS, ctx, **kw)

    assert reorder_subsprite(cells, frames, limits, 1, 0, assign=assign) == swapped
    with pytest.raises(ValueError, match="keeps its record count"):
        remove_subsprites(cells, frames, limits, [1], place=place, assign=assign)
    assert cells == [a, b, c]


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ({"duration": "u8"}, "needs a `pointer`"),
        ({"pointer": "u24"}, "must be one of u8, u16, u32"),
        ({"pointer": "u16", "duration": "s8"}, "must be one of u8, u16, u32"),
        ({"pointer": "u16", "hold": 0xFF, "loop": 0xFF}, "must differ"),
        ({"pointer": "u16", "hodl": 0xFE}, "not hodl"),
        ("u16", "must be a table"),
    ],
)
def test_animation_block_is_validated(block, message) -> None:
    with pytest.raises(ValueError, match=message):
        AnimationLayout(PARAMS | {"animation": block})
    assert (
        AnimationLayout.of({k: v for k, v in PARAMS.items() if k != "animation"})
        is None
    )
