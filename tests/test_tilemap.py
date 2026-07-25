"""Virtual tile rearrangement: the permutation, and the edits it redirects.

The Qt-free half checks the invariant everything else leans on — a
:class:`TileMap` is always a permutation, so every display position resolves to
exactly one tile. The window half checks the payoff: a tile shown somewhere else
is read and written *there*, and the bytes land at its real home.
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from celpix.core import transform
from celpix.core.index_grid import IndexGrid
from celpix.core.tilemap import (
    TILE_FLIP_BOTH,
    TILE_FLIP_H,
    TILE_FLIP_V,
    TILE_ORIENT_MASK,
    TILE_ROTATE_CCW,
    TILE_ROTATE_CW,
    TILE_TRANSPOSE,
    TileMap,
    apply_orientation,
    coalesce_runs,
    compose_orientation,
    invert_orientation,
    unapply_orientation,
)
from celpix.ui.main_window import MainWindow
from celpix.ui.main_window.selection import SelectionShape

# transform.OP_* are destructive TransformOps; tilemap's flags are the display
# orientation. Kept apart so the two never read as the same thing.
from celpix.ui.main_window.transform import OP_FLIP_H, OP_ROTATE_CW
from celpix.ui.tools import EditMode, Tool


# -- the permutation -------------------------------------------------------
def test_swap_is_an_involution() -> None:
    once = TileMap().swap(3, 17)
    assert once.actual(3) == 17 and once.actual(17) == 3
    assert once.swap(3, 17).is_identity()


def test_actual_and_virtual_are_inverses_after_composed_swaps() -> None:
    """Two overlapping swaps make a 3-cycle, not a pair of transpositions —
    the case where a naive forward/reverse dict would come apart."""
    tile_map = TileMap().swap(0, 1).swap(1, 2)
    assert not tile_map.is_identity()
    for index in range(4):
        assert tile_map.virtual(tile_map.actual(index)) == index
        assert tile_map.actual(tile_map.virtual(index)) == index


def test_from_pairs_drops_identity_so_equal_rearrangements_compare_equal() -> None:
    assert TileMap.from_pairs([(4, 4)]).is_identity()
    assert TileMap.from_pairs([(1, 2), (2, 1), (9, 9)]) == TileMap().swap(1, 2)


@pytest.mark.parametrize(
    "pairs",
    [
        [(1, 2)],  # unbalanced: tile 1 is displaced with nowhere to go
        [(1, 2), (3, 2)],  # two positions claiming one tile
    ],
)
def test_a_non_permutation_is_refused(pairs) -> None:
    with pytest.raises(ValueError):
        TileMap(tuple(pairs))


def test_swap_many_refuses_overlapping_moves() -> None:
    """A block drop onto its own source: the exchanges stop being independent,
    so the gesture is refused rather than quietly becoming a rotation."""
    with pytest.raises(ValueError):
        TileMap().swap_many([(1, 2), (2, 3)])


def test_bounded_drops_whole_cycles_that_reach_past_the_end() -> None:
    """A shorter document (a codec with bigger tiles) can strand part of a
    cycle; dropping only the stranded member would leave a broken map."""
    tile_map = TileMap().swap(1, 2).swap(4, 30)
    assert tile_map.bounded(10) == TileMap().swap(1, 2)
    assert TileMap().swap(0, 1).swap(1, 9).bounded(5).is_identity()


def test_a_flip_toggles_and_follows_its_tile_through_a_swap() -> None:
    """Orientations are keyed by the tile, not the display position — you turned
    it to make the art read, so moving it has to keep it reading."""
    flipped = TileMap().oriented([3], TILE_FLIP_H)
    assert flipped.orient_of(3) == TILE_FLIP_H and not flipped.is_identity()
    assert flipped.oriented([3], TILE_FLIP_H).is_identity()  # the buttons compose
    assert flipped.oriented([3], TILE_FLIP_V).orient_of(3) == TILE_FLIP_BOTH
    moved = flipped.swap(3, 20)
    assert moved.orient_of(3) == TILE_FLIP_H  # still the tile's own property
    assert moved.virtual(3) == 20  # ...now shown over here


def test_an_orientation_alone_is_not_identity() -> None:
    """A map with no moves still has to take the gather path, or the orientation
    would silently not render."""
    assert not TileMap().oriented([1], TILE_FLIP_V).is_identity()


def test_bounded_drops_an_out_of_range_orientation() -> None:
    assert TileMap().oriented([2, 40], TILE_FLIP_H).bounded(10) == TileMap().oriented(
        [2], TILE_FLIP_H
    )


def test_four_quarter_turns_come_back_to_where_they_started() -> None:
    """The buttons compose onto what a tile already carries, so pressing one
    repeatedly walks the orientations rather than fighting the stored flags."""
    tile_map = TileMap()
    for _ in range(3):
        tile_map = tile_map.oriented([1], TILE_ROTATE_CW)
        assert not tile_map.is_identity()
    assert tile_map.oriented([1], TILE_ROTATE_CW).is_identity()


def _ramp(width: int = 8, height: int = 8) -> IndexGrid:
    """A grid whose every pixel is distinct, so any transform is detectable."""
    grid = IndexGrid(width, height)
    for y in range(height):
        for x in range(width):
            grid.set(x, y, (y * width + x) & 0xFF)
    return grid


def test_the_orientations_are_a_group_over_the_pixels_they_move() -> None:
    """The whole basis of composing: one stored orientation has to do exactly what
    the sequence of button presses that built it would have done, for every pair of
    the eight. Get this wrong and a second press on a turned tile mirrors the wrong
    axis — and the map, being canonical, has no record of how it got there."""
    grid = _ramp()
    every = range(TILE_ORIENT_MASK + 1)
    for first in every:
        for second in every:
            once = apply_orientation(grid, compose_orientation(second, first))
            twice = apply_orientation(apply_orientation(grid, first), second)
            assert once.data == twice.data, (first, second)
    # ...and all eight are distinct, so none of the three bits is redundant.
    assert len({bytes(apply_orientation(grid, f).data) for f in every}) == 8


def test_the_two_rotations_are_the_transforms_they_are_named_for() -> None:
    """The display orientation and the destructive transform must agree, or the
    same button would turn a tile one way in the file and the other on screen."""
    grid = _ramp()
    assert (
        apply_orientation(grid, TILE_ROTATE_CW).data == transform.rotate_cw(grid).data
    )
    assert (
        apply_orientation(grid, TILE_ROTATE_CCW).data == transform.rotate_ccw(grid).data
    )


def test_unapply_orientation_returns_every_orientation_to_storage() -> None:
    """The write path's half. A mirror is its own inverse but a turn is not, so
    the read and write directions are two functions that must not disagree: an
    edit made on a turned tile has to land back the way the file holds it."""
    grid = _ramp()
    for flags in range(TILE_ORIENT_MASK + 1):
        shown = apply_orientation(grid, flags)
        assert unapply_orientation(shown, flags).data == grid.data
        assert compose_orientation(invert_orientation(flags), flags) == 0


def test_a_turn_is_ignored_on_a_tile_that_is_not_square() -> None:
    """A turn would swap the tile's dimensions, so it could neither be shown in
    the cell it came from nor written back to the bytes it came from. It is
    ignored — in *both* directions, or the round trip would not be exact — and
    kept in the map, so a codec with square tiles brings it back."""
    tall = _ramp(8, 16)
    assert apply_orientation(tall, TILE_ROTATE_CW).data == tall.data
    assert unapply_orientation(tall, TILE_ROTATE_CW).data == tall.data
    assert apply_orientation(tall, TILE_TRANSPOSE).data == tall.data
    # Mirrors are unaffected: they keep the tile's shape.
    assert (
        apply_orientation(tall, TILE_FLIP_H).data
        == transform.flip_horizontal(tall).data
    )


def test_coalesce_runs_merges_small_gaps_but_not_distant_tiles() -> None:
    assert coalesce_runs([5, 6, 7]) == [(5, 3)]
    assert coalesce_runs([0, 3], gap=8) == [(0, 4)]  # cheaper as one decode
    assert coalesce_runs([0, 100], gap=8) == [(0, 1), (100, 1)]
    assert coalesce_runs([]) == []


# -- the window ------------------------------------------------------------
def _window(qtbot, tmp_path, tiles: int = 64):
    """A window with a 4bpp file open, each tile's bytes distinct."""
    px = tmp_path / "s.4bpp.sfc"
    px.write_bytes(bytes((i * 7 + 3) & 0xFF for i in range(32 * tiles)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._columns.setValue(8)
    window._rows.setValue(8)
    return window


def _tile_bytes(window, index: int) -> bytes:
    return window._doc.pixel_data[index * 32 : (index + 1) * 32]


def test_a_rearranged_position_reads_the_tile_it_was_given(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    plain = window._decode_run(0, 4)
    window._set_tile_map(TileMap().swap(1, 40))
    swapped = window._decode_run(0, 4)
    assert swapped[1] == window._decode_actual_run(40, 1)[0]
    assert swapped[0] == plain[0] and swapped[2] == plain[2]
    # ...and the swap runs both ways: position 40 now shows the tile from 1.
    assert window._decode_run(40, 1)[0] == plain[1]


def test_the_toggle_takes_the_map_out_of_the_read_path(qtbot, tmp_path) -> None:
    """Off is not merely a different picture: the map stops applying, so an
    edit made while it is off lands where the file says it should."""
    window = _window(qtbot, tmp_path)
    window._set_tile_map(TileMap().swap(1, 40))
    assert window._active_tile_map().actual(1) == 40
    window._show_rearranged_action.setChecked(False)
    assert window._active_tile_map().is_identity()
    assert window._decode_run(0, 2) == window._decode_actual_run(0, 2)


def test_r_arms_the_tool_and_shift_r_swaps_the_view(qtbot, tmp_path) -> None:
    """Both keys go through their actions, so they carry the actions' side
    effects: arming forces the rearranged view back on, and turning that view off
    disarms the tool."""
    window = _window(qtbot, tmp_path)
    window._toggle_show_rearranged()
    assert not window._show_rearranged

    window._toggle_rearranging()
    assert window._rearranging
    assert window._show_rearranged  # arming brought the view back with it

    window._toggle_show_rearranged()
    assert not window._show_rearranged and not window._rearranging


def test_both_switches_are_dead_with_nothing_open(qtbot, tmp_path, monkeypatch) -> None:
    """They are shown in the Edit menu as well as on the transform bar, and a
    menu row does not inherit that bar's disabled state — so the no-document
    state has to reach the actions themselves, on a fresh window and again when
    the last entry closes."""
    from PySide6.QtWidgets import QMessageBox

    def switches(win) -> list[bool]:
        return [
            win._rearrange_action.isEnabled(),
            win._show_rearranged_action.isEnabled(),
        ]

    fresh = MainWindow()
    qtbot.addWidget(fresh)
    assert switches(fresh) == [False, False]

    window = _window(qtbot, tmp_path)
    assert switches(window) == [True, True]
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    window._remove_entry(window._workspace.current)
    assert switches(window) == [False, False]


def test_the_rearrange_keys_are_inert_under_a_2d_pattern(qtbot, tmp_path) -> None:
    """The 2D lockout disables both actions, and the keys press those actions —
    so the lockout covers the keyboard without knowing the keys exist."""
    window = _window(qtbot, tmp_path)
    window._two_d.setChecked(True)
    window._toggle_rearranging()
    assert not window._rearranging


def test_a_pixel_edit_writes_back_to_the_tiles_real_home(qtbot, tmp_path) -> None:
    """The whole point: paint at the position a tile is *shown*, and its own
    bytes change — the tile that normally lives there is untouched."""
    window = _window(qtbot, tmp_path)
    window._set_edit_mode(EditMode.PIXEL)
    window._on_tool_selected(Tool.PENCIL)
    window._palette_panel.select_index(5)
    window._set_tile_map(TileMap().swap(1, 40))
    before_shown, before_real = _tile_bytes(window, 1), _tile_bytes(window, 40)
    # Tile slot 1 is the second cell of the top row: paint a pixel inside it.
    window._on_pixel_pressed(9, 1, Qt.MouseButton.LeftButton)
    window._on_pixel_released(9, 1)
    assert _tile_bytes(window, 40) != before_real
    assert _tile_bytes(window, 1) == before_shown


def test_a_flipped_tile_renders_mirrored_but_keeps_its_bytes(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    stored = window._decode_actual_run(3, 1)[0]
    before = _tile_bytes(window, 3)
    window._set_tile_map(TileMap().oriented([3], TILE_FLIP_H))
    assert window._decode_run(3, 1)[0] == transform.flip_horizontal(stored)
    assert _tile_bytes(window, 3) == before  # display only — nothing written


def test_an_edit_on_a_flipped_tile_lands_unflipped(qtbot, tmp_path) -> None:
    """The one that really matters. Paint the displayed top-left of a mirrored
    tile: the *stored* top-right must change. Miss the unflip and the mirror
    bakes into the file — flipped on disk and still flipped on screen."""
    window = _window(qtbot, tmp_path)
    window._set_tile_map(TileMap().oriented([3], TILE_FLIP_H))
    shown = window._decode_run(3, 1)[0]
    edited = type(shown)(shown.width, shown.height, bytes(shown.data))
    edited.set(0, 0, 7)
    window._apply_tile_edit(3, [edited], "paint")
    assert window._decode_actual_run(3, 1)[0].get(7, 0) == 7  # stored, mirrored back
    assert window._decode_run(3, 1)[0].get(0, 0) == 7  # reads back where painted


def test_a_turned_tile_renders_rotated_and_an_edit_lands_unturned(
    qtbot, tmp_path
) -> None:
    """The flip test's harder twin. A quarter turn is not its own inverse, so the
    write path has to turn back the *other* way: paint the displayed top-left of a
    tile shown rotated right, and the pixel the file gains is its bottom-left. Get
    the direction wrong and the edit lands at 180° to where it belongs."""
    window = _window(qtbot, tmp_path)
    stored = window._decode_actual_run(3, 1)[0]
    before = _tile_bytes(window, 3)
    window._set_tile_map(TileMap().oriented([3], TILE_ROTATE_CW))
    shown = window._decode_run(3, 1)[0]
    assert shown == transform.rotate_cw(stored)
    assert _tile_bytes(window, 3) == before  # display only — nothing written

    edited = type(shown)(shown.width, shown.height, bytes(shown.data))
    edited.set(0, 0, 7)
    window._apply_tile_edit(3, [edited], "paint")
    assert window._decode_actual_run(3, 1)[0].get(0, 7) == 7  # stored, turned back
    assert window._decode_run(3, 1)[0].get(0, 0) == 7  # reads back where painted


def test_the_rotate_buttons_need_a_square_tile(qtbot, tmp_path) -> None:
    """A turn swaps the tile's width and height, so on a tile that isn't square it
    has nowhere to land — the mirrors are unaffected. Same rule as the destructive
    groups, and it has to reach the block pair as well as the per-tile one."""
    window = _window(qtbot, tmp_path)
    window._set_rearranging(True)
    window._select_tiles(0, 0)
    tile, block = window._rearrange_group, window._rearrange_block_group
    assert tile.rotate_cw.isEnabled() and block.rotate_cw.isEnabled()

    window._doc.tile_height = 16  # an 8×16 codec's tile
    window._sync_rearrange_actions()
    assert tile.flip_h.isEnabled() and block.flip_h.isEnabled()
    assert not tile.rotate_cw.isEnabled() and not block.rotate_ccw.isEnabled()
    # ...and the key can't get past the disabled button either.
    assert window._transform_key(Qt.Key.Key_C, False, False)
    assert window._tile_map.is_identity()


def test_flipping_the_selection_is_an_undoable_step(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._set_rearranging(True)
    window._select_tiles(2, 3)
    window._orient_rearranged(TILE_FLIP_V)
    assert window._tile_map == TileMap().oriented([2, 3], TILE_FLIP_V)
    window._undo_stack.undo()
    assert window._tile_map.is_identity()


def test_transform_keys_orient_the_carried_tile_and_commit_with_the_drop(
    qtbot, tmp_path
) -> None:
    """Mid-drag the keys retarget to what is in the air, and the orientation rides
    along to the drop as part of that one step. Two presses compose into the single
    orientation that gets stored, so the second acts on the *displayed* tile."""
    window = _window(qtbot, tmp_path)
    window._set_rearranging(True)
    window._on_rearrange_started(0)
    assert window._transform_key(Qt.Key.Key_H, False, False)
    assert window._rearrange_drag.orient == TILE_FLIP_H
    assert window._transform_key(Qt.Key.Key_C, False, False)  # rotate right
    want = compose_orientation(TILE_ROTATE_CW, TILE_FLIP_H)
    assert want & TILE_TRANSPOSE  # a mirror plus a turn is still a turn
    assert window._rearrange_drag.orient == want
    assert window._tile_map.is_identity()  # pending, not committed
    window._on_rearrange_dropped(3)
    assert window._tile_map == TileMap().swap(0, 3).oriented([0], want)
    window._undo_stack.undo()
    assert window._tile_map.is_identity()  # move and orientation revert together


def test_picking_a_tile_up_and_putting_it_back_flipped_is_a_step(
    qtbot, tmp_path
) -> None:
    """A drop that only flips still counts — that is the natural way to mirror a
    tile in place, and a no-move drop would otherwise be discarded."""
    window = _window(qtbot, tmp_path)
    window._set_rearranging(True)
    window._on_rearrange_started(5)
    window._transform_key(Qt.Key.Key_V, False, False)
    window._on_rearrange_dropped(5)  # back where it came from
    assert window._tile_map == TileMap().oriented([5], TILE_FLIP_V)


def test_the_flip_key_follows_the_group_on_the_transform_bar(qtbot, tmp_path) -> None:
    """H is the bar's key, not the tool's: with the tool off it presses the
    destructive Tile flip, and only while armed does it write the display map."""
    window = _window(qtbot, tmp_path)
    window._select_tiles(2, 2)
    before = _tile_bytes(window, 2)
    assert window._transform_key(Qt.Key.Key_H, False, False)
    assert window._tile_map.is_identity()  # nothing display-only happened...
    assert _tile_bytes(window, 2) != before  # ...the pixels were rewritten

    window._set_rearranging(True)
    after_edit = _tile_bytes(window, 2)
    assert window._transform_key(Qt.Key.Key_H, False, False)
    assert window._tile_map == TileMap().oriented([2], TILE_FLIP_H)
    assert _tile_bytes(window, 2) == after_edit  # no byte moved this time


@pytest.mark.parametrize("op", [OP_FLIP_H, OP_ROTATE_CW])
def test_a_rearranged_block_transform_looks_like_the_destructive_one(
    qtbot, tmp_path, op
) -> None:
    """The whole promise of the feature: the picture reads identically to a real
    block transform, while every byte stays exactly where and how the file had it.
    Both halves have to agree for a rotation too — the tiles' own turn *and* the
    permutation of their positions, which is where a wrong turn direction shows up
    as a block that comes apart rather than one that turns."""
    window = _window(qtbot, tmp_path)
    window._block_cols.setValue(2)
    window._block_rows.setValue(2)
    window._select_tiles(0, 0)  # a lone tile expands to its 2x2 arrangement block

    # What the *destructive* block transform would put on screen, in a throwaway
    # window.
    reference = _window(qtbot, tmp_path)
    reference._block_cols.setValue(2)
    reference._block_rows.setValue(2)
    reference._select_tiles(0, 0)
    reference._transform_block(op)
    want = reference._decode_run(0, 4)

    before = [_tile_bytes(window, i) for i in range(4)]
    window._set_rearranging(True)
    window._orient_rearranged_block(op)
    assert window._decode_run(0, 4) == want  # same picture...
    assert [_tile_bytes(window, i) for i in range(4)] == before  # ...untouched bytes


def test_a_block_flip_permutes_positions_and_flips_the_tiles(qtbot, tmp_path) -> None:
    """Both halves land in the map: cells 0/1 and 2/3 exchange across the mirror,
    and all four tiles carry the flip flag."""
    window = _window(qtbot, tmp_path)
    window._block_cols.setValue(2)
    window._block_rows.setValue(2)
    window._select_tiles(0, 0)
    window._set_rearranging(True)
    window._orient_rearranged_block(OP_FLIP_H)
    assert window._tile_map == TileMap().swap(0, 1).swap(2, 3).oriented(
        [0, 1, 2, 3], TILE_FLIP_H
    )
    window._undo_stack.undo()
    assert window._tile_map.is_identity()  # positions and flips revert together


def test_shift_h_and_v_drive_the_block_flip(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._block_cols.setValue(2)
    window._block_rows.setValue(2)
    window._select_tiles(0, 0)
    window._set_rearranging(True)
    assert window._transform_key(Qt.Key.Key_H, True, False)
    assert window._tile_map.pairs  # positions moved: a block flip, not a tile one
    # Bare H is still the per-tile flip, which never moves anything.
    window._undo_stack.undo()
    assert window._transform_key(Qt.Key.Key_H, False, False)
    assert not window._tile_map.pairs and window._tile_map.orientations


def test_a_rearrange_drop_is_one_undo_step(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._set_rearranging(True)
    window._on_rearrange_started(0)  # cell (0, 0)
    window._on_rearrange_dropped(3)  # cell (3, 0)
    assert window._tile_map == TileMap().swap(0, 3)
    window._undo_stack.undo()
    assert window._tile_map.is_identity()
    window._undo_stack.redo()
    assert window._tile_map == TileMap().swap(0, 3)


def test_a_drop_on_the_grabbed_cell_is_not_a_step(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._set_rearranging(True)
    window._on_rearrange_started(5)
    window._on_rearrange_dropped(5)
    assert window._tile_map.is_identity()
    assert window._undo_stack.count() == 1  # only the file-open command


def test_escape_abandons_a_drag_in_flight(qtbot, tmp_path) -> None:
    """Armed over pixel mode, Escape must reach the drag first — the pixel
    handler would otherwise stamp the float and strand the carried tile."""
    window = _window(qtbot, tmp_path)
    window._set_edit_mode(EditMode.PIXEL)
    window._set_rearranging(True)
    window._on_rearrange_started(0)
    window._on_rearrange_moved(3)
    assert window._rearrange_drag is not None
    assert window._rearrange_key(Qt.Key.Key_Escape, False, False)
    assert window._rearrange_drag is None
    assert window._tile_map.is_identity()


def test_dragging_a_selection_moves_the_whole_block_and_the_selection(
    qtbot, tmp_path
) -> None:
    """A multi-tile selection is carried as a block, each cell swapping with the
    one the same distance from the drop as it was from the grab — and the
    selection lands with it, so the next gesture acts on the tiles just moved
    rather than on the ones they swapped with."""
    window = _window(qtbot, tmp_path)
    window._set_rearranging(True)
    window._on_tiles_selected(0, 1)  # a 2x1 rectangle: the tool forces Rectangle
    window._on_rearrange_started(0)
    window._on_rearrange_dropped(4)  # shift right by four cells
    assert window._tile_map == TileMap().swap_many([(0, 4), (1, 5)])
    assert window._selection_tiles() == [4, 5]


def test_a_block_drop_overlapping_its_source_is_refused(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._set_rearranging(True)
    window._on_tiles_selected(0, 2)
    window._on_rearrange_started(0)
    window._on_rearrange_dropped(1)  # one cell right — destinations overlap
    assert window._tile_map.is_identity()
    assert window._selection_tiles() == [0, 1, 2]  # nothing moved, nor the selection


def test_a_tile_transform_follows_the_view_too(qtbot, tmp_path) -> None:
    """Tile-mode operations go through the same choke point as painting, so
    flipping "this tile" flips the one on screen, at its real home."""
    window = _window(qtbot, tmp_path)
    window._set_tile_map(TileMap().swap(1, 40))
    window._select_tiles(1, 1)
    before_shown, before_real = _tile_bytes(window, 1), _tile_bytes(window, 40)
    window._transform_tiles(OP_FLIP_H)
    assert _tile_bytes(window, 40) != before_real
    assert _tile_bytes(window, 1) == before_shown


def test_a_2d_pattern_locks_the_tool_out_and_suspends_the_map(qtbot, tmp_path) -> None:
    """Under the wide-bitmap walk a tile's bytes interleave with its
    neighbours', so two rearranged tiles could land in one stripe and the second
    write would undo the first. The tool switches off and the map goes inert —
    kept, not discarded, so leaving 2D brings the rearrangement back."""
    window = _window(qtbot, tmp_path)
    window._set_tile_map(TileMap().swap(0, 5))
    window._set_rearranging(True)

    window._two_d.setChecked(True)
    assert not window._rearrange_available()
    assert not window._rearranging  # disarmed, not merely greyed
    assert not window._rearrange_action.isEnabled()
    assert window._active_tile_map().is_identity()
    assert window._decode_run(0, 2) == window._decode_actual_run(0, 2)

    window._two_d.setChecked(False)
    assert window._rearrange_action.isEnabled()
    assert window._active_tile_map() == TileMap().swap(0, 5)


def test_writes_stay_disjoint_so_a_scattered_edit_keeps_every_tile(
    qtbot, tmp_path
) -> None:
    """A rearranged gesture splits into one splice per run of consecutive
    homes. They must not overlap, or a later one would carry through an
    earlier one's pre-edit bytes and silently undo it."""
    window = _window(qtbot, tmp_path)
    # Three positions whose real homes are far apart and out of order.
    window._set_tile_map(TileMap().swap(0, 30).swap(1, 17).swap(2, 45))
    blanks = window._blank_tiles(3)
    spans = window._encode_spans(0, blanks)
    assert len(spans) == 3
    ends = sorted((start, start + len(data)) for start, data in spans)
    assert all(a[1] <= b[0] for a, b in zip(ends, ends[1:]))
    window._apply_tile_edit(0, blanks, "blank three")
    assert window._decode_run(0, 3) == blanks


def test_a_real_drag_rearranges(qtbot, tmp_path) -> None:
    """Driven by real mouse events — the canvas has to check the tool's flag
    ahead of the tile/pixel split, or the press selects instead of picking the
    tile up."""
    window = _window(qtbot, tmp_path)
    window._set_rearranging(True)
    canvas = window._canvas
    zoom, tile = window._zoom.value(), 8

    def send(kind, cell, button, buttons):
        QApplication.sendEvent(
            canvas,
            QMouseEvent(
                kind,
                QPointF((cell * tile + 4) * zoom, 4 * zoom),
                button,
                buttons,
                Qt.KeyboardModifier.NoModifier,
            ),
        )

    before = _tile_bytes(window, 0)
    send(
        QEvent.Type.MouseButtonPress,
        0,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
    )
    send(QEvent.Type.MouseMove, 2, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    assert canvas._float_image is not None  # the tile is in the air
    assert canvas._drop_slots == frozenset({2})
    send(
        QEvent.Type.MouseButtonRelease,
        2,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
    )
    assert window._tile_map == TileMap().swap(0, 2)
    assert canvas._float_image is None
    assert _tile_bytes(window, 0) == before  # the display moved, the file did not


def test_arming_the_tool_forces_rectangle_and_keeps_the_anchor(qtbot, tmp_path) -> None:
    """A linear run is a run through *storage*, so it has no shape a drag could
    carry: arming collapses it to the tile the user is demonstrably on and locks
    the picker, which unlocks again when the tool is put away."""
    window = _window(qtbot, tmp_path)
    window._select_tiles(2, 6)
    window._set_rearranging(True)
    assert window._selection_shape.currentData() is SelectionShape.RECT
    assert not window._selection_shape.isEnabled()
    assert not window._can_toggle_selection_mode()  # nor by its shortcut
    assert window._selection_tiles() == [2]
    window._set_rearranging(False)
    assert window._selection_shape.isEnabled()


def test_the_tool_and_pixel_mode_are_exclusive(qtbot, tmp_path) -> None:
    """Both want the same drag, so arming either puts the other away — and the
    toolbar has to follow, or a button claims a mode that is no longer on."""
    window = _window(qtbot, tmp_path)
    window._set_edit_mode(EditMode.PIXEL)
    window._on_tool_selected(Tool.PENCIL)
    window._set_rearranging(True)
    assert window._edit_mode is EditMode.TILE
    assert not window._edit_mode_action.isChecked()
    window._set_edit_mode(EditMode.PIXEL)
    assert not window._rearranging
    assert not window._rearrange_action.isChecked()


def test_a_right_drag_selects_tiles_while_rearranging(qtbot, tmp_path) -> None:
    """The right button carries the selection drag while the tool is armed — the
    left one is picking tiles up, and a block has to be selected before it can be
    carried as one. Nothing may be rearranged by it."""
    window = _window(qtbot, tmp_path)
    window._set_rearranging(True)
    canvas = window._canvas
    zoom, tile = window._zoom.value(), 8
    right = Qt.MouseButton.RightButton

    def send(kind, cell, button, buttons):
        QApplication.sendEvent(
            canvas,
            QMouseEvent(
                kind,
                QPointF((cell * tile + 4) * zoom, 4 * zoom),
                button,
                buttons,
                Qt.KeyboardModifier.NoModifier,
            ),
        )

    send(QEvent.Type.MouseButtonPress, 1, right, right)
    send(QEvent.Type.MouseMove, 3, Qt.MouseButton.NoButton, right)
    send(QEvent.Type.MouseButtonRelease, 3, right, Qt.MouseButton.NoButton)
    assert window._selection_tiles() == [1, 2, 3]
    assert window._tile_map.is_identity()
    assert canvas._float_image is None  # nothing was picked up


def test_the_map_rides_the_entry_and_survives_a_switch(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._set_tile_map(TileMap().swap(2, 9))
    first = window._workspace.current
    other = tmp_path / "b.4bpp.sfc"
    other.write_bytes(bytes(32 * 8))
    window._load_pixel(str(other))
    assert window._tile_map.is_identity()  # the new entry has its own
    window._activate_entry(first)
    assert window._tile_map == TileMap().swap(2, 9)


def test_a_map_survives_a_project_round_trip(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    stored = (
        TileMap()
        .swap(2, 9)
        .oriented([5], TILE_FLIP_BOTH)
        .oriented([7], TILE_ROTATE_CCW)
    )
    window._set_tile_map(stored)
    window._show_rearranged_action.setChecked(False)
    path = str(tmp_path / "p.celpix")
    window._save_project_to(path)
    reopened = MainWindow()
    qtbot.addWidget(reopened)
    reopened._load_project(path)
    assert reopened._tile_map == stored
    assert not reopened._show_rearranged


def test_orientations_alone_survive_a_project_round_trip(qtbot, tmp_path) -> None:
    """A map with no moves still has to persist — `tile_map` would be empty."""
    window = _window(qtbot, tmp_path)
    window._set_tile_map(TileMap().oriented([4], TILE_FLIP_V))
    path = str(tmp_path / "p.celpix")
    window._save_project_to(path)
    reopened = MainWindow()
    qtbot.addWidget(reopened)
    reopened._load_project(path)
    assert reopened._tile_map == TileMap().oriented([4], TILE_FLIP_V)


def test_a_project_written_before_turning_keeps_its_mirrors(qtbot, tmp_path) -> None:
    """`tile_flips` is where those projects hold them, and its two bits are the low
    two of an orientation — so it is read as one rather than silently dropped."""
    window = _window(qtbot, tmp_path)
    path = tmp_path / "p.celpix"
    window._set_tile_map(TileMap().swap(2, 9))
    window._save_project_to(str(path))
    data = json.loads(path.read_text())
    data["entries"][0]["view"]["tile_flips"] = [[5, TILE_FLIP_BOTH]]
    path.write_text(json.dumps(data))

    reopened = MainWindow()
    qtbot.addWidget(reopened)
    reopened._load_project(str(path))
    assert reopened._tile_map == TileMap().swap(2, 9).oriented([5], TILE_FLIP_BOTH)


def test_an_unrearranged_project_writes_no_map(qtbot, tmp_path) -> None:
    """The feature existing must not change the file of a project nobody
    rearranged — the key is absent, not an empty list."""
    window = _window(qtbot, tmp_path)
    path = tmp_path / "p.celpix"
    window._save_project_to(str(path))
    assert "tile_map" not in path.read_text()
