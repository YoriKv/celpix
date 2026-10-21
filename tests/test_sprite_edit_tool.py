"""Edit Subsprites: the Edit Tiles tool read on a sprite map's pieces — the drag,
the nudge, the eyedrop and band, click-to-add, and the record ops on their keys
(``docs/design/sprite-map.md`` §10.1-§10.2, §10.5-§10.6)."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from celpix.project.workspace import TileMode, TileSource
from celpix.ui.main_window import MainWindow
from uihelpers import _make_snes_file

_LEFT, _RIGHT = Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton
_PLAIN = Qt.KeyboardModifier.NoModifier
_SHIFT = Qt.KeyboardModifier.ShiftModifier
_ALT = Qt.KeyboardModifier.AltModifier
_CTRL = Qt.KeyboardModifier.ControlModifier


def _obj_bytes(frames) -> bytes:
    """An S-CG-CAD object whose frame *f* holds ``frames[f]``: ``(x, y, tile)``
    or ``(x, y, tile, palette_row)`` per piece, in slot order from slot 0."""
    from celpix.plugins.builtins.scgcad import OBJ_PAYLOADS, OBJ_SIZE, SIGNATURE

    out = bytearray(OBJ_SIZE)
    for number, parts in enumerate(frames):
        for slot, (x, y, tile, *rest) in enumerate(parts):
            at = (number * 64 + slot) * 6
            attr = (tile & 0x1FF) | ((rest[0] if rest else 0) & 0x7) << 9
            out[at : at + 6] = bytes((0x80, 0, y & 0xFF, x & 0xFF)) + attr.to_bytes(
                2, "big"
            )
    out[OBJ_PAYLOADS[0] : OBJ_PAYLOADS[0] + len(SIGNATURE)] = SIGNATURE
    return bytes(out)


def _spr_bytes(frames) -> bytes:
    """A Yoshi SPR pattern: per frame a count byte, then 8-byte records."""
    from celpix.plugins.builtins.ys_spr import FRAMES, TRAILER

    out = bytearray()
    for number in range(FRAMES):
        parts = frames[number] if number < len(frames) else []
        out.append(len(parts))
        for x, y, tile in parts:
            out += (x & 0xFFFF).to_bytes(2, "big") + (y & 0xFFFF).to_bytes(2, "big")
            out += tile.to_bytes(2, "big") + bytes([0, 0])
    return bytes(out) + bytes(TRAILER) + b"SIG"


def _armed(qtbot, tmp_path, frames, *, name="sprite.OBJ", data=None):
    """A window on a sprite object bound to a bank, Edit Subsprites armed."""
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    path = tmp_path / name
    path.write_bytes(_obj_bytes(frames) if data is None else data)
    window._load_pixel(str(path))
    entry = window._workspace.current
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    entry.doc = None  # re-read through the new binding
    window._activate_entry(window._workspace.entries[0])
    window._activate_entry(entry)
    window._stamp_action.trigger()
    assert window._stamping
    return window


def _at(window, frame, x, y):
    """Sheet pixel of object ``(x, y)`` in frame slot ``frame``."""
    ox, oy = window._slot_origin(window._sprite_sheet(), frame)
    return ox + x, oy + y


def _drag(window, start, end, mods=_PLAIN):
    canvas = window._canvas
    canvas.stamp_pixel_pressed.emit(*start, _LEFT)
    window._on_subsprite_moved(*end, mods)
    window._on_subsprite_released(*end, mods)


def _key(window, key, mods=_PLAIN) -> bool:
    return window._handle_nav_key(QKeyEvent(QEvent.Type.KeyPress, key, mods))


def _xs(window):
    return [(sub.x, sub.y) for sub in window._doc.shown_frames[0]]


def _status(window) -> str:
    return window.statusBar().currentMessage()


def test_the_tool_reads_as_edit_subsprites_and_stays_off_a_view_only_map(
    qtbot, tmp_path
) -> None:
    """On a sprite with limits the same pair arms and is named for pieces, and
    its rows are offered with a pick; a sprite map declaring no limits refuses
    the tool and says why."""
    window = _armed(qtbot, tmp_path, [[(0, 0, 1)]])
    assert window._stamp_action.iconText() == "Edit Subsprites"
    assert "Edit Subsprites (T)" in window._stamp_action.toolTip()
    window._set_picked_records([0])
    window._sync_subsprite_actions()
    assert window._subsprite_duplicate_action.isVisible()
    assert window._subsprite_duplicate_action.isEnabled()

    window._doc.subsprite_limits = None  # what a mapping table declares
    window._sync_stamp_actions()
    assert not window._stamping and not window._stamp_action.isEnabled()
    assert "Unavailable" in window._stamp_action.toolTip()


def test_a_drag_moves_the_picked_piece_and_undo_puts_it_back(qtbot, tmp_path) -> None:
    """The float and the landing box follow the pointer; release lands one step
    that keeps the pick, and undo restores both the offset and the pick."""
    window = _armed(qtbot, tmp_path, [[(0, 0, 1), (8, 0, 2), (24, 4, 3)]])
    canvas = window._canvas
    start = _at(window, 0, 10, 2)
    canvas.stamp_pixel_pressed.emit(*start, _LEFT)
    assert window._picked_records == [1]
    window._on_subsprite_moved(start[0] + 4, start[1] + 3, _PLAIN)
    assert canvas._float_image is not None and len(canvas._drop_rects) == 1
    assert "Moving subsprite 1: (+8, +0) -> (+12, +3)" in _status(window)
    window._on_subsprite_released(start[0] + 4, start[1] + 3, _PLAIN)
    assert canvas._float_image is None and not canvas._drop_rects
    assert _xs(window)[1] == (12, 3) and window._picked_records == [1]
    assert _status(window) == "Moved 1 subsprite by (+4, +3)"

    window._undo_stack.undo()
    assert _xs(window)[1] == (8, 0) and window._picked_records == [1]

    # A click in place cycles rather than moving: nothing is pushed.
    count = window._undo_stack.count()
    window._canvas.stamp_pixel_pressed.emit(*start, _LEFT)
    window._on_subsprite_released(*start, _PLAIN)
    assert window._undo_stack.count() == count


def test_a_group_drag_is_clamped_as_a_group_and_shift_locks_an_axis(
    qtbot, tmp_path
) -> None:
    """The delta shrinks until the outermost piece reaches the range's edge, and
    every piece moves by that one delta — the pick keeps its shape."""
    window = _armed(qtbot, tmp_path, [[(100, 0, 1), (120, 0, 2)]])
    window._set_picked_records([0, 1])
    start = _at(window, 0, 121, 1)
    _drag(window, start, (start[0] + 20, start[1] + 3), _SHIFT)
    assert _xs(window) == [(107, 0), (127, 0)]


def test_arrows_nudge_one_run_and_refuse_the_edge(qtbot, tmp_path) -> None:
    """A held arrow is one undo step; another key ends the run; Shift steps a
    tile; a nudge that would leave the range is refused whole, naming it."""
    window = _armed(qtbot, tmp_path, [[(0, 0, 1), (111, 0, 2)]])
    window._set_picked_records([0])
    count = window._undo_stack.count()
    assert _key(window, Qt.Key.Key_Right) and _key(window, Qt.Key.Key_Right)
    assert _xs(window)[0] == (2, 0) and window._undo_stack.count() == count + 1
    _key(window, Qt.Key.Key_Escape)
    assert _key(window, Qt.Key.Key_Down, _SHIFT)
    assert _xs(window)[0] == (2, 8) and window._undo_stack.count() == count + 2
    window._undo_stack.undo()
    window._undo_stack.undo()
    assert _xs(window)[0] == (0, 0)

    window._set_picked_records([0, 1])
    cells = list(window._doc.cells)
    assert _key(window, Qt.Key.Key_Right, _SHIFT) and _key(
        window, Qt.Key.Key_Right, _SHIFT
    )
    assert _xs(window) == [(16, 0), (127, 0)]
    _key(window, Qt.Key.Key_Right)
    assert _xs(window) == [(16, 0), (127, 0)]
    assert _status(window) == "Offsets here run -128..127 - stopped at the edge"
    assert window._doc.cells != cells  # the two tile steps landed


def test_the_right_button_eyedrops_a_piece_and_bands_pick_touched_ones(
    qtbot, tmp_path
) -> None:
    """A right click holds the piece's whole record and its row in Palette Row;
    a band picks every piece whose box it touches, replacing the pick."""
    window = _armed(qtbot, tmp_path, [[(0, 0, 1), (8, 0, 2, 3), (24, 8, 3)]])
    doc = window._doc
    window._canvas.stamp_pixel_pressed.emit(*_at(window, 0, 10, 2), _RIGHT)
    assert window._source_cell == doc.cells[1] and window._source_tile_id == 2
    assert window._palette_row.value() == window._drawn_palette_row(3)

    # From inside piece 1 to one pixel into piece 2's box: 0 is not touched.
    window._set_picked_records([0])
    x0, y0 = _at(window, 0, 12, 4)
    x1, y1 = _at(window, 0, 24, 8)
    window._canvas.stamp_pixel_area_picked.emit(x0, y0, x1, y1)
    assert window._picked_records == [1, 2]


def test_a_click_on_empty_sheet_adds_the_held_record(qtbot, tmp_path) -> None:
    """The held record lands in the first free slot of the frame under the
    pointer, snapped to the tile grid, with the record's own fields, and becomes
    the pick; Ctrl places it at the pixel instead."""
    window = _armed(qtbot, tmp_path, [[(0, 0, 1), (8, 0, 2, 3), (24, 8, 3)]])
    window._canvas.stamp_pixel_pressed.emit(*_at(window, 0, 10, 2), _RIGHT)
    window._canvas.stamp_pixel_hovered.emit(_at(window, 0, 19, 13))
    assert window._canvas._stamp_preview is not None
    assert window._canvas._stamp_preview_at == _at(window, 0, 16, 8)

    window._canvas.stamp_pixel_pressed.emit(*_at(window, 0, 19, 13), _LEFT)
    window._on_subsprite_released(*_at(window, 0, 19, 13), _PLAIN)
    assert window._picked_records == [3]
    sub = window._picked_subsprite_record()
    assert (sub.x, sub.y, sub.index) == (16, 8, 2)
    # Byte 0 (drawn, size) and the group byte are the template's; the low two
    # bytes are the offsets just written.
    assert window._doc.cells[3].flags >> 16 == window._source_cell.flags >> 16

    window._on_subsprite_pressed(*_at(window, 0, 5, 11), _LEFT, _CTRL)
    assert window._picked_records == [4]
    sub = window._picked_subsprite_record()
    assert (sub.x, sub.y) == (5, 11)


def test_clear_removes_a_piece_on_the_format_s_own_terms(qtbot, tmp_path) -> None:
    """On slots Clear turns the drawn bit off and the pick stays for Drawn to put
    back; on a counted format the record goes and so does the pick."""
    window = _armed(qtbot, tmp_path, [[(0, 0, 1), (8, 0, 2)]])
    window._set_picked_records([1])
    window._clear_cells()
    assert len(window._doc.shown_frames[0]) == 1 and window._picked_records == [1]
    assert len(window._doc.cells) == 32 * 64

    spr = _armed(
        qtbot,
        tmp_path,
        None,
        name="pattern.spr",
        data=_spr_bytes([[(0, 0, 1), (8, 0, 2)], [(4, 4, 3)]]),
    )
    assert len(spr._doc.cells) == 3
    spr._set_picked_records([0])
    spr._clear_cells()
    assert len(spr._doc.cells) == 2 and spr._picked_records == []
    assert [len(frame) for frame in spr._doc.shown_frames[:2]] == [1, 1]
    spr._undo_stack.undo()
    assert len(spr._doc.cells) == 3 and spr._picked_records == [0]


def test_alt_up_reorders_and_the_pick_follows(qtbot, tmp_path) -> None:
    window = _armed(qtbot, tmp_path, [[(0, 0, 1), (8, 0, 2), (24, 8, 3)]])
    window._set_picked_records([2])
    assert _key(window, Qt.Key.Key_Up, _ALT)
    assert [sub.index for sub in window._doc.shown_frames[0]] == [1, 3, 2]
    assert window._picked_records == [1]
    assert _key(window, Qt.Key.Key_Up, _ALT | _SHIFT)
    assert [sub.index for sub in window._doc.shown_frames[0]] == [3, 1, 2]
    assert window._picked_records == [0]


def test_ctrl_d_duplicates_and_picks_the_copies(qtbot, tmp_path) -> None:
    window = _armed(qtbot, tmp_path, [[(0, 0, 1), (8, 0, 2), (24, 8, 3)]])
    window._set_picked_records([0, 1])
    assert _key(window, Qt.Key.Key_D, _CTRL)
    assert window._picked_records == [3, 4]
    assert _xs(window)[3:] == [(8, 8), (16, 8)]
    assert [sub.index for sub in window._doc.shown_frames[0]][3:] == [1, 2]


def test_mirror_about_origin_reflects_each_piece_by_its_own_width(
    qtbot, tmp_path
) -> None:
    window = _armed(qtbot, tmp_path, [[(8, 0, 1), (-4, 4, 2)]])
    window._set_picked_records([0, 1])
    window._sync_subsprite_actions()
    window._subsprite_mirror_h_action.trigger()
    frame = window._doc.shown_frames[0]
    assert [(sub.x, sub.y) for sub in frame] == [(-16, 0), (-4, 4)]
    assert all(sub.flip_h for sub in frame)
    assert "about the origin" in _status(window)


def test_a_drag_across_a_frame_boundary_moves_the_piece_there(qtbot, tmp_path) -> None:
    """Released over another frame's slot, the piece joins that frame at the
    same object position plus the delta, and stays picked."""
    window = _armed(qtbot, tmp_path, [[(0, 0, 1), (8, 0, 2)], [(0, 0, 3)]])
    start = _at(window, 0, 9, 1)
    window._canvas.stamp_pixel_pressed.emit(*start, _LEFT)
    end = _at(window, 1, 9, 1)
    window._on_subsprite_moved(*end, _PLAIN)
    assert window._canvas._drop_rects_valid
    window._on_subsprite_released(*end, _PLAIN)
    frames = window._doc.shown_frames
    assert [sub.index for sub in frames[0]] == [1]
    assert [(sub.index, sub.x) for sub in frames[1]] == [(3, 0), (2, 8)]
    assert window._picked_records == [65]
