"""The sprite record ops: what add, remove, reorder and move mean per structure kind.

Driven by a fake codec rather than a builtin one, so a test states the
structural rule it guards and nothing about where a real format keeps its bits:
the fake packs x, y, the size bit and the drawn bit into ``Cell.flags``, plus a
frame mark and a header bit for the kinds that carry membership in the cells
(``docs/design/sprite-map.md`` §8.4).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from celpix.core.sprite import Frame, Subsprite, SubspriteLimits
from celpix.core.sprite_edit import (
    FIXED_REFUSAL,
    SpriteEditError,
    SweepPiece,
    add_subsprite,
    fit_footprint,
    fit_group_delta,
    frame_count,
    membership,
    move_subsprite,
    record_places,
    record_positions,
    reflect_group,
    remove_subsprites,
    reorder_subsprite,
    split_sweep,
    sweep_footprint,
)
from celpix.core.tilemap import Cell

BYTE = range(-128, 128)

# --- the fake codec ----------------------------------------------------------
# flags: bit 0 drawn, bit 1 large, bit 2 header, x s8 at 8, y s8 at 16, frame at 24.

_DRAWN, _LARGE, _HEADER = 1, 2, 4


def _s8(value: int) -> int:
    return value - 256 if value & 0x80 else value


def _fields(cell: Cell) -> dict:
    f = cell.flags
    return {
        "drawn": bool(f & _DRAWN),
        "large": bool(f & _LARGE),
        "header": bool(f & _HEADER),
        "x": _s8((f >> 8) & 0xFF),
        "y": _s8((f >> 16) & 0xFF),
        "frame": f >> 24,
    }


def _pack(*, x=0, y=0, large=False, drawn=True, header=False, frame=0) -> int:
    return (
        (_DRAWN if drawn else 0)
        | (_LARGE if large else 0)
        | (_HEADER if header else 0)
        | ((x & 0xFF) << 8)
        | ((y & 0xFF) << 16)
        | (frame << 24)
    )


def _cell(index, **fields) -> Cell:
    return Cell(index=index, flags=_pack(**fields))


def place(cell, *, x=None, y=None, size=None, drawn=None) -> Cell:
    f = _fields(cell)
    for key, value in (("x", x), ("y", y), ("drawn", drawn)):
        if value is not None:
            f[key] = value
    if size is not None:
        f["large"] = size == "large"
    return replace(cell, flags=_pack(**f))


class Assign:
    """Stamps each cell's frame mark and a header on each frame's first cell."""

    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    def __call__(self, cells, frames):
        self.calls.append(list(frames))
        out, seen = [], set()
        for cell, number in zip(cells, frames, strict=True):
            f = _fields(cell)
            f["frame"], f["header"] = number, number not in seen
            seen.add(number)
            out.append(replace(cell, flags=_pack(**f)))
        return out


def _piece(cell: Cell, record: int) -> Subsprite:
    f = _fields(cell)
    side = 2 if f["large"] else 1
    return Subsprite(
        x=f["x"], y=f["y"], index=cell.index, across=side, down=side, record=record
    )


def frames_of(cells, limits) -> list[Frame]:
    """What the fake codec's ``frames()`` would answer."""
    if limits.structure == "slots":
        stride = limits.per_frame
        return [
            tuple(
                _piece(cells[at], at)
                for at in range(start, min(start + stride, len(cells)))
                if _fields(cells[at])["drawn"]
            )
            for start in range(0, len(cells), stride)
        ]
    count = frame_count(cells, [], limits) if limits.structure == "counted" else 0
    grouped: dict[int, list[Subsprite]] = {}
    for at, cell in enumerate(cells):
        number = 0 if limits.structure == "run" else _fields(cell)["frame"]
        grouped.setdefault(number, []).append(_piece(cell, at))
    count = max(count, max(grouped, default=-1) + 1)
    return [tuple(grouped.get(number, ())) for number in range(count)]


SLOTS = SubspriteLimits("slots", BYTE, BYTE, ("small", "large"), per_frame=4)
COUNTED = SubspriteLimits("counted", BYTE, BYTE, per_frame=3, frames=4)
HEADED = SubspriteLimits("headed", BYTE, BYTE)
RUN = SubspriteLimits("run", BYTE, BYTE)
TABLE = SubspriteLimits("table", BYTE, BYTE)


def _framed(*frames_of_indices: list[int]) -> list[Cell]:
    """Cells for a counted/headed map: one list of tile indices per frame."""
    cells = [
        _cell(index, frame=number, header=k == 0)
        for number, indices in enumerate(frames_of_indices)
        for k, index in enumerate(indices)
    ]
    return cells


# --- slots -------------------------------------------------------------------


def _slots() -> list[Cell]:
    # Frame 0: A, a cleared slot holding residue, B, C.  Frame 1: D, three free.
    return [
        _cell(10, x=1),
        _cell(99, x=50, y=50, large=True, drawn=False),
        _cell(11, x=2),
        _cell(12, x=3),
        _cell(13),
        _cell(98, drawn=False),
        _cell(97, drawn=False),
        _cell(96, drawn=False),
    ]


def test_slots_add_writes_the_whole_template_over_the_slot_residue():
    cells = _slots()
    template = Cell(index=42, palette_row=3, flip_h=True, flags=_pack(large=True))
    out, record = add_subsprite(
        cells, frames_of(cells, SLOTS), SLOTS, 0, template,
        x=-5, y=7, place=place, assign=None,
    )  # fmt: skip
    assert record == 1
    assert len(out) == len(cells)
    new = out[1]
    assert (new.index, new.palette_row, new.flip_h) == (42, 3, True)
    f = _fields(new)
    assert (f["drawn"], f["large"], f["x"], f["y"]) == (True, True, -5, 7)
    assert out[:1] + out[2:] == cells[:1] + cells[2:]


def test_slots_add_refuses_a_full_frame_naming_the_count():
    cells = _slots()
    cells[1] = place(cells[1], drawn=True)
    with pytest.raises(SpriteEditError, match=r"^Frame 0 has no free slot \(4 of 4\)$"):
        add_subsprite(
            cells, frames_of(cells, SLOTS), SLOTS, 0, _cell(1),
            x=0, y=0, place=place, assign=None,
        )  # fmt: skip


def test_no_add_creates_a_frame():
    cells = _slots()
    frames = frames_of(cells, SLOTS)
    assert frame_count(cells, frames, SLOTS) == 2
    with pytest.raises(SpriteEditError, match="2 frame slots"):
        add_subsprite(
            cells, frames, SLOTS, 2, _cell(1), x=0, y=0, place=place, assign=None
        )
    headed = _framed([1], [2])
    with pytest.raises(SpriteEditError, match="not created"):
        add_subsprite(
            headed, frames_of(headed, HEADED), HEADED, 2, _cell(1),
            x=0, y=0, place=place, assign=Assign(),
        )  # fmt: skip


def test_slots_remove_clears_the_drawn_bit_and_moves_nothing():
    cells = _slots()
    out = remove_subsprites(
        cells, frames_of(cells, SLOTS), SLOTS, [2, 4], place=place, assign=None
    )
    assert len(out) == len(cells)
    assert out[2] == place(cells[2], drawn=False)
    assert out[4] == place(cells[4], drawn=False)
    assert [out[at] for at in (0, 1, 3, 5, 6, 7)] == [
        cells[at] for at in (0, 1, 3, 5, 6, 7)
    ]


def test_slots_an_undrawn_slot_is_not_a_piece():
    # Its residue reads as a perfectly good record; only the frames say it is not.
    cells = _slots()
    frames = frames_of(cells, SLOTS)
    with pytest.raises(SpriteEditError, match="not a drawn subsprite"):
        remove_subsprites(cells, frames, SLOTS, [1], place=place, assign=None)
    with pytest.raises(SpriteEditError, match="not a drawn subsprite"):
        reorder_subsprite(cells, frames, SLOTS, 1, 0, assign=None)


def test_slots_reorder_permutes_drawn_slots_and_leaves_undrawn_in_place():
    cells = _slots()
    out = reorder_subsprite(cells, frames_of(cells, SLOTS), SLOTS, 3, 0, assign=None)
    assert [c.index for c in out[:4]] == [12, 99, 10, 11]
    assert out[1] == cells[1]
    assert out[4:] == cells[4:]


def test_slots_move_clears_here_and_takes_the_first_free_slot_there():
    cells = _slots()
    frames = frames_of(cells, SLOTS)
    out, record = move_subsprite(cells, frames, SLOTS, 2, 1, place=place, assign=None)
    assert record == 5
    assert out[5] == cells[2]
    assert not _fields(out[2])["drawn"]
    full = list(cells)
    full[5:8] = [place(c, drawn=True) for c in full[5:8]]
    with pytest.raises(SpriteEditError, match=r"Frame 1 has no free slot \(4 of 4\)"):
        move_subsprite(
            full, frames_of(full, SLOTS), SLOTS, 0, 1, place=place, assign=None
        )


# --- counted -----------------------------------------------------------------


def test_counted_add_populates_an_empty_frame_where_it_would_sit():
    # Frames 0 and 2 hold pieces, 1 is empty: the new piece lands between them.
    cells = _framed([1, 2], [], [3])
    assign = Assign()
    frames = frames_of(cells, COUNTED)
    out, record = add_subsprite(
        cells, frames, COUNTED, 1, _cell(50, frame=3), x=4, y=4,
        place=place, assign=assign,
    )  # fmt: skip
    assert record == 2
    assert [c.index for c in out] == [1, 2, 50, 3]
    assert assign.calls == [[0, 0, 1, 2]]
    assert frame_count(out, frames_of(out, COUNTED), COUNTED) == 4


def test_counted_add_refuses_a_frame_at_its_ceiling():
    cells = _framed([1, 2, 3])
    with pytest.raises(SpriteEditError, match=r"^Frame 0 is full \(3 of 3\)$"):
        add_subsprite(
            cells, frames_of(cells, COUNTED), COUNTED, 0, _cell(9),
            x=0, y=0, place=place, assign=Assign(),
        )  # fmt: skip


def test_counted_remove_may_empty_a_frame():
    cells = _framed([1], [2, 3])
    assign = Assign()
    out = remove_subsprites(
        cells, frames_of(cells, COUNTED), COUNTED, [0], place=place, assign=assign
    )
    assert [c.index for c in out] == [2, 3]
    assert assign.calls == [[1, 1]]


def test_counted_reorder_splices_within_the_frame_run():
    cells = _framed([1], [2, 3, 4], [5])
    assign = Assign()
    out = reorder_subsprite(
        cells, frames_of(cells, COUNTED), COUNTED, 1, 99, assign=assign
    )
    assert [c.index for c in out] == [1, 3, 4, 2, 5]
    assert assign.calls == [[0, 1, 1, 1, 2]]


def test_counted_move_splices_to_the_end_of_the_target_run():
    cells = _framed([1, 2], [3], [4])
    assign = Assign()
    out, record = move_subsprite(
        cells, frames_of(cells, COUNTED), COUNTED, 0, 1, place=place, assign=assign
    )
    assert [c.index for c in out] == [2, 3, 1, 4]
    assert record == 2
    assert assign.calls == [[0, 1, 1, 2]]


def test_counted_membership_refuses_a_cell_no_frame_names():
    cells = _framed([1, 2])
    frames = [(Subsprite(record=0),)]
    with pytest.raises(SpriteEditError, match="Record 1 is in no frame"):
        membership(cells, frames, COUNTED)


# --- headed ------------------------------------------------------------------


def test_headed_remove_refuses_to_empty_a_frame():
    cells = _framed([1, 2], [3])
    with pytest.raises(
        SpriteEditError,
        match="^A frame here holds at least one record - not removed$",
    ):
        remove_subsprites(
            cells, frames_of(cells, HEADED), HEADED, [0, 2], place=place,
            assign=Assign(),
        )  # fmt: skip


def test_headed_remove_of_a_first_record_moves_the_header():
    cells = _framed([1, 2], [3])
    out = remove_subsprites(
        cells, frames_of(cells, HEADED), HEADED, [0], place=place, assign=Assign()
    )
    assert [(c.index, _fields(c)["header"]) for c in out] == [(2, True), (3, True)]


def test_headed_move_refuses_to_empty_the_source_frame():
    cells = _framed([1, 2], [3])
    with pytest.raises(SpriteEditError, match="at least one record - not moved"):
        move_subsprite(
            cells, frames_of(cells, HEADED), HEADED, 2, 0, place=place,
            assign=Assign(),
        )  # fmt: skip


# --- run and table -----------------------------------------------------------


def test_run_add_appends_remove_shrinks_and_frames_stay_one():
    cells = [_cell(1), _cell(2)]
    frames = frames_of(cells, RUN)
    out, record = add_subsprite(
        cells, frames, RUN, 0, _cell(3), x=0, y=0, place=place, assign=None
    )
    assert (record, [c.index for c in out]) == (2, [1, 2, 3])
    with pytest.raises(SpriteEditError, match="one frame"):
        add_subsprite(
            cells, frames, RUN, 1, _cell(3), x=0, y=0, place=place, assign=None
        )
    out = remove_subsprites(cells, frames, RUN, [0], place=place, assign=None)
    assert [c.index for c in out] == [2]


def test_table_refuses_every_structural_op():
    cells = [_cell(1)]
    frames = [(Subsprite(),)]
    with pytest.raises(SpriteEditError, match="^Frames here are not editable$"):
        add_subsprite(
            cells, frames, TABLE, 0, _cell(1), x=0, y=0, place=place, assign=None
        )
    with pytest.raises(SpriteEditError, match="not editable"):
        remove_subsprites(cells, frames, TABLE, [0], place=place, assign=None)


def test_fixed_refuses_every_structural_and_geometric_op():
    # Two frames of two pieces at slots the drawing routine fixes: the cells
    # are editable (frame_count still answers), but nothing moves or counts.
    fixed = SubspriteLimits("fixed", range(0), range(0))
    cells = [_cell(1), _cell(2), _cell(3), _cell(4)]
    frames = [
        (Subsprite(record=0), Subsprite(x=8, record=1)),
        (Subsprite(record=2), Subsprite(x=8, record=3)),
    ]
    assert frame_count(cells, frames, fixed) == 2
    refused = [
        lambda: membership(cells, frames, fixed),
        lambda: add_subsprite(
            cells, frames, fixed, 0, _cell(9), x=0, y=0, place=place, assign=None
        ),
        lambda: remove_subsprites(cells, frames, fixed, [0], place=place, assign=None),
        lambda: reorder_subsprite(cells, frames, fixed, 1, 0, assign=None),
        lambda: move_subsprite(cells, frames, fixed, 0, 1, place=place, assign=None),
        lambda: fit_group_delta(list(frames[0]), 1, 0, fixed),
        lambda: fit_group_delta([], 0, 0, fixed),
    ]
    for op in refused:
        with pytest.raises(SpriteEditError) as caught:
            op()
        assert str(caught.value) == FIXED_REFUSAL


# --- geometry ----------------------------------------------------------------


def test_a_record_drawn_twice_resolves_to_its_first_place():
    """A ``fixed`` word drawn in two frames: one place answers with the first,
    every place lists both, in draw order."""
    frames = [
        (Subsprite(0, 0, 1, record=0), Subsprite(8, 0, 2, record=1)),
        (Subsprite(8, 0, 2, record=1), Subsprite(0, 0, 1, record=0)),
    ]
    assert record_positions(frames) == {0: (0, 0), 1: (0, 1)}
    assert record_places(frames) == {0: [(0, 0), (1, 1)], 1: [(0, 1), (1, 0)]}


def test_group_delta_is_shrunk_for_the_group_not_per_piece():
    pieces = [Subsprite(x=-120, y=0), Subsprite(x=100, y=0)]
    limits = SubspriteLimits("slots", BYTE, BYTE, per_frame=4)
    assert fit_group_delta(pieces, -20, 0, limits) == (-8, 0)
    assert fit_group_delta(pieces, 30, -5, limits) == (27, -5)
    outside = [*pieces, Subsprite(x=0, y=200)]
    with pytest.raises(SpriteEditError, match=r"at \(\+0, \+200\) .*\(-128\.\.127\)$"):
        fit_group_delta(outside, 1, 0, limits)


def test_reflect_mixed_sizes_keeps_the_box_and_swaps_sides():
    # A 2x2 at x=0 beside a 1x1 at x=16: the box is 0..24 before and after.
    pieces = [Subsprite(x=0, y=4, across=2, down=2), Subsprite(x=16, y=4)]
    assert reflect_group(pieces, 8, 8, horizontal=True) == [(8, 4), (0, 4)]
    assert reflect_group(pieces, 8, 8, horizontal=False) == [(0, 4), (16, 12)]
    assert reflect_group(pieces, 8, 8, horizontal=True, pivot=0) == [
        (-16, 4),
        (-24, 4),
    ]


# --- the tile source: the sweep and the footprint ----------------------------

_BIT = [("small", (1, 1)), ("large", (2, 2))]
_RECTS = [((a, d), (a, d)) for a in range(1, 5) for d in range(1, 5)]


def test_a_row_major_sweep_is_read_as_the_picture_on_the_sheet() -> None:
    """On a 16-wide sheet a 2x2 is the four tiles a piece at its corner draws;
    a square no choice fits lays the smallest choice on its own, and a hole, a
    sheet not laid out at the stride, or a pair whose small is itself 2x2 each
    fall back the same way."""
    assert split_sweep([[0, 1, 2], [16, 17, 18]], _BIT, column_major=False) == [
        SweepPiece(0, "large", (2, 2), 0, 0),
        SweepPiece(2, "small", (1, 1), 2, 0),
        SweepPiece(18, "small", (1, 1), 2, 1),
    ]
    assert split_sweep([[0, 1], [16, None]], _BIT, column_major=False) == [
        SweepPiece(0, "small", (1, 1), 0, 0),
        SweepPiece(1, "small", (1, 1), 1, 0),
        SweepPiece(16, "small", (1, 1), 0, 1),
    ]
    assert split_sweep([[0, 1], [8, 9]], _BIT, column_major=False) == [
        SweepPiece(tile, "small", (1, 1), tile % 8, tile // 8) for tile in (0, 1, 8, 9)
    ]
    wide = [("small", (2, 2)), ("large", (4, 4))]
    assert split_sweep([[0, 1], [16, 17]], wide, column_major=False) == [
        SweepPiece(0, "small", (2, 2), 0, 0)
    ]
    assert split_sweep([[5]], wide, column_major=False) == [
        SweepPiece(5, "small", (2, 2), 0, 0)
    ]
    # No size field: every square is a piece of the record's own size.
    assert split_sweep([[3, 4]], [], column_major=False) == [
        SweepPiece(3, None, (1, 1), 0, 0),
        SweepPiece(4, None, (1, 1), 1, 0),
    ]


def test_a_column_major_sweep_is_runs_of_consecutive_tiles() -> None:
    """A Mega Drive piece spends consecutive tiles, so a strip of four is one
    2x2 - the squarest of the three shapes that spend four - eight is a 2x4
    (taller before wider) and ten a 3x3 with a 1x1 beside it; a gap in the IDs
    ends the run, and a second row of the sweep is a second strip under the
    first."""
    assert split_sweep([[0, 1, 2, 3]], _RECTS, column_major=True) == [
        SweepPiece(0, (2, 2), (2, 2), 0, 0)
    ]
    assert split_sweep([list(range(8))], _RECTS, column_major=True) == [
        SweepPiece(0, (2, 4), (2, 4), 0, 0)
    ]
    assert split_sweep([list(range(10))], _RECTS, column_major=True) == [
        SweepPiece(0, (3, 3), (3, 3), 0, 0),
        SweepPiece(9, (1, 1), (1, 1), 3, 0),
    ]
    assert split_sweep([[0, 1, 5, 6]], _RECTS, column_major=True) == [
        SweepPiece(0, (1, 2), (1, 2), 0, 0),
        SweepPiece(5, (1, 2), (1, 2), 1, 0),
    ]
    two = split_sweep([[0, 1, 2, 3], [16, 17, 18, 19]], _RECTS, column_major=True)
    assert two == [
        SweepPiece(0, (2, 2), (2, 2), 0, 0),
        SweepPiece(16, (2, 2), (2, 2), 0, 2),
    ]
    assert sweep_footprint(two, column_major=True) == frozenset(range(4)) | frozenset(
        range(16, 20)
    )
    assert sweep_footprint(
        [SweepPiece(1, "large", (2, 2), 0, 0)], column_major=False
    ) == frozenset({1, 2, 17, 18})


def test_a_footprint_past_the_bank_is_refused_by_name() -> None:
    """The first tile a piece spends outside the reach is named, with the
    bank's last where it is past it - along the piece's own walk."""
    fit_footprint(Subsprite(index=14, across=2, down=2), range(32))
    past = (
        r"2x2 piece at tile \$F spends tile \$20, past the tile source's last \(\$1F\)"
    )
    with pytest.raises(SpriteEditError, match=past):
        fit_footprint(Subsprite(index=15, across=2, down=2), range(32))
    with pytest.raises(SpriteEditError, match=r"tile \$20"):
        fit_footprint(
            Subsprite(index=30, across=2, down=2, column_major=True), range(32)
        )
    with pytest.raises(SpriteEditError, match="does not reach"):
        fit_footprint(Subsprite(index=14, across=2, down=2), [14, 15, 31])
