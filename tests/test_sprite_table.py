"""The sprite mapping table: offsets as the entry, frames out of a region input."""

from __future__ import annotations

import pytest

from celpix.core.context import (
    KEY_INPUTS,
    KEY_TILEMAP_SUBSPRITE_TILES,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.core.notices import notices
from celpix.core.sprite import Subsprite
from celpix.plugins.builtins.sprite_table import SPRITE_TABLE_ENGINE
from celpix.plugins.registry import default_registry

# One piece, as every layout below must read it: 2x3 tiles from $3C8, palette
# row 2, flipped horizontally, at a negative X.
_PIECE = Subsprite(
    x=-24,
    y=-8,
    index=0x3C8,
    palette_row=2,
    flip_h=True,
    across=2,
    down=3,
    column_major=True,
)
_Y, _SIZE, _ATTR = b"\xf8", b"\x06", b"\x4b\xc8"  # y -8, (2-1)<<2 | (3-1), row 2 + h


def _engine_and_params(preset_id: str):  # noqa: ANN202
    reg = default_registry()
    preset = reg.preset(preset_id)
    return reg.plugin(Stage.INTERPRET_TILEMAP, preset.engine_id), dict(preset.params)


def _frames(preset_id: str, table: bytes, region: bytes, **extra):  # noqa: ANN202
    engine, params = _engine_and_params(preset_id)
    params.update(extra)
    ctx = PipelineContext()
    ctx.set(KEY_INPUTS, {"frames": region})
    cells = engine.decode(table, params, ctx)
    assert engine.encode(cells, params, ctx) == table
    return engine.frames(cells, params, ctx), ctx


@pytest.mark.parametrize(
    ("preset_id", "frame"),
    [
        ("preset.tilemap.sega-mappings-5", b"\x01" + _Y + _SIZE + _ATTR + b"\xe8"),
        (
            "preset.tilemap.sonic2-mappings",
            b"\x00\x01" + _Y + _SIZE + _ATTR + b"\x25\xe4" + b"\xff\xe8",
        ),
        (
            "preset.tilemap.sonic3k-mappings",
            b"\x00\x01" + _Y + _SIZE + _ATTR + b"\xff\xe8",
        ),
    ],
)
def test_each_sonic_layout_reads_its_own_piece(preset_id: str, frame: bytes) -> None:
    """The three shipped dialects differ in the count's width, X's width and
    Sonic 2's two-player word between; each must land the same piece, with the
    offset counted from the table's first byte."""
    table = b"\x00\x02"
    (got,), _ = _frames(preset_id, table, table + frame)
    assert got == (_PIECE,)


def test_a_table_draws_each_distinct_frame_once_and_keeps_unreachable_ones() -> None:
    """Table order, repeats dropped: a table naming one frame for several steps
    draws it once. A count of 0 is an empty frame, and a frame past the region
    is kept empty rather than dropped — so frame k is still the table's k-th —
    with a notice that says so."""
    table = b"\x00\x08\x00\x09\x00\x09\x7f\x00"
    region = table + b"\x00" + b"\x01" + _Y + _SIZE + _ATTR + b"\xe8"
    frames, ctx = _frames("preset.tilemap.sega-mappings-5", table, region)
    assert frames == [(), (_PIECE,), ()]
    (notice,) = notices(ctx)
    assert notice.source == SPRITE_TABLE_ENGINE and "1 of" in notice.summary


def test_a_frame_reads_every_piece_its_count_names() -> None:
    """A count of 2 is two records back to back after the header, the second
    read at one record's size past the first rather than over it."""
    table = b"\x00\x02"
    second = b"\x10" + b"\x00" + b"\x00\x01" + b"\x08"  # y 16, 1x1, tile 1, x 8
    frame = b"\x02" + _Y + _SIZE + _ATTR + b"\xe8" + second
    (got,), _ = _frames("preset.tilemap.sega-mappings-5", table, table + frame)
    assert got == (_PIECE, Subsprite(x=8, y=16, index=1, column_major=True))


def test_a_region_ending_inside_a_frame_header_draws_that_frame_empty() -> None:
    """A count word cut off by the region's end is not read as a short count:
    the frame is kept, empty, and the notice says one frame ran past."""
    table = b"\x00\x02"
    frames, ctx = _frames("preset.tilemap.sonic2-mappings", table, table + b"\x00")
    assert frames == [()]
    (notice,) = notices(ctx)
    assert "1 of" in notice.summary


def test_grid_frames_read_a_grid_of_nametable_words_only_when_asked() -> None:
    """Phantasy Star II's second frame kind: first byte 0 or >= $80, then
    ``x.w y.w cols-1 rows-1`` and the words, 0 drawing nothing. Off by default,
    because to the Sonic 1 layout the same leading 0 is an empty frame."""
    table = b"\x00\x02"
    grid = b"\x00\x10\xff\xf0\x01\x00" + b"\x20\x05\x00\x00"  # x 16, y -16, 2x1
    region = table + grid
    plain, _ = _frames("preset.tilemap.sega-mappings-5", table, region)
    assert plain == [()]
    (got,), _ = _frames(
        "preset.tilemap.sega-mappings-5", table, region, grid_frames=True
    )
    assert got == (Subsprite(x=16, y=-16, index=5, palette_row=1),)
    with pytest.raises(ValueError, match="u8 at count_at = 0"):
        _frames("preset.tilemap.sonic3k-mappings", table, region, grid_frames=True)


def test_a_piece_with_a_lone_size_bit_reads_the_size_pair_off_the_context() -> None:
    """The table hands its pieces the pair the host published, and answers the
    host's probe with its preset's stated `size_pair`."""
    engine, params = _engine_and_params("preset.tilemap.sega-mappings-5")
    params["record"] = [
        {"name": "y", "type": "s8"},
        {"name": "size", "type": "u8", "bits": "s... ...."},
        {"name": "attr", "type": "u16", "bits": "oppv hiii iiii iiii"},
        {"name": "x", "type": "s8"},
    ]
    params["size_pair"] = [2, 3]
    table = b"\x00\x02"
    frame = b"\x02" + _Y + b"\x00" + _ATTR + b"\xe8" + _Y + b"\x80" + _ATTR + b"\xe8"
    ctx = PipelineContext()
    ctx.set(KEY_INPUTS, {"frames": table + frame})
    ctx.set(KEY_TILEMAP_SUBSPRITE_TILES, (1, 4))
    (got,) = engine.frames(engine.decode(table, params, ctx), params, ctx)
    assert [(p.across, p.down) for p in got] == [(1, 1), (4, 4)]
    assert engine.size_pair(params) == (2, 3)
