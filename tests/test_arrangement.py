"""Arrangement sub-step: block/row-interleave placement and the 2D byte walk.

These carry real regression risk — the slot↔cell mapping must reduce *exactly* to
row-major when blocks are off (or the ordinary view breaks), the interleave layout
must match how 8×16 sprite sheets are actually stored, and the 2D reflow is
bit-exact byte arithmetic. The trivial getters on ViewOptions are not tested.
"""

from __future__ import annotations

from celpix.core.arrangement import (
    ARRANGEMENT_PRESETS,
    BLOCK_ORDERS,
    BlockLayout,
    arrangement_preset_for,
    compose_window,
    reflow_2d,
    split_coverage,
    split_grid,
)
from celpix.core.index_grid import IndexGrid


def test_plain_layout_is_row_major_both_directions() -> None:
    # Blocks off must be identical to slot ↔ (slot % cols, slot // cols); the whole
    # existing view path relies on this reduction.
    layout = BlockLayout(columns=4)
    assert layout.is_plain
    for slot in range(12):
        assert layout.slot_to_cell(slot) == (slot % 4, slot // 4)
    for tx in range(4):
        for ty in range(3):
            assert layout.cell_to_slot(tx, ty) == ty * 4 + tx


def test_slot_and_cell_are_inverses_for_every_block_order() -> None:
    for order in BLOCK_ORDERS:
        layout = BlockLayout(
            columns=6, block_columns=2, block_rows=2, block_order=order
        )
        seen: set[tuple[int, int]] = set()
        for slot in range(24):  # 6 wide × 4 tall = 24 cells, one block-row is 12
            cell = layout.slot_to_cell(slot)
            assert cell not in seen  # the mapping is a bijection over the cells
            seen.add(cell)
            assert layout.cell_to_slot(*cell) == slot


def test_sequential_block_stacks_consecutive_tiles_vertically() -> None:
    # NES/GB 8×16: a 1×2 block, filled block-by-block, stacks tile i (top) over
    # tile i+1 (bottom); the next sprite lands in the next column.
    layout = BlockLayout(columns=4, block_rows=2)
    assert layout.slot_to_cell(0) == (0, 0)  # sprite 0 top
    assert layout.slot_to_cell(1) == (0, 1)  # sprite 0 bottom
    assert layout.slot_to_cell(2) == (1, 0)  # sprite 1 top
    assert layout.slot_to_cell(3) == (1, 1)  # sprite 1 bottom


def test_column_order_stacks_genesis_sprite_tiles() -> None:
    # Mega Drive / Neo Geo store a multi-tile sprite column-major ("first
    # vertically then horizontally"): a 2×2 sprite is tiles TL, BL, TR, BR — down
    # the first column, then the next — not the row-major TL, TR, BL, BR.
    layout = BlockLayout(columns=2, block_columns=2, block_rows=2, block_order="column")
    assert layout.slot_to_cell(0) == (0, 0)  # TL
    assert layout.slot_to_cell(1) == (0, 1)  # BL — down before across
    assert layout.slot_to_cell(2) == (1, 0)  # TR
    assert layout.slot_to_cell(3) == (1, 1)  # BR


def test_row_interleave_lays_all_tops_then_all_bottoms() -> None:
    # The other 8×16 storage: a whole row of sprite tops precedes the matching row
    # of bottoms. With a 1×2 block over 4 columns, tiles 0..3 are the top row and
    # tiles 4..7 the bottom row.
    layout = BlockLayout(columns=4, block_rows=2, block_order="row-interleave")
    assert [layout.slot_to_cell(s) for s in range(4)] == [
        (0, 0),
        (1, 0),
        (2, 0),
        (3, 0),
    ]
    assert [layout.slot_to_cell(s) for s in range(4, 8)] == [
        (0, 1),
        (1, 1),
        (2, 1),
        (3, 1),
    ]


def test_partial_width_block_column_has_no_slot() -> None:
    # 5 columns with 2-wide blocks: two whole blocks fill x0..3; the last column
    # (x=4) is past the last whole block, so no tile maps there.
    layout = BlockLayout(columns=5, block_columns=2)
    assert layout.cell_to_slot(4, 0) is None
    assert layout.cell_to_slot(3, 0) is not None


def test_compose_window_places_tiles_by_block_layout() -> None:
    # 1×1 tiles carrying their own index make placement directly assertable.
    tiles = [IndexGrid(1, 1, bytes([v])) for v in range(4)]
    layout = BlockLayout(columns=2, block_rows=2)  # one 1×2 block per column
    grid = compose_window(tiles, columns=2, first_tile=0, rows=2, layout=layout)
    assert (grid.get(0, 0), grid.get(0, 1)) == (0, 1)  # column 0 = tiles 0 (top), 1
    assert (grid.get(1, 0), grid.get(1, 1)) == (2, 3)  # column 1 = tiles 2, 3


def test_split_grid_inverts_compose_under_every_block_order() -> None:
    # Copy composes a run into an image and paste cuts it back apart; the pair
    # must be exact, or a round trip through an image editor scrambles tiles.
    for order in BLOCK_ORDERS:
        tiles = [IndexGrid(2, 2, bytes([v] * 4)) for v in range(8)]
        layout = BlockLayout(
            columns=4, block_columns=2, block_rows=2, block_order=order
        )
        grid = compose_window(tiles, columns=4, first_tile=0, rows=2, layout=layout)
        assert split_grid(grid, 2, 2, layout) == tiles


def test_split_grid_pads_a_partial_edge_tile() -> None:
    # A 3×3 image on a 2×2 grid: four tiles, the right/bottom edges zero-filled
    # rather than shrunk, so a codec always gets whole tiles.
    grid = IndexGrid(3, 3, bytes([7] * 9))
    tiles = split_grid(grid, 2, 2)
    assert len(tiles) == 4
    assert bytes(tiles[1].data) == bytes([7, 0, 7, 0])
    assert bytes(tiles[3].data) == bytes([7, 0, 0, 0])


def test_split_coverage_marks_the_padding_split_grid_invented() -> None:
    # Parallel to the tiles above: the same 3×3 image says how far it actually
    # reached, so a write can leave the pad alone instead of stamping it.
    assert split_coverage(3, 3, 2, 2) == [(2, 2), (1, 2), (2, 1), (1, 1)]
    # A block layout can send a slot below a one-tile-tall image entirely —
    # nothing of it is data, so it must read as covering nothing at all.
    layout = BlockLayout(columns=2, block_rows=2)
    assert split_coverage(4, 2, 2, 2, layout) == [(2, 2), (0, 0)]


def test_reflow_2d_gathers_strided_rows_into_contiguous_tiles() -> None:
    # 8-byte tiles, 8 rows → 1 byte/row; a 2-tile-wide bitmap interleaves the two
    # tiles' rows: [t0r0, t1r0, t0r1, t1r1, …]. Reflow must ungather them into two
    # contiguous 8-byte tiles.
    t0 = list(range(0x00, 0x08))
    t1 = list(range(0x10, 0x18))
    stored = bytearray()
    for r in range(8):
        stored.append(t0[r])
        stored.append(t1[r])
    out = reflow_2d(bytes(stored), bytes_per_tile=8, tile_height=8, columns=2)
    assert list(out) == t0 + t1


def test_reflow_2d_pads_partial_bitmap_row() -> None:
    # A window short of a whole bitmap-row (2 tiles × 8 bytes = 16) is padded up, so
    # the output is a whole number of tiles the codec can consume.
    out = reflow_2d(bytes(range(8)), bytes_per_tile=8, tile_height=8, columns=2)
    assert len(out) == 16  # padded to one full 2-tile bitmap-row


def test_reflow_2d_leaves_indivisible_tiles_untouched() -> None:
    # bytes_per_tile not a whole number of per-row chunks has no wide-bitmap reading.
    data = bytes(range(9))
    assert reflow_2d(data, bytes_per_tile=9, tile_height=2, columns=2) == data


def test_arrangement_presets_have_distinct_params() -> None:
    # The UI re-derives the Pattern selection from the four view values, so two
    # presets sharing a param tuple would make that lookup ambiguous (the first
    # would always shadow the second).
    seen = [p.params for p in ARRANGEMENT_PRESETS]
    assert len(seen) == len(set(seen))
    # Their block_order values must be real orders, or BlockLayout ignores them.
    for preset in ARRANGEMENT_PRESETS:
        assert preset.block_order in BLOCK_ORDERS


def test_arrangement_preset_lookup_round_trips_and_rejects_custom() -> None:
    # Every preset's own params resolve back to it (identity, since the lookup
    # returns the same frozen instance the UI stored in the combo)...
    for preset in ARRANGEMENT_PRESETS:
        assert arrangement_preset_for(*preset.params) is preset
    # ...and a combination no preset defines is a custom arrangement (None). 2×2
    # column *with* the 2D walk isn't the Mega Drive sprite preset (that's 1D).
    assert arrangement_preset_for(2, 2, "column", True) is None
    assert arrangement_preset_for(3, 1, "row", False) is None
