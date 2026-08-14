"""Editing through a tilemap: its cells, the tile source panel,
the stamp tool, painting pixels on a map and the animation player."""

from __future__ import annotations

from PySide6.QtCore import QRect

from celpix.ui.main_window import MainWindow
from celpix.ui.main_window.transform import OP_FLIP_H
from celpix.ui.tools import EditMode
from celpix.ui.widgets import select_combo_data
from uihelpers import (
    _bound_screen,
    _bound_tilemap,
    _bound_to_slice,
    _make_snes_file,
    _map_file,
    _obj_file,
    _pnl_file,
    _scr_file,
)


def test_write_saves_the_tiles_a_map_was_painted_on(qtbot, tmp_path) -> None:
    """The tilemap half of the same rule. A map borrows its tiles, so a pixel edit
    made on it is deposited into the entry those bytes belong to and the map reads
    clean (``tilemap-entry.md`` §8.1) — which left Ctrl+W reporting a successful
    write with the painting still only in memory, reachable only by finding the
    bank in the Files list.
    """
    from pathlib import Path

    from celpix.core.tilemap import Cell

    window, bank, entry = _bound_screen(qtbot, tmp_path, [Cell(index=1)])
    window._activate_entry(entry)
    _painting_on(window)
    before = Path(bank.path).read_bytes()

    _paint(window, 1, 1, 9)
    assert bank.pixel_dirty and not entry.pixel_dirty  # it landed in the bank

    window._write_current()
    assert not bank.pixel_dirty  # the bank went with the map...
    assert Path(bank.path).read_bytes() != before
    assert bank.name in window.statusBar().currentMessage()  # ...and is named

    # A bank nobody painted on is left alone, for the reason a clean .pal is: it
    # is a shared file with nothing of its own to save.
    stamp = Path(bank.path).stat().st_mtime_ns
    window._write_current()
    assert Path(bank.path).stat().st_mtime_ns == stamp
    assert bank.name not in window.statusBar().currentMessage()


def test_clicking_a_sprite_picks_the_subsprite_and_rings_the_tile_it_names(
    qtbot, tmp_path
) -> None:
    """The one kind of document where clicking the picture used to say nothing:
    a sprite object has no cell under the cursor, so the slot the canvas reports
    names no record and the tile source panel had nothing to ring.

    The press is answered by the **pixel** instead — subsprites overlap and sit
    at offsets that are not tile-aligned, so a slot cannot tell them apart — and
    the pick then drives both readers: the outline on the canvas, and the ring
    over the tile that record draws.
    """
    from PySide6.QtCore import QPoint, QRect, Qt

    window, _ = _shown_sprite_source(qtbot, tmp_path, [(0, 0, 1), (24, 5, 2, 3)])
    canvas, panel = window._canvas, window._tile_source_panel
    zoom = canvas._zoom
    # The sheet is the bank a subsprite names, on the same terms a map's is: the
    # panel refused a sprite outright while there was no pick for it to answer.
    assert window._tile_source_note() is None and len(panel._ids) == 8

    # The second part, at x=24 y=5 — a pixel no tile boundary would put there.
    qtbot.mouseClick(
        canvas, Qt.MouseButton.LeftButton, pos=QPoint(int(26 * zoom), int(7 * zoom))
    )
    assert window._picked_subsprite == (0, 1)
    assert canvas._pick_outline == QRect(24, 5, 8, 8)
    assert panel._marked == 2  # the tile that record draws
    assert "subsprite 1" in window.statusBar().currentMessage()
    # ...and the palette grid rings the row it draws in, which a record states
    # for itself: row 3 of the sprite half, so row 11 of the palette.
    assert window._palette_panel._marked_row == 3 + window._doc.palette_row_base

    # The other part answers the same way, and the ring follows it.
    qtbot.mouseClick(
        canvas, Qt.MouseButton.LeftButton, pos=QPoint(int(2 * zoom), int(2 * zoom))
    )
    assert window._picked_subsprite == (0, 0) and panel._marked == 1
    assert window._palette_panel._marked_row == window._doc.palette_row_base

    # Empty space in the frame picks nothing rather than the nearest record.
    qtbot.mouseClick(
        canvas, Qt.MouseButton.LeftButton, pos=QPoint(int(14 * zoom), int(14 * zoom))
    )
    assert window._picked_subsprite is None
    assert canvas._pick_outline is None and panel._marked is None

    # And the pick belongs to the document: opening another one drops it, or its
    # outline would sit over a picture it says nothing about.
    window._activate_entry(window._workspace.entries[0])
    assert window._picked_subsprite is None and canvas._pick_outline is None


def test_pressing_the_same_sprite_tile_again_cycles_the_records_under_it(
    qtbot, tmp_path
) -> None:
    """Overlap is the normal case on a sprite object, and the front-most record
    hides the ones behind it - so one answer per press would leave whole records
    unreachable by clicking the picture they are part of.

    The tile is what makes a press a second press: the same spot clicked twice is
    never the same pixel twice, and anywhere else has to start over at the front
    or the first answer on a piece would not be the one the eye picks out.
    """
    from PySide6.QtCore import QPoint, Qt

    # Two records stacked at the origin, and a third off on its own.
    window, _ = _shown_sprite_source(
        qtbot, tmp_path, [(0, 0, 1), (0, 0, 2), (24, 5, 3)]
    )
    canvas = window._canvas
    zoom = canvas._zoom
    stack = QPoint(int(2 * zoom), int(2 * zoom))
    apart = QPoint(int(26 * zoom), int(7 * zoom))

    qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=stack)
    first = window._picked_subsprite
    # The stack is worth saying out loud - the outline moving is otherwise the
    # only sign that pressing again does anything at all.
    assert "click again for the next of 2" in window.statusBar().currentMessage()

    qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=stack)
    second = window._picked_subsprite
    assert {first, second} == {(0, 0), (0, 1)}  # both reachable, front one first

    # A third press wraps rather than sticking on the last one.
    qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=stack)
    assert window._picked_subsprite == first

    # A record with nothing over it says so, and does not cycle.
    qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=apart)
    assert window._picked_subsprite == (0, 2)
    assert "click again" not in window.statusBar().currentMessage()
    qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=apart)
    assert window._picked_subsprite == (0, 2)

    # Coming back to the stack starts at its front again, not where it left off.
    qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=stack)
    assert window._picked_subsprite == first


def test_the_cell_spin_follows_the_selection(qtbot, tmp_path) -> None:
    """It reads the selected cell, so a click on a different one has to move it —
    and it greys with nothing selected, because then it is the selection that is
    missing rather than the control that means nothing here.

    The selection changes without the view being re-rendered, so nothing else in
    the refresh cycle would bring the spin along.
    """
    from celpix.core.tilemap import Cell

    window, _ = _bound_tilemap(
        qtbot,
        tmp_path,
        [Cell(index=1), Cell(index=2), Cell(index=3)],
        maker=_pnl_file,
    )
    assert not window._cell_index.isHidden()  # the window itself is never shown

    window._select_tiles(0, 0)
    assert window._cell_index.isEnabled()
    assert window._cell_index.value() == 1
    window._select_tiles(2, 2)
    assert window._cell_index.value() == 3
    # A run reads its first cell, which is the one an edit through the spin anchors
    # on, and the one a paste would start at.
    window._select_tiles(1, 2)
    assert window._cell_index.value() == 2

    window._clear_selection()
    assert not window._cell_index.isEnabled()


def test_a_tilemap_selection_says_where_on_the_map_it_starts(qtbot, tmp_path) -> None:
    """A map has no byte address to report — the bytes under the view are the
    tile bank's — so the status line answers "and where is it" with the grid
    position instead, the slot a graphic fills with its file offset.

    Zero-based ``(column, row)``, read off the width the map is *drawn* at, so
    re-laying the same cells at a different Cols moves the coordinate with them.
    """
    from celpix.core.tilemap import Cell

    window, _ = _bound_tilemap(
        qtbot, tmp_path, [Cell(index=i) for i in range(12)], maker=_pnl_file
    )
    window._columns.setValue(4)

    # A rectangle, the shape a tilemap selects in.
    window._on_slots_selected(6, 6)
    assert (
        window.statusBar().currentMessage() == "Selected 1×1 cells from (2, 1) (1 cell)"
    )

    # A linear run answers for its first cell, the one an edit anchors on.
    window._set_linear_selection(6, 7)
    window._announce_selection()
    assert "from (2, 1)" in window.statusBar().currentMessage()
    window._set_linear_selection(6, 6)
    window._announce_selection()
    assert window.statusBar().currentMessage() == "Selected cell 6 at (2, 1)"

    # The same cell at a different width is a different position on the picture.
    window._columns.setValue(3)
    window._announce_selection()
    assert window.statusBar().currentMessage() == "Selected cell 6 at (0, 2)"


def test_flipping_a_cell_toggles_its_bit_and_moves_no_pixels(qtbot, tmp_path) -> None:
    """The whole reason hardware has the bit: a mirrored tile costs one bit and
    no pixels, so nothing in the tile source is touched."""
    from celpix.core.tilemap import Cell

    window, entry = _bound_tilemap(qtbot, tmp_path, [Cell(index=1), Cell(index=2)])
    before_tiles = bytes(window._doc.pixel_data)

    window._set_linear_selection(0, 0)
    window._transform_tiles(OP_FLIP_H)

    assert window._doc.cells[0] == Cell(index=1, flip_h=True)
    assert window._doc.cells[1] == Cell(index=2)  # untouched
    assert window._doc.pixel_data == before_tiles  # no pixels rewritten
    assert entry.pixel_dirty  # ...but the map is unsaved


def test_a_format_with_no_flip_bit_says_so_and_changes_nothing(qtbot, tmp_path) -> None:
    """The tool names the operation and the *format* answers it, so a map read
    through a codec with nowhere to put a flip refuses rather than setting a bit
    the next save would drop (``docs/design/tilemap-entry.md`` §4)."""
    from celpix.core.tilemap import Cell

    window, entry = _bound_tilemap(qtbot, tmp_path, [Cell(index=1)])
    entry.tilemap_preset_id = "preset.tilemap.gb-bg"  # one byte, index only
    window._reload_tilemap(entry)
    before = list(window._doc.cells)
    steps = window._undo_stack.count()

    window._set_linear_selection(0, 0)
    window._transform_tiles(OP_FLIP_H)

    assert window._doc.cells == before
    assert "no horizontal flip" in window.statusBar().currentMessage()
    assert window._undo_stack.count() == steps  # nothing to take back


def test_flipping_a_cell_twice_comes_back(qtbot, tmp_path) -> None:
    from celpix.core.tilemap import Cell

    window, _ = _bound_tilemap(qtbot, tmp_path, [Cell(index=1)])
    window._set_linear_selection(0, 0)
    window._transform_tiles(OP_FLIP_H)
    window._transform_tiles(OP_FLIP_H)
    assert window._doc.cells[0] == Cell(index=1)


def test_a_cell_flip_undoes(qtbot, tmp_path) -> None:
    from celpix.core.tilemap import Cell

    window, entry = _bound_tilemap(qtbot, tmp_path, [Cell(index=1)])
    window._set_linear_selection(0, 0)
    window._transform_tiles(OP_FLIP_H)
    assert entry.pixel_dirty

    window._undo_stack.undo()
    assert window._doc.cells[0] == Cell(index=1)
    # The revision goes back with it, so an undo to the saved state reads clean.
    assert not entry.pixel_dirty


def test_a_block_flip_reorders_the_cells_and_flips_each(qtbot, tmp_path) -> None:
    """Both halves: reversing alone mirrors the layout with every tile still
    facing its original way, toggling alone mirrors each in place."""
    from celpix.core.tilemap import Cell
    from celpix.ui.main_window.selection import SelectionShape

    # A panel, so the block can be a 2x2 at two columns: a screen file's four
    # pages own the column count (:meth:`_settle_tilemap_width`).
    window, _ = _bound_tilemap(
        qtbot,
        tmp_path,
        [Cell(index=1), Cell(index=2), Cell(index=3), Cell(index=4)],
        maker=_pnl_file,
    )
    window._columns.setValue(2)
    window._refresh_view()
    select_combo_data(window._selection_shape, SelectionShape.RECT)
    window._on_slots_selected(0, 3)  # cells (0,0)..(1,1) at 2 columns
    window._transform_block(OP_FLIP_H)

    cells = window._doc.cells[:4]
    assert [c.index for c in cells] == [2, 1, 4, 3]  # columns reversed
    assert all(c.flip_h for c in cells)  # ...and each mirrored


def test_copying_cells_stays_inside_the_app(qtbot, tmp_path) -> None:
    """Cells are indices into a tile source another program knows nothing about,
    so they never reach the system clipboard."""
    from PySide6.QtWidgets import QApplication

    from celpix.core.tilemap import Cell
    from celpix.ui import clipboard

    window, _ = _bound_tilemap(qtbot, tmp_path, [Cell(index=7), Cell(index=8)])
    QApplication.clipboard().clear()
    window._set_linear_selection(0, 0)
    assert window._copy_selection()

    assert window._has_cell_clipboard()
    assert not clipboard.has_content()  # nothing went out to the system
    window._sync_edit_actions()
    assert window._paste_action.isEnabled()  # from the in-app buffer, not the OS


def test_pasting_cells_overwrites_from_the_selection(qtbot, tmp_path) -> None:
    from celpix.core.tilemap import Cell

    window, _ = _bound_tilemap(
        qtbot, tmp_path, [Cell(index=7), Cell(index=8), Cell(index=9)]
    )
    window._set_linear_selection(0, 0)
    window._copy_selection()
    window._set_linear_selection(2, 2)
    window._paste()

    assert window._doc.cells[2] == Cell(index=7)
    assert window._doc.cells[0] == Cell(index=7)  # the source is untouched
    window._undo_stack.undo()
    assert window._doc.cells[2] == Cell(index=9)


def test_cutting_cells_copies_then_blanks_them(qtbot, tmp_path) -> None:
    from celpix.core.tilemap import Cell

    window, _ = _bound_tilemap(qtbot, tmp_path, [Cell(index=7), Cell(index=8)])
    window._set_linear_selection(0, 0)
    window._cut_selection()

    assert window._doc.cells[0] == Cell()  # blanked, not removed
    assert window._doc.cells[1] == Cell(index=8)
    assert len(window._doc.cells) == 4096  # the extent is the file's
    window._set_linear_selection(1, 1)
    window._paste()
    assert window._doc.cells[1] == Cell(index=7)


def test_an_edit_that_changes_nothing_adds_no_undo_step(qtbot, tmp_path) -> None:
    """Or a flip of an empty selection would leave a step that appears to do
    nothing when it comes back."""
    from celpix.core.tilemap import Cell

    window, _ = _bound_tilemap(qtbot, tmp_path, [Cell(index=1)])
    depth = window._undo_stack.count()
    window._clear_selection()
    window._transform_tiles(OP_FLIP_H)
    assert window._undo_stack.count() == depth


def test_a_stamp_layout_opens_with_its_own_coordinate_codec(qtbot, tmp_path) -> None:
    from celpix.core.capabilities import ContentKind
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_map_file(tmp_path, [Cell(index=3)])))

    entry = window._workspace.entries[0]
    assert entry.container_id == "container.scgcad-map"
    assert entry.content_kind is ContentKind.TILEMAP
    assert entry.tilemap_preset_id == "preset.tilemap.scgcad-map"
    assert window._tilemap_is_indirect(entry)


def test_a_forced_tilemap_open_is_editable_under_the_default_codec(
    qtbot, tmp_path
) -> None:
    """File > Open tilemap data on a raw region: no container names a cell codec,
    so the entry carries none and is read under the default. Everything that asks
    the *format* a question has to resolve it the same way the load did, or the
    flips, the Cell spin and Edit Tiles switch off over a map drawing perfectly
    well.
    """
    from celpix.core.capabilities import ContentKind

    path = tmp_path / "raw.bin"
    path.write_bytes(bytes(range(256)) * 8)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(path), content_kind=ContentKind.TILEMAP)

    entry = window._workspace.current
    assert entry.tilemap_preset_id is None  # nothing declared one
    assert entry.doc is not None and entry.doc.is_tilemap
    # The default is a 10-bit console BG entry, and the probes have to say so.
    assert window._cell_index_limit() == 0x3FF
    assert window._tilemap_format_name() != "This tilemap format"
    assert window._stamp_available()
    # And the picker names the format the canvas is actually drawing in.
    assert window._tilemap_preset.currentData() == "preset.tilemap.snes-bg"


def test_a_stamp_layout_is_offered_panels_first_but_not_only(qtbot, tmp_path) -> None:
    """Chaining is gated on depth, not on format, so a tile bank stays reachable.
    What the format contributes is the *order* - its coordinates cannot read a
    bank sensibly, so the tilemaps come first."""
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))  # a tile bank, opened first
    window._load_pixel(str(_pnl_file(tmp_path, [Cell(index=1)])))
    window._load_pixel(str(_map_file(tmp_path, [Cell(index=0)])))

    labels = [
        window._tile_binding.itemText(i) for i in range(window._tile_binding.count())
    ]
    assert labels == ["(none)", "panel.PNL", "s.4bpp.sfc", "From file..."]


def test_a_stamp_layout_draws_through_the_panel_and_can_be_restamped(
    qtbot, tmp_path
) -> None:
    """Two hops: layout entry -> panel cell -> the panel's own tile source. The
    panel's attributes travel with the stamp, and the layout's own entry table
    stays writable - a cell edit here restamps."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(
        str(_pnl_file(tmp_path, [Cell(index=0), Cell(index=1, flip_h=True)]))
    )
    panel = window._workspace.entries[1]
    panel.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(panel)

    window._load_pixel(str(_map_file(tmp_path, [Cell(index=1), Cell(index=0)])))
    layout = window._workspace.current
    layout.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[1]
    )
    window._reload_tilemap(layout)

    doc = window._doc
    assert doc.is_indirect
    assert doc.cells[0].index == 1  # the file's own word: a panel coordinate
    assert doc.drawn_cells[0] == Cell(index=1, flip_h=True)  # ...the panel's cell
    assert doc.cell_tiles == (1, 1)  # the panel's geometry, not the layout's
    assert doc.pixel_data  # the panel's tile source reached through
    assert not window._canvas._image.isNull()
    # Its own entry table is writable - that is what a restamp saves. The panel's
    # art is not: the tiles belong to the map at the end of the chain.
    assert doc.tilemap_config.write_enabled
    assert not doc.pixel_config.write_enabled

    # Restamping: point cell 0 at a different panel cell and the drawn cell
    # follows, while the file's own word is the coordinate that was set.
    window._set_linear_selection(0, 0)
    window._set_cell_index(0)
    assert doc.cells[0].index == 0
    assert doc.drawn_cells[0] == Cell(index=0)  # panel cell 0, not the flipped 1
    # A stamp layout's word has no flip bits, so the format refuses one rather
    # than setting something `encode` would drop.
    assert not window._tile_group.flip_h.isEnabled()


def test_a_stamp_layout_draws_the_panels_whole_block_per_entry(qtbot, tmp_path) -> None:
    """The panel says how big a stamp is and the layout obeys: one entry covers a
    2x2 block, walking the *panel's* rows for the lower half, and the three
    positions the tool never wrote are never read. An edit anywhere in the block
    changes the one entry it came from."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    # A panel is 32 cells wide, so the 2x2 block at coordinate 0 is panel cells
    # 0, 1, 32 and 33 - the last two a row down in the panel, not two along.
    cells = [Cell()] * 34
    cells[0], cells[1], cells[32], cells[33] = (
        Cell(index=10),
        Cell(index=11),
        Cell(index=12),
        Cell(index=13),
    )
    window._load_pixel(str(_pnl_file(tmp_path, cells)))
    panel = window._workspace.entries[1]
    panel.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(panel)

    # Entry 0 names coordinate 0; entry 1 is a leftover the format never reads.
    window._load_pixel(str(_map_file(tmp_path, [Cell(index=0), Cell(index=999)])))
    layout = window._workspace.current
    layout.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[1]
    )
    window._reload_tilemap(layout)

    doc = window._doc
    assert doc.stamp_cells == (2, 2)  # the panel's header, read through the chain
    # A layout is 64 wide, so the block's lower half is 64 positions along.
    drawn = doc.drawn_cells
    assert [drawn[at].index for at in (0, 1, 64, 65)] == [10, 11, 12, 13]
    # Every position in the block edits entry 0, including the one holding the
    # leftover word - which is why that word never had to be read.
    assert [doc.cell_at(at) for at in (0, 1, 64, 65)] == [0, 0, 0, 0]


def test_an_ordinary_tilemap_can_draw_through_another_tilemap(qtbot, tmp_path) -> None:
    """Chaining is not the stamp layout's own feature: a screen bound to a panel
    stamps its cells the same way, and because a screen's format *does* carry
    attributes, its own compose over the panel's."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))  # 0: the art
    window._load_pixel(
        str(_pnl_file(tmp_path, [Cell(index=0), Cell(index=7, palette_row=2)]))
    )
    panel = window._workspace.entries[1]
    panel.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(panel)

    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1, palette_row=1)])))
    screen = window._workspace.current
    screen.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[1]
    )
    window._reload_tilemap(screen)

    doc = window._doc
    assert doc.is_indirect
    assert doc.cells[0].index == 1  # the screen's own word, untouched
    # The panel's tile, the screen's palette row: a screen has the field to state
    # one, so it wins over the row the panel put on that cell.
    assert doc.drawn_cells[0] == Cell(index=7, palette_row=1)
    assert doc.pixel_data  # the panel's own binding reached through
    # Editable, and a flip is a real one here: a screen's word has the bits a
    # stamp layout's coordinate does not, so it composes over the panel's.
    assert doc.cells_editable
    assert doc.tilemap_config.write_enabled
    window._set_linear_selection(0, 0)
    window._tile_group.flip_h.trigger()
    assert doc.cells[0].flip_h  # set on the screen's own cell...
    assert doc.drawn_cells[0].flip_h  # ...and composed onto the panel's
    assert window._tile_base.isHidden()  # no tile numbering for a base to shift
    assert "Stamped from panel.PNL" in window._tile_binding_note.text()


def test_editing_the_panel_restamps_the_layout_drawing_through_it(
    qtbot, tmp_path
) -> None:
    """A cell edit replaces the panel's cell list rather than mutating it, so a
    layout holding the old one would keep drawing the stamps as they were. Both
    ends of the chain settle on the one edit - including on undo, which can land
    on a map the view has since moved off."""
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    layout = _bound_stamp_layout(window, tmp_path)
    assert layout.doc.drawn_cells[0] == Cell(index=1)  # panel cell 1

    # Edit the *panel*, which is the stamp itself...
    panel = window._workspace.entries[1]
    window._activate_entry(panel)
    window._set_linear_selection(1, 1)
    window._set_cell_index(6)
    assert panel.doc.cells[1] == Cell(index=6)
    # ...and the layout, still open, is drawing the stamp as it now is.
    assert layout.doc.drawn_cells[0] == Cell(index=6)

    # Undo from the layout's own view: the command lands on the panel, and the
    # layout has to be re-resolved and repainted even though it is not the entry
    # the edit belongs to.
    window._activate_entry(layout)
    window._undo_stack.undo()
    assert layout.doc.drawn_cells[0] == Cell(index=1)


def test_a_chain_two_tilemaps_deep_is_refused_and_says_so(qtbot, tmp_path) -> None:
    """A coordinate into a coordinate has no defined meaning, so the second hop
    is not taken. Judged from the binding rather than a loaded document, which is
    what keeps two maps pointed at each other from recursing."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))  # 0: the art
    window._load_pixel(str(_pnl_file(tmp_path, [Cell(index=3)])))  # 1
    window._load_pixel(str(_map_file(tmp_path, [Cell(index=0)])))  # 2

    # 1 -> 0 is fine, so 2 -> 1 resolves...
    panel = window._workspace.entries[1]
    panel.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    layout = window._workspace.entries[2]
    layout.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[1]
    )
    window._reload_tilemap(layout)
    assert window._doc.is_indirect

    # ...but pointing the panel at a tilemap too makes the chain one hop deeper,
    # and the layout stops resolving rather than reaching past it.
    panel.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[2]
    )
    window._reload_tilemap(layout)

    assert not window._doc.is_indirect
    assert "draws through a tilemap itself" in window._tile_binding_note.text()


def test_editing_a_stamp_layout_is_refused(qtbot, tmp_path) -> None:
    """An edit here would have to decide between restamping and editing the
    stamp, and the format's own answer to that is still unconfirmed."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_pnl_file(tmp_path, [Cell(index=1)])))
    panel = window._workspace.entries[1]
    panel.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(panel)
    window._load_pixel(str(_map_file(tmp_path, [Cell(index=0)])))
    layout = window._workspace.current
    layout.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[1]
    )
    window._reload_tilemap(layout)

    before = list(window._doc.cells)
    depth = window._undo_stack.count()
    window._set_linear_selection(0, 0)
    window._transform_tiles(OP_FLIP_H)

    assert window._doc.cells == before
    assert window._undo_stack.count() == depth


def _bound_stamp_layout(window, tmp_path):
    """A stamp layout drawing through a panel, which draws through a tile bank."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_pnl_file(tmp_path, [Cell(index=0), Cell(index=1)])))
    panel = window._workspace.entries[1]
    panel.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(panel)
    window._load_pixel(str(_map_file(tmp_path, [Cell(index=1), Cell(index=0)])))
    layout = window._workspace.current
    layout.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[1]
    )
    window._reload_tilemap(layout)
    return layout


def test_a_format_with_no_flip_bit_disables_the_flip_buttons(qtbot, tmp_path) -> None:
    """Which transforms a cell can express is the format's answer, and it is
    given on the toolbar like every other unavailable control rather than as a
    message after the click. A bare-index map has no bit for either flip."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    entry = window._workspace.current
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(entry)
    window._set_linear_selection(0, 0)
    assert window._tile_group.flip_h.isEnabled()  # the console map mirrors

    # Swapping the cell format reloads through the render cycle, which is the
    # only thing that re-decides this: the selection has not moved.
    entry.tilemap_preset_id = "preset.tilemap.gb-bg"
    window._reload_tilemap(entry)

    assert not window._tile_group.flip_h.isEnabled()
    assert not window._tile_group.flip_v.isEnabled()
    assert not window._tile_group.rotate_cw.isEnabled()  # no format rotates


def test_a_view_only_map_disables_the_cell_clipboard(qtbot, tmp_path) -> None:
    """A sprite object's cells are parts at pixel offsets, so there is no cell
    under the cursor to *write* - cut, clear and paste all go, even with cells in
    the buffer from an editable map. Copy stays, because it is a read and the
    picture under the selection is well defined even where the cells are not."""
    from celpix.core.tilemap import Cell, CellGrid

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_obj_file(tmp_path, [(0, 0, 1)])))
    assert window._doc.is_sprite
    window._cell_clipboard = CellGrid.from_cells(1, 1, [Cell(index=7)])
    window._set_linear_selection(0, 0)

    assert not window._cut_action.isEnabled()
    assert not window._clear_action.isEnabled()
    assert not window._paste_action.isEnabled()
    assert window._copy_action.isEnabled()
    assert not window._tile_group.flip_h.isEnabled()
    # ...and the reference spin has nothing to point at either.
    assert window._cell_index.isHidden()


def test_a_stamp_layout_has_no_base_tile_control(qtbot, tmp_path) -> None:
    """It draws through a panel and takes the panel's base with it, so a base of
    its own would be a live control that changes nothing."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    _bound_stamp_layout(window, tmp_path)
    assert window._tile_base.isHidden()
    assert window._tile_base_label.isHidden()

    # ...and it comes back for an ordinary map, which numbers into a tile bank.
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)], name="screen2.SCR")))
    entry = window._workspace.current
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(entry)

    assert not window._tile_base.isHidden()


def test_an_unbound_stamp_layout_still_opens(qtbot, tmp_path) -> None:
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_map_file(tmp_path, [Cell(index=5)])))

    doc = window._doc
    assert doc is not None and doc.is_tilemap
    assert not doc.is_indirect  # nothing to resolve through yet
    assert "No source bound" in window._tile_binding_note.text()


# -- the tile source panel -------------------------------------------------
def _shown_tile_source(qtbot, tmp_path, cells):
    """A bound tilemap with the Tile Source tab raised and filled.

    Shown for real: the dock composes its sheet only while it is visible, and a
    tab sharing a bar with the Palette is not visible until it is raised.
    """
    window, entry = _bound_tilemap(qtbot, tmp_path, cells, maker=_pnl_file)
    window.show()
    window._tile_source_dock.setVisible(True)
    window._tile_source_dock.raise_()
    window._refresh_tile_source()
    return window, entry


def _shown_sprite_source(qtbot, tmp_path, parts):
    """The same, over a bound sprite object whose first frame holds ``parts``."""
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_obj_file(tmp_path, parts)))
    entry = window._workspace.current
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    entry.doc = None  # re-read through the new binding
    window._activate_entry(window._workspace.entries[0])
    window._activate_entry(entry)
    window.show()
    window._tile_source_dock.setVisible(True)
    window._tile_source_dock.raise_()
    window._refresh_tile_source()
    return window, entry


def test_clicking_the_sheet_picks_an_id_and_not_a_slot(qtbot, tmp_path) -> None:
    """The panel is addressed in tile **IDs** — the numbers the file holds and
    the Cell spin sets — not in positions on a sheet.

    The two only differ once a base tile is in play, which is the ordinary case
    for a map bound to a slice: with base -2 the first ID on offer is 2, so a
    click on the first tile has to report 2. Reported as a slot it would name a
    tile the map cannot draw and the readout would be off by the base.
    """
    from PySide6.QtCore import QPoint, Qt

    from celpix.core.tilemap import Cell

    window, _ = _shown_tile_source(qtbot, tmp_path, [Cell(index=1), Cell(index=3)])
    panel = window._tile_source_panel
    assert panel._ids.start == 0  # eight tiles, indexed absolutely

    window._tile_base.setValue(-2)
    window._refresh_tile_source()
    assert panel._ids.start == 2

    # The second tile of the top row, at one cell's width in.
    cell_px = panel._cell_px[0] * panel._zoom
    qtbot.mouseClick(panel, Qt.MouseButton.LeftButton, pos=QPoint(cell_px + 1, 1))
    assert panel.selected_id() == 3
    assert window._source_tile_id == 3
    assert "$3" in window._tile_source_details.text()


def test_selecting_a_cell_rings_the_tile_it_names(qtbot, tmp_path) -> None:
    """The question a tilemap view cannot otherwise answer: a cell's picture is
    the *tile's*, so nothing says which tile that is or where it lives.

    Driven by the selection pass rather than the render cycle, because a
    selection moves without anything being redrawn — so a marker fed from the
    refresh alone would sit on the cell before last.
    """
    from celpix.core.tilemap import Cell

    window, _ = _shown_tile_source(
        qtbot, tmp_path, [Cell(index=1), Cell(index=4), Cell(index=6)]
    )
    panel = window._tile_source_panel

    window._select_tiles(0, 0)
    assert panel._marked == 1
    window._select_tiles(2, 2)
    assert panel._marked == 6
    window._clear_selection()
    assert panel._marked is None


def test_the_sheet_is_ruled_every_sixteen_tiles(qtbot) -> None:
    """The lattice marks where the *numbering* rolls over — 16 across by 16 down
    is the 0x100-tile page a bank is addressed in — so it has to land on that
    step and nowhere between it. Interior lines only: one at 0 would be a border
    around the widget rather than a division of it."""
    from PySide6.QtGui import QImage

    from celpix.ui.tile_source_panel import GRID_STEP_TILES, TileSourcePanel

    panel = TileSourcePanel()
    qtbot.addWidget(panel)
    # A sheet wide and tall enough to hold two boundaries each way, in a colour
    # nothing in the lattice could be confused with.
    cells, cell_px = 40, 4
    sheet = QImage(cells * cell_px, cells * cell_px, QImage.Format.Format_ARGB32)
    sheet.fill(0xFF00FF00)
    panel.set_zoom(1)
    panel.set_sheet(sheet, range(cells * cells), (cell_px, cell_px), cells)

    painted = panel.grab().toImage()
    step = GRID_STEP_TILES * cell_px

    def _ruled(x: int, y: int) -> bool:
        return painted.pixelColor(x, y).rgb() != sheet.pixelColor(0, 0).rgb()

    assert _ruled(step, 3) and _ruled(2 * step, 3)  # verticals
    assert _ruled(3, step) and _ruled(3, 2 * step)  # horizontals
    assert not _ruled(step // 2, 3)  # nothing between them
    assert not _ruled(3, step // 2)
    assert not _ruled(0, 3) and not _ruled(3, 0)  # no border


def test_set_base_tile_makes_the_picked_tile_the_one_cell_zero_draws(
    qtbot, tmp_path
) -> None:
    """The base is stated in the *source's* tile numbers and the panel is
    addressed in cell IDs, so a pick has to be resolved through the base in force
    before it can replace it — which is the arithmetic the readout above the
    button already prints. Setting it from a picked ID without that step would
    walk the base backwards the moment one was in play."""
    from celpix.core.tilemap import Cell

    window, entry = _shown_tile_source(qtbot, tmp_path, [Cell(index=1)])
    assert not window._set_base_tile_button.isEnabled()  # nothing picked yet

    window._tile_source_panel.select_id(3)
    assert window._set_base_tile_button.isEnabled()
    steps = window._undo_stack.count()
    window._set_base_tile_button.click()
    assert entry.tile_source.base_index == 3
    assert window._tile_base.value() == 3  # the spin is the same value
    # The sheet is re-addressed by the base, so the tile just picked is now ID 0
    # - the ring follows it rather than sliding onto a different picture.
    assert window._source_tile_id == 0

    # A second press composes rather than replacing: ID 2 under a base of 3 is
    # bank tile 5, and that is what becomes cell 0's.
    window._tile_source_panel.select_id(2)
    window._set_base_tile_button.click()
    assert entry.tile_source.base_index == 5

    # The Base tile spin's own step, so it takes back the same way.
    assert window._undo_stack.count() == steps + 2
    window._undo_stack.undo()
    assert entry.tile_source.base_index == 3


def test_set_base_tile_is_offered_on_a_sprite_object_too(qtbot, tmp_path) -> None:
    """A subsprite is not a cell, but it holds a tile number in the same space
    and the base shifts it the same way — which is why the binding bar shows an
    object the Base tile spin at all. Refusing the pointing gesture for the value
    that spin holds would be this panel disagreeing with the bar about one number.

    The tooltip is checked with the gate rather than apart from it: what the base
    moves here is *records*, and a sentence about cells over a file that has none
    is the kind of wrong a user cannot check against anything on screen."""
    window, entry = _shown_sprite_source(qtbot, tmp_path, [(0, 0, 1), (24, 5, 2)])

    window._tile_source_panel.select_id(3)
    assert window._set_base_tile_button.isEnabled()
    assert "subsprite" in window._set_base_tile_button.toolTip()
    assert "cell" not in window._set_base_tile_button.toolTip()

    window._set_base_tile_button.click()
    assert entry.tile_source.base_index == 3
    assert window._tile_base.value() == 3  # the spin is the same value
    assert window._doc.tile_base_index == 3  # ...and the object draws through it


def test_a_subsprite_counts_as_a_user_of_every_tile_it_draws(qtbot, tmp_path) -> None:
    """A subsprite is a *rectangle*, and its record names only the corner of it.

    Counted by that number alone, a 2x2 piece reports one user for its corner
    tile and none for the other three - so the three tiles it plainly draws read
    as spare, which is the opposite of what this line is asked for.
    """
    # A 2x2 record at tile 1, which draws 1, 2 and the row below (17, 18); and a
    # 1x1 record naming tile 2, so that tile has two users by two different routes.
    window, _ = _shown_sprite_source(qtbot, tmp_path, [(0, 0, 1, 0, True), (24, 5, 2)])

    window._tile_source_panel.select_id(2)
    assert "used by 2 subsprites" in window._tile_source_details.text()

    # The corner tile is the one record that names it, counted once and not twice.
    window._tile_source_panel.select_id(1)
    assert "used by 1 subsprite." in window._tile_source_details.text()

    # And a tile no record reaches is still spare - the rectangle widens the
    # count, it does not blur it.
    window._tile_source_panel.select_id(4)
    assert "used by 0 subsprites" in window._tile_source_details.text()


def test_the_sheet_reads_in_the_selected_cells_palette_row(qtbot, tmp_path) -> None:
    """A bank is indices until a row is chosen for it, so the panel showing row 0
    while the cell you clicked draws in row 2 is the right art in the wrong
    colours. With nothing selected the Subpal row answers, which is the row the
    palette dock's own selection sets."""
    from celpix.core.tilemap import Cell

    window, _ = _shown_tile_source(
        qtbot, tmp_path, [Cell(index=1, palette_row=2), Cell(index=1)]
    )
    assert window._doc.cells_carry_palette_rows
    window._subpalette.setValue(0)
    window._clear_selection()
    window._refresh_tile_source()
    assert window._tile_source_row() == 0
    row0 = window._tile_source_panel._sheet.copy()

    # The selected cell's row, and the sheet is recomposed for it - a row is
    # folded into the indices, so it cannot be applied to a finished sheet.
    window._select_tiles(0, 0)
    assert window._tile_source_row() == 2
    assert window._tile_source_row_shown == 2
    assert window._tile_source_panel._sheet != row0

    # Cell 1 is row 0, so moving there puts the sheet back.
    window._select_tiles(1, 1)
    assert window._tile_source_row_shown == 0
    assert window._tile_source_panel._sheet == row0

    # No selection: the Subpal row, wherever the user last set it.
    window._clear_selection()
    window._subpalette.setValue(3)
    window._refresh_tile_source()
    assert window._tile_source_row() == 3
    assert window._tile_source_panel._sheet != row0


def test_ctrl_wheel_zooms_the_sheet_and_a_plain_wheel_is_left_to_scroll(
    qtbot, tmp_path
) -> None:
    """The canvas's gesture on the canvas's terms, driving the Zoom spin so the
    wheel, the spin and the keyboard stay one value."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    from celpix.core.tilemap import Cell

    window, _ = _shown_tile_source(qtbot, tmp_path, [Cell(index=1)])
    panel = window._tile_source_panel
    window._tile_source_zoom.setValue(2)

    def _wheel(dy, modifier=Qt.KeyboardModifier.ControlModifier):
        return QWheelEvent(
            QPointF(4, 4),
            QPointF(4, 4),
            QPoint(0, 0),
            QPoint(0, dy),
            Qt.MouseButton.NoButton,
            modifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )

    panel.wheelEvent(_wheel(120))
    assert window._tile_source_zoom.value() == 3 and panel._zoom == 3
    panel.wheelEvent(_wheel(-120))
    assert window._tile_source_zoom.value() == 2

    # No Ctrl: passed up rather than accepted, which is what lets the scroll
    # area it sits in scroll on it.
    plain = _wheel(120, modifier=Qt.KeyboardModifier.NoModifier)
    panel.wheelEvent(plain)
    assert window._tile_source_zoom.value() == 2
    assert not plain.isAccepted()

    # The backing beside the sheet answers the same gesture: a short bank leaves
    # most of the dock empty, and a zoom that only works over the tiles reads as
    # a broken wheel rather than as a missed target. The anchor is clamped into
    # the sheet, since a point out on the grey is not a content pixel to hold
    # still.
    from PySide6.QtWidgets import QApplication

    viewport = window._tile_source_scroll.viewport()
    beyond = QPointF(panel.width() + 20, panel.height() + 20)
    backing = QWheelEvent(
        beyond,
        beyond,
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(viewport, backing)
    assert window._tile_source_zoom.value() == 3


def test_the_cols_keys_widen_the_sheet_while_it_holds_the_focus(
    qtbot, tmp_path, monkeypatch
) -> None:
    """Shift+arrow is bound app-wide for the view's width, and the sheet is a
    second grid with a width of its own - so working in the sheet has to point
    the keys at the spin that lays *it* out, or they re-lay the picture the user
    is not looking at.

    focusWidget is monkeypatched because real focus delivery is
    environment-dependent."""
    from PySide6.QtWidgets import QApplication

    from celpix.core.tilemap import Cell

    window, _ = _shown_tile_source(qtbot, tmp_path, [Cell(index=1)])
    view_cols = window._columns.value()
    sheet_cols = window._tile_source_columns.value()

    monkeypatch.setattr(
        QApplication, "focusWidget", staticmethod(lambda: window._tile_source_panel)
    )
    window._adjust_columns(1)
    assert window._tile_source_columns.value() == sheet_cols + 1
    assert window._columns.value() == view_cols

    # Anywhere else, including the canvas, and the keys mean the view's width.
    monkeypatch.setattr(
        QApplication, "focusWidget", staticmethod(lambda: window._canvas)
    )
    window._adjust_columns(-1)
    assert window._columns.value() == view_cols - 1
    assert window._tile_source_columns.value() == sheet_cols + 1


def test_clicking_the_backing_puts_the_focus_on_the_sheet(qtbot, tmp_path) -> None:
    """The grey around a short sheet is the sheet as far as a user is concerned.

    Qt hands the focus to the scroll area on a press out there, which left the
    keys addressed to the sheet - its width, its pick - pointed at the view
    instead, the same miss the wheel zoom used to have.
    """
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from celpix.core.tilemap import Cell

    window, _ = _shown_tile_source(qtbot, tmp_path, [Cell(index=1)])
    panel = window._tile_source_panel
    viewport = window._tile_source_scroll.viewport()
    QApplication.setActiveWindow(window)
    window._canvas.setFocus()

    beyond = QPointF(panel.width() + 20, panel.height() + 20)
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        beyond,
        QPointF(viewport.mapToGlobal(QPoint(0, 0))) + beyond,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(viewport, press)

    assert QApplication.focusWidget() is panel
    # Which is the whole point: the Cols keys now lay out the sheet.
    sheet_cols = window._tile_source_columns.value()
    view_cols = window._columns.value()
    window._adjust_columns(1)
    assert window._tile_source_columns.value() == sheet_cols + 1
    assert window._columns.value() == view_cols


def test_space_pans_whichever_surface_the_pointer_is_over(qtbot, tmp_path) -> None:
    """The key is filtered app-wide, so one place has to decide which surface it
    arms - and the other has to go down, or a pointer that moved mid-hold would
    leave it holding an open hand and eating the next press it got.

    The **pointer** decides, not the focus ring: the sheet is reached with the
    mouse, usually straight from the Zoom spin that made it too big for its dock
    and without a tile ever being clicked, so a focused-widget rule left the
    gesture dead exactly where it is most wanted."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QApplication

    from celpix.core.tilemap import Cell

    window, _ = _shown_tile_source(qtbot, tmp_path, [Cell(index=1)])
    panel = window._tile_source_panel
    # Offscreen, setFocus only lands once the window is active.
    QApplication.setActiveWindow(window)
    window._tile_source_zoom.setFocus()  # where using the spin leaves it

    qtbot.mouseMove(panel, QPoint(4, 4))
    qtbot.keyPress(panel, Qt.Key.Key_Space)
    assert panel._pan_active and not window._canvas._pan_active
    # The hand is worn by the surface that pans, by the backing it has claimed as
    # its own, and by nothing else. That is what keeps the arming rule honest now
    # that a press decides nothing about it: the cursor is a property of the
    # widget, so the pixels showing an open hand are exactly the pixels a press
    # would pan - which the surround now is (`PanZoomSurface.claim_background`).
    # An application override cursor would put a hand over every dock instead.
    assert panel.cursor().shape() is Qt.CursorShape.OpenHandCursor
    assert (
        window._tile_source_scroll.viewport().cursor().shape()
        is Qt.CursorShape.OpenHandCursor
    )
    assert window._canvas.cursor().shape() is not Qt.CursorShape.OpenHandCursor
    # An armed pan takes the mouse from the pick, as it does on the canvas.
    qtbot.mousePress(panel, Qt.MouseButton.LeftButton, pos=QPoint(1, 1))
    assert panel.selected_id() is None
    qtbot.mouseRelease(panel, Qt.MouseButton.LeftButton, pos=QPoint(1, 1))
    qtbot.keyRelease(panel, Qt.Key.Key_Space)
    assert not panel._pan_active

    qtbot.mouseMove(window._canvas, QPoint(4, 4))
    qtbot.keyPress(window._canvas, Qt.Key.Key_Space)
    assert window._canvas._pan_active and not panel._pan_active
    qtbot.keyRelease(window._canvas, Qt.Key.Key_Space)

    # A pointer over a sheet its shared tab bar has buried is over nothing: the
    # sheet's own rectangle still covers those pixels, its visible region does not.
    window._palette_dock.raise_()
    qtbot.mouseMove(panel, QPoint(4, 4))
    assert window._pan_surface() is window._canvas


def test_a_held_space_is_let_go_when_the_window_stops_being_active(
    qtbot, tmp_path
) -> None:
    """Alt-tab mid-hold and the release lands in whatever was raised, so the
    window never sees the key come up. Nothing else would take the arm down: it
    is put up by a filter that only runs while this window is active, and the
    surface would sit holding an open hand and swallow the first click of
    whoever came back to it."""
    from PySide6.QtCore import QEvent, QPoint, Qt
    from PySide6.QtWidgets import QApplication

    from celpix.core.tilemap import Cell

    window, _ = _shown_tile_source(qtbot, tmp_path, [Cell(index=1)])
    QApplication.setActiveWindow(window)
    qtbot.mouseMove(window._canvas, QPoint(4, 4))
    qtbot.keyPress(window._canvas, Qt.Key.Key_Space)
    assert window._canvas._pan_active

    # What alt-tabbing away delivers, and what the pan filter listens for
    # (``navigation.py``: WindowDeactivate on the window itself).
    QApplication.sendEvent(window, QEvent(QEvent.Type.WindowDeactivate))
    assert not window._canvas._pan_active
    assert not window._tile_source_panel._pan_active


# -- the stamp tool --------------------------------------------------------
def _stamping(qtbot, tmp_path, cells):
    """A bound tilemap with Edit Tiles armed and a tile held."""
    window, entry = _shown_tile_source(qtbot, tmp_path, cells)
    window._stamp_action.setChecked(True)
    return window, entry


def test_edit_tiles_is_offered_on_a_tilemap_and_nowhere_else(qtbot, tmp_path) -> None:
    """The mode is hidden rather than greyed off a tilemap: a tool for placing
    cells is not a feature switched off on a pixel document, it is furniture for
    a different room — the reading the pixel tools rail already gets.

    It also has to *disarm* itself when the document moves out from under it, or
    a mode with no cells to act on stays latched over the next entry.
    """
    from celpix.core.tilemap import Cell

    window, tilemap = _stamping(qtbot, tmp_path, [Cell(index=1), Cell(index=2)])
    assert window._stamp_action.isVisible()
    assert window._stamp_action.isEnabled()
    assert window._stamping

    # Entry 0 is the tile bank the map draws from — an ordinary pixel entry.
    window._activate_entry(window._workspace.entries[0])
    assert not window._stamping
    assert not window._stamp_action.isVisible()
    assert not window._toggle_stamp_action.isVisible()

    window._activate_entry(tilemap)
    assert window._stamp_action.isVisible()


def test_a_stamp_drag_is_one_undoable_step(qtbot, tmp_path) -> None:
    """A drag across cells is one gesture, so it undoes as one.

    Pushed per cell it would take four Ctrl+Z to take back one sweep, which is
    the pixel pen's problem and has the pixel pen's answer: preview on the live
    document, commit the stroke on release.
    """
    from PySide6.QtCore import Qt

    from celpix.core.tilemap import Cell

    window, _ = _stamping(qtbot, tmp_path, [Cell(index=c) for c in (1, 2, 3, 4)])
    window._tile_source_panel.select_id(7)
    assert window._source_tile_id == 7
    depth = window._undo_stack.count()

    window._on_stamp_pressed(0, Qt.MouseButton.LeftButton)
    window._on_stamp_moved(1)
    window._on_stamp_moved(2)
    # Live while the drag is in progress — the map has to show what is landing.
    assert [c.index for c in window._doc.cells[:4]] == [7, 7, 7, 4]
    assert window._undo_stack.count() == depth  # nothing pushed yet

    window._on_stamp_finished()
    assert window._undo_stack.count() == depth + 1
    window._undo_stack.undo()
    assert [c.index for c in window._doc.cells[:4]] == [1, 2, 3, 4]


def test_a_stamp_keeps_the_cell_s_own_attributes(qtbot, tmp_path) -> None:
    """Pointing a cell at another tile is not rebuilding the cell: its palette
    row, flips and carried ``flags`` are as likely to be what the user set up as
    what they meant to replace, so only the index moves — the rule the Cell spin
    already follows."""
    from dataclasses import replace

    from PySide6.QtCore import Qt

    from celpix.core.tilemap import Cell

    window, _ = _stamping(qtbot, tmp_path, [Cell(index=1, palette_row=3, flip_h=True)])
    # Read back rather than assumed: what the file carries is the format's
    # answer, and only the index moving is this test's.
    before = window._doc.cells[0]
    assert (before.index, before.palette_row, before.flip_h) == (1, 3, True)

    window._tile_source_panel.select_id(5)
    window._on_stamp_pressed(0, Qt.MouseButton.LeftButton)
    window._on_stamp_finished()
    assert window._doc.cells[0] == replace(before, index=5)


def test_right_click_picks_the_tile_the_cell_names(qtbot, tmp_path) -> None:
    """The eyedropper, and the number it takes is the cell's own index *before*
    the binding's base tile — the space the panel is addressed in, so picking
    here and looking there cannot disagree.

    It has to reach the held tile directly rather than through the panel: the
    dock composes nothing while its tab is in the background, so a pick routed
    through the sheet would be dropped exactly when the panel is not on screen.
    """
    from PySide6.QtCore import Qt

    from celpix.core.tilemap import Cell

    window, _ = _stamping(qtbot, tmp_path, [Cell(index=1), Cell(index=6)])
    window._tile_source_dock.setVisible(False)  # the usual state: a background tab

    window._on_stamp_pressed(1, Qt.MouseButton.RightButton)
    assert window._source_tile_id == 6

    # And it is what a following stamp lays down.
    window._on_stamp_pressed(0, Qt.MouseButton.LeftButton)
    window._on_stamp_finished()
    assert [c.index for c in window._doc.cells[:2]] == [6, 6]


def test_the_eyedropper_takes_the_cells_palette_row_with_it(qtbot, tmp_path) -> None:
    """A tile picked without its row stamps back in whatever colours Subpal was
    left on — so the pick moves the row the way a left-click selection does.

    Subpal is the drawn row, so what lands is the row the palette grid rings and
    the tile sheet is read in: the file's number with the entry's base applied.
    """
    from PySide6.QtCore import Qt

    from celpix.core.tilemap import Cell

    window, _ = _stamping(
        qtbot,
        tmp_path,
        [Cell(index=1, palette_row=2), Cell(index=6, palette_row=5)],
    )
    assert window._doc.cells_carry_palette_rows  # the fixture has rows to take
    window._row_base.setValue(4)  # a base, so "stored" and "drawn" differ
    base = window._doc.palette_row_base
    assert base == 4
    window._subpalette.setValue(0)

    window._on_stamp_pressed(1, Qt.MouseButton.RightButton)
    assert window._source_tile_id == 6
    assert window._subpalette.value() == 5 + base
    assert f"palette row {5 + base}" in window.statusBar().currentMessage()

    # The neighbouring cell answers with its own, so the row tracks the pick
    # rather than being set once and left.
    window._on_stamp_pressed(0, Qt.MouseButton.RightButton)
    assert window._subpalette.value() == 2 + base


def test_a_stamp_lays_down_the_whole_cell_the_eyedropper_took(qtbot, tmp_path) -> None:
    """ "Put that one here" means the cell, not just its tile number: the row, the
    flips and the priority the codec carries all travel with the pick.

    A tile picked in the **sheet** has no such record behind it, so that path
    still sets the index and leaves the target's own attributes alone — and
    reaching for the sheet after an eyedrop drops the held record rather than
    stamping a stale one.
    """
    from dataclasses import replace

    from PySide6.QtCore import Qt

    from celpix.core.tilemap import Cell

    window, _ = _stamping(
        qtbot,
        tmp_path,
        [
            Cell(index=6, palette_row=5, flip_h=True, flip_v=True, priority=1),
            Cell(index=1, palette_row=2),
        ],
    )
    # Read the source back off the document: what a stamp can carry is what this
    # format round-trips, and asserting against a literal would test the codec.
    source = window._doc.cells[0]
    assert (source.palette_row, source.flip_h, source.priority) == (5, True, 1)

    window._on_stamp_pressed(0, Qt.MouseButton.RightButton)
    window._on_stamp_pressed(1, Qt.MouseButton.LeftButton)
    window._on_stamp_finished()
    assert window._doc.cells[1] == source

    # A sheet pick carries an ID alone, so the target keeps what it has.
    before = window._doc.cells[1]
    window._tile_source_panel.select_id(3)
    window._on_stamp_pressed(1, Qt.MouseButton.LeftButton)
    window._on_stamp_finished()
    assert window._doc.cells[1] == replace(before, index=3)


def test_edit_tiles_suppresses_the_canvas_menu(qtbot, tmp_path, monkeypatch) -> None:
    """The right button is the tool's eyedropper, so the popup that button
    normally opens has to stay down — otherwise it lands on top of the pick.

    Asserted at the Python level: reaching ``menu.exec()`` under the offscreen
    platform would block the run, which is what the guard avoids.
    """
    from PySide6.QtCore import QPoint

    from celpix.core.tilemap import Cell

    window, _ = _stamping(qtbot, tmp_path, [Cell(index=1)])

    def _boom():
        raise AssertionError("built the context menu with Edit Tiles armed")

    # The guard must return before the menu pulls its first actions.
    monkeypatch.setattr(window, "_clipboard_actions", _boom)
    window._show_canvas_menu(QPoint(0, 0))  # returns early: no menu, no raise


def test_stamping_with_nothing_held_says_so(qtbot, tmp_path) -> None:
    """A click that lays nothing down with no reason given is the worst of the
    outcomes: the tool is armed, the cursor is a cross, and the map does not
    change. Reachable on a fresh session, before anything has been picked."""
    from PySide6.QtCore import Qt

    from celpix.core.tilemap import Cell

    window, _ = _stamping(qtbot, tmp_path, [Cell(index=1)])
    assert window._source_tile_id is None

    window._on_stamp_pressed(0, Qt.MouseButton.LeftButton)
    assert window._doc.cells[0].index == 1
    assert "Tile Source" in window.statusBar().currentMessage()


# -- pixel editing through a tilemap ---------------------------------------
def _painting_on(window, *, tool=None):
    """Put ``window`` into pixel mode with a drawing tool armed."""
    from celpix.ui.tools import EditMode, Tool

    window._set_edit_mode(EditMode.PIXEL)
    window._on_tool_selected(tool or Tool.PENCIL)


def _paint(window, x, y, index):
    """One pencil click at canvas pixel ``(x, y)`` in palette ``index``."""
    from PySide6.QtCore import Qt

    window._palette_panel.select_index(index)
    window._on_pixel_pressed(x, y, Qt.MouseButton.LeftButton)
    window._on_pixel_released(x, y)


def _bank_tile(window, entry, index):
    """Bank tile ``index`` as ``entry``'s document currently holds it."""
    from celpix.pipeline import pipeline

    return pipeline.decode_tiles(entry.doc, window._registry, index, 1)[0]


def test_painting_a_map_deposits_into_the_entry_it_borrows_tiles_from(
    qtbot, tmp_path, monkeypatch
) -> None:
    """The whole of the feature in one step. A tilemap's ``pixel_data`` is a copy
    of the bound entry's art, so an edit that only spliced it would be visible,
    unsavable and gone on the next reload. It has to land in the entry that owns
    those bytes — which for a map bound to a *slice* means the slice **and** the
    file it is a window into, the fold that rule already does
    (``docs/design/slices-and-parents.md``)."""
    from celpix.core.tilemap import Cell

    window, tilemap, sliced = _bound_to_slice(
        qtbot, tmp_path, monkeypatch, [Cell(index=i) for i in range(16)]
    )
    rom = window._workspace.entries[0]
    window._activate_entry(sliced)
    window._activate_entry(tilemap)
    _painting_on(window)

    # Cell 2 sits at canvas x 16..23 and draws bank tile 2.
    _paint(window, 16, 0, 9)

    assert _bank_tile(window, sliced, 2).get(0, 0) == 9
    assert sliced.pixel_dirty and rom.pixel_dirty  # the bytes are one set
    assert not tilemap.pixel_dirty  # its own data is its cells, and they stand
    # The map's borrowed copy is kept in step, so the picture agrees with the file.
    assert tilemap.doc.pixel_data == sliced.doc.pixel_data

    window._undo_stack.undo()
    assert _bank_tile(window, sliced, 2).get(0, 0) != 9
    assert not sliced.pixel_dirty and not rom.pixel_dirty
    # Undo comes back to the picture the stroke was drawn on, not to the bank.
    assert window._workspace.current is tilemap


def test_painting_through_a_flipped_cell_stores_the_tile_unmirrored(
    qtbot, tmp_path
) -> None:
    """A mirrored cell shows its tile the other way round, so a stroke on its left
    edge belongs at the tile's right. Miss the un-flip and the mirror bakes itself
    into the file — the art then comes apart the moment the same tile is drawn
    somewhere unflipped, which is the ordinary case for a mirrored one."""
    from celpix.core.tilemap import Cell

    window, bank, _entry = _bound_screen(
        qtbot, tmp_path, [Cell(index=0), Cell(index=1, flip_h=True)]
    )
    window._activate_entry(bank)
    window._activate_entry(window._workspace.entries[1])
    _painting_on(window)

    _paint(window, 8, 0, 5)  # the leftmost pixel of the flipped cell

    tile = _bank_tile(window, bank, 1)
    assert tile.get(7, 0) == 5  # stored at the far edge
    assert tile.get(0, 0) != 5


def test_painting_a_cell_stores_its_palette_row_relative_index(qtbot, tmp_path) -> None:
    """A cell's row is folded into the *indices* to reach the screen, so what is
    composed is absolute palette indices while the tile stores a row-relative one.
    Storing the absolute number would write an index the format cannot hold, and
    the same tile drawn on another row would come back a different colour."""
    from celpix.core.tilemap import Cell

    window, bank, _entry = _bound_screen(
        qtbot, tmp_path, [Cell(index=0), Cell(index=1, palette_row=2)]
    )
    window._activate_entry(bank)
    window._activate_entry(window._workspace.entries[1])
    _painting_on(window)

    _paint(window, 8, 0, 0x21)  # colour 1 of row 2, as the picture shows it

    assert _bank_tile(window, bank, 1).get(0, 0) == 1


def _bound_object(qtbot, tmp_path, parts):
    """A window with a 4bpp bank at entry 0 and a sprite object bound to it.

    ``parts`` is ``_obj_file``'s — ``(x, y, tile)`` or ``(x, y, tile, row)``.
    """
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    bank = window._workspace.entries[0]
    obj = _obj_file(tmp_path, parts)
    window._load_pixel(str(obj))
    entry = window._workspace.find_file(str(obj))
    window._activate_entry(entry)
    window._rebind_tiles(entry, TileSource(mode=TileMode.ENTRY, entry=bank))
    return window, bank, entry


def test_painting_a_sprite_object_deposits_into_the_bank_it_borrows_from(
    qtbot, tmp_path
) -> None:
    """The tilemap feature over the other shape. An object's ``pixel_data`` is the
    bound entry's art exactly as a grid map's is, so the edit has to land there
    and not in the map — whose own data is its records and whose save writes
    those.

    What differs is only the way *in*: a piece sits at a signed pixel offset, so
    which one owns a pixel is the overlap order rather than a slot
    (``docs/design/tilemap-entry.md`` §8.5).
    """
    window, bank, obj = _bound_object(qtbot, tmp_path, [(0, 0, 2)])
    _painting_on(window)
    assert window._pixel_edit_available()

    _paint(window, 1, 1, 9)

    assert _bank_tile(window, bank, 2).get(1, 1) == 9
    assert bank.pixel_dirty
    assert not obj.pixel_dirty  # its own data is its records, and they stand
    assert obj.doc.pixel_data == bank.doc.pixel_data  # the borrowed copy kept up

    window._undo_stack.undo()
    assert _bank_tile(window, bank, 2).get(1, 1) != 9
    assert not bank.pixel_dirty
    assert window._workspace.current is obj  # back to the picture it was drawn on


def test_the_pixel_clipboard_is_offered_on_a_sprite_object(qtbot, tmp_path) -> None:
    """What Cut, Clear and Paste need to be able to write is a different thing in
    each mode, and asking the *cell* question in pixel mode refused an object the
    gestures it does have: its cells are pieces at signed offsets, so there is
    none under the cursor to blank, while the pixels those pieces draw are as
    editable as a grid map's (§8.5). The brush was offered and taking back what it
    laid down was not.

    Tile mode is the other half of the same rule: there the cells really are what
    a Cut would blank, so the object still refuses.
    """
    from PySide6.QtCore import QRect

    from celpix.ui.tools import EditMode

    window, bank, _obj = _bound_object(qtbot, tmp_path, [(0, 0, 2)])
    _painting_on(window)
    window._marquee = QRect(0, 0, 8, 8)
    window._sync_edit_actions()

    assert window._copy_action.isEnabled()
    assert window._cut_action.isEnabled()
    assert window._clear_action.isEnabled()

    # And they act: a cut reaches the bank tile the piece draws, as one step.
    before = [_bank_tile(window, bank, 2).get(x, 0) for x in range(8)]
    window._cut_selection()
    assert [_bank_tile(window, bank, 2).get(x, 0) for x in range(8)] != before
    window._sync_edit_actions()
    assert window._paste_action.isEnabled()  # the cut is on the clipboard
    window._undo_stack.undo()
    assert [_bank_tile(window, bank, 2).get(x, 0) for x in range(8)] == before

    # Tile mode asks the cell question, and an object has no cell to blank.
    window._set_edit_mode(EditMode.TILE)
    window._select_tiles(0, 0)
    window._sync_edit_actions()
    assert window._copy_action.isEnabled()  # a read: the sheet's pixels
    assert not window._cut_action.isEnabled()
    assert not window._clear_action.isEnabled()


def test_painting_a_sprite_piece_undoes_its_flip_and_its_palette_row(
    qtbot, tmp_path
) -> None:
    """Both of the contributions a piece makes to what is on screen, taken back
    off the way a cell's are. The offsets are what make this its own test: a
    piece is placed at a signed pixel offset, so the pixel inside the tile is not
    the canvas position modulo the tile size, and getting that wrong writes a
    neighbouring pixel of the right tile — visible, plausible and wrong.
    """
    from dataclasses import replace

    window, bank, obj = _bound_object(qtbot, tmp_path, [(0, 0, 3, 2)])
    # Mirror the piece and shift it off the tile grid, both through the document
    # so the record's own decode is what placed it.
    frame = obj.doc.sprite_frames[0]
    obj.doc.sprite_frames[0] = (replace(frame[0], flip_h=True, x=3, y=5),)
    window._refresh_view()
    _painting_on(window)

    # The piece's top-left pixel now sits at canvas (3, 5), and it is mirrored,
    # so that pixel is the tile's *far* column. Row 2 is folded into the picture.
    _paint(window, 3, 5, 0x21)

    tile = _bank_tile(window, bank, 3)
    assert tile.get(7, 0) == 1  # mirrored across, and stored row-relative
    assert tile.get(0, 0) != 1


def test_one_stroke_over_two_cells_drawing_one_tile_leaves_the_last(
    qtbot, tmp_path
) -> None:
    """A map's picture is a many-to-one scatter of its bank, so a gesture can
    reach one tile from several cells — and then the two halves of the stroke
    disagree about what that tile should hold. There is no answer that keeps
    both, and every cell drawing it repaints together, so the rule is that the
    last slot wins. Worth pinning because what is *lost* is invisible: the
    stroke's first half looks applied until the repaint takes it away."""
    from PySide6.QtCore import Qt

    from celpix.core.tilemap import Cell
    from celpix.ui.tools import Tool

    window, bank, _entry = _bound_screen(
        qtbot, tmp_path, [Cell(index=3), Cell(index=3)]
    )
    window._activate_entry(bank)
    window._activate_entry(window._workspace.entries[1])
    _painting_on(window, tool=Tool.LINE)
    was = _bank_tile(window, bank, 3)
    assert was.get(4, 0) != 7  # the pixel the first half claims, before anything

    # One drag from cell 0's top-left across into cell 1: it covers all eight
    # pixels of the tile through the first cell and only two through the second.
    window._palette_panel.select_index(7)
    window._on_pixel_pressed(0, 0, Qt.MouseButton.LeftButton)
    window._on_pixel_moved(9, 0)
    window._on_pixel_released(9, 0)

    tile = _bank_tile(window, bank, 3)
    assert [tile.get(x, 0) for x in (0, 1)] == [7, 7]  # what cell 1 painted
    assert tile.get(4, 0) == was.get(4, 0)  # what cell 0 painted, overwritten


def test_painting_a_metatile_cell_writes_the_tile_its_stride_names(
    qtbot, tmp_path
) -> None:
    """A cell covering four tiles does not take them from four consecutive
    indices: the second row is ``cell_row_stride`` away, because the hardware
    reads the map against a tile array wider than the cell. So a stroke in the
    lower half of a cell belongs to a tile nowhere near the upper half's, and
    getting it wrong writes over somebody else's art rather than failing."""
    from celpix.core.tilemap import Cell

    window, bank, entry = _bound_screen(qtbot, tmp_path, [Cell(index=0)])
    entry.doc.cell_tiles = (2, 2)
    entry.doc.cell_row_stride = 4  # cell 0 draws tiles 0, 1, 4, 5
    window._activate_entry(bank)
    window._activate_entry(entry)
    _painting_on(window)

    _paint(window, 0, 8, 6)  # the cell's lower-left tile

    assert _bank_tile(window, bank, 4).get(0, 0) == 6  # the stride's answer
    assert _bank_tile(window, bank, 2).get(0, 0) != 6  # not the next tile along


def test_painting_a_stamp_layout_reaches_the_bank_at_the_end_of_the_chain(
    qtbot, tmp_path
) -> None:
    """A layout's cells are coordinates into a panel and the panel's are tiles of
    a bank, so the art is two hops away and belongs to neither map. The deposit
    has to walk to it — and must leave the panel's own cells alone, since what
    was edited is a tile, not the stamp that names it."""
    window = MainWindow()
    qtbot.addWidget(window)
    layout = _bound_stamp_layout(window, tmp_path)
    bank, panel = window._workspace.entries[0], window._workspace.entries[1]
    window._activate_entry(bank)
    window._activate_entry(layout)
    _painting_on(window)
    cells = list(panel.doc.cells)

    _paint(window, 0, 0, 9)

    assert _bank_tile(window, bank, 1).get(0, 0) == 9
    assert bank.pixel_dirty
    assert not panel.pixel_dirty and not layout.pixel_dirty
    assert panel.doc.cells == cells  # a pixel edit is not a restamp


def test_a_bank_edited_in_its_own_view_shows_through_in_an_open_map(
    qtbot, tmp_path
) -> None:
    """A map holds a decoded copy of its bank, so an edit to the bank reaches it
    only if it is put there. Without that the two views of one file disagree
    until something reloads — which is the promise a binding makes by naming an
    *entry* rather than a path."""
    from celpix.core.tilemap import Cell

    window, bank, entry = _bound_screen(qtbot, tmp_path, [Cell(index=1)])
    window._activate_entry(bank)
    _painting_on(window)

    _paint(window, 8, 0, 6)  # bank tile 1, in the bank's own view

    assert entry.doc.pixel_data == bank.doc.pixel_data
    # And a re-read of the map takes the *unsaved* bytes, not the file's.
    window._reload_tilemap(entry)
    assert _bank_tile(window, entry, 1).get(0, 0) == 6


def test_pixel_mode_is_unavailable_where_a_gesture_would_reach_nothing(
    qtbot, tmp_path
) -> None:
    """Two documents have no tile under a canvas pixel: a map with nothing bound,
    and a sprite object, whose records overlap at signed offsets so what a pixel
    belongs to is an overlap order rather than a slot. The mode is app-wide and
    nothing else resets it, so arriving on one of them from a pixel-mode session
    has to put the brush down — otherwise the canvas keeps reporting gestures
    that land in a borrowed buffer and mark the wrong entry dirty."""
    from celpix.core.tilemap import Cell
    from celpix.ui.tools import EditMode

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    _painting_on(window)

    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))  # nothing bound
    assert window._edit_mode is EditMode.TILE
    assert not window._edit_mode_action.isEnabled()
    assert not window._pixel_edit_available()

    window._load_pixel(str(_obj_file(tmp_path, [(0, 0, 1)])))
    assert not window._pixel_edit_available()


def test_closing_an_entry_never_re_points_a_binding_at_another_file(
    qtbot, tmp_path, monkeypatch
) -> None:
    """A binding names an entry, not a place in the list. Held positionally,
    removing anything ahead of the bound entry shifted every later binding onto
    its neighbour — which drew the wrong art while a binding was only read, and
    would *write* into a file the user never pointed at now that a stroke is
    deposited into the entry it names. Closing the bank itself is the other half:
    the binding keeps holding it and simply stops resolving, so undoing the close
    binds the maps back with nothing to restore."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    spare = tmp_path / "spare.4bpp"
    spare.write_bytes(bytes(32 * 8))
    window._load_pixel(str(spare))
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    entry = window._workspace.current
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[1]
    )
    window._reload_tilemap(entry)
    bound = window._workspace.entries[1]

    window._apply_close_entry(window._workspace.entries[0])

    assert window._binding_target(entry.tile_source) is bound  # followed, not shifted

    # And closing the bound entry unbinds without losing what it was bound to, so
    # undo needs no snapshot of its own to put the binding back.
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(
        QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Yes
    )
    window._remove_entry(bound)
    assert window._binding_target(entry.tile_source) is None
    assert entry.tile_source.entry is bound  # kept, so undo has nothing to restore
    window._undo_stack.undo()
    assert window._binding_target(entry.tile_source) is bound


def test_the_eyedropper_reads_a_map_without_composing_it(qtbot, tmp_path) -> None:
    """A right-click wants one pixel, and a map's picture is the whole file — so
    the sample resolves the single tile under the cursor instead of building an
    image to read one value out of. The risk in that is the fast path and the
    composed picture disagreeing, so this pins them equal over every pixel of a
    cell drawn plainly, one mirrored, and one on a palette row of its own."""
    from celpix.core.tilemap import Cell

    window, bank, entry = _bound_screen(
        qtbot,
        tmp_path,
        [
            Cell(index=1),
            Cell(index=2, flip_h=True, flip_v=True),
            Cell(index=3, palette_row=2),
        ],
    )
    window._activate_entry(bank)
    window._activate_entry(entry)
    _painting_on(window)

    composed = window._window_grid()
    for x in range(24):  # the three cells, every pixel of their top rows
        for y in range(8):
            assert window._sampled_value(x, y) == composed.get(x, y), (x, y)


# -- the animation player --------------------------------------------------
def _obj_with_sequence(tmp_path, steps, parts=((0, 0, 1),), name="anim.OBJ"):
    """A sprite object whose first animation group holds ``steps``.

    ``steps`` are ``(duration, frame)`` pairs, written the way the file stores
    them - duration byte first - and terminated the way the tool's writer does.
    """
    from celpix.plugins.builtins.scgcad import HEADER, OBJ_PAYLOADS, OBJ_SIZE, SIGNATURE

    out = bytearray(OBJ_SIZE)
    for at, (x, y, tile) in enumerate(parts):
        out[at * 6 : at * 6 + 6] = bytes((0x80, 0, y & 0xFF, x & 0xFF)) + (
            tile & 0x1FF
        ).to_bytes(2, "big")
    payload = OBJ_PAYLOADS[0]
    out[payload : payload + len(SIGNATURE)] = SIGNATURE
    table = payload + HEADER
    for at, (duration, frame) in enumerate(steps):
        out[table + at * 2 : table + at * 2 + 2] = bytes((duration, frame))
    path = tmp_path / name
    path.write_bytes(bytes(out))
    return path


def test_the_player_can_show_a_frame_the_canvas_strip_trims(qtbot, tmp_path) -> None:
    """The canvas stops after the last frame holding a drawn subsprite, which is
    right for a file whose trailing slots hold a template rather than art. A
    *sequence* can name a frame past that - 349 of the corpus's objects do - so
    the player composes its own untrimmed strip and every slot has a rectangle.

    The regression this guards is handing the player the canvas's strip, which
    would report a perfectly real frame as one the file does not have.
    """
    window = MainWindow()
    qtbot.addWidget(window)
    # Only frame 0 is drawn, so the canvas trims to one frame - and the sequence
    # names frame 3, four slots past where the trim stops.
    window._load_pixel(str(_obj_with_sequence(tmp_path, [(2, 0), (2, 3)])))

    assert len(window._doc.shown_frames) == 1  # the trim really is biting
    assert window._animation_action.isEnabled()
    window._show_animation()
    assert window._animation.isVisible()

    assert len(window._animation._rects) == 32  # every slot the file has
    window._animation._advance(1)  # onto the step naming frame 3
    assert "frame 3" in window._animation._status.currentMessage()
    assert "not in file" not in window._animation._status.currentMessage()


def test_a_step_naming_a_frame_the_file_lacks_says_so_and_warns(
    qtbot, tmp_path
) -> None:
    """The table carries whatever was in the tool's buffer past its terminator, so
    a step can name a frame that was never drawn - 7,019 of them across the
    corpus. The picture cannot show that, so the status line and an amber badge
    do; the step is still walked rather than skipped."""
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_obj_with_sequence(tmp_path, [(2, 0), (2, 200)])))
    window._show_animation()
    player = window._animation

    assert player._badge.isVisible()
    assert "1 missing frame" in player._badge.text()
    player._advance(1)
    assert "not in file" in player._status.currentMessage()
    # Walked, not skipped: one more step comes back to where it started.
    player._advance(1)
    assert player._step == 0


def test_the_player_is_offered_only_where_there_is_something_to_play(
    qtbot, tmp_path
) -> None:
    """Sharper than the content kind, and sharper than the capability table can
    be: every sprite object carries a table and the corpus fills a handful of its
    16 slots, so an object whose groups are all terminator would open a window
    with an empty picker in it."""
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_obj_with_sequence(tmp_path, [], name="silent.OBJ")))
    assert not window._animation_action.isEnabled()

    window._load_pixel(str(_obj_with_sequence(tmp_path, [(4, 0)], name="live.OBJ")))
    assert window._animation_action.isEnabled()
    # And a document that is not a sprite object at all has none either.
    window._load_pixel(str(_make_snes_file(tmp_path)))
    assert not window._animation_action.isEnabled()


def test_the_players_zoom_is_its_own(qtbot, tmp_path) -> None:
    """Deliberately not the main view's: the frame is this module's widget rather
    than a Canvas precisely so the two levels can differ. Changing either must
    leave the other alone."""
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_obj_with_sequence(tmp_path, [(2, 0)])))
    window._show_animation()
    player = window._animation

    before = window._zoom.value()
    player._zoom.setValue(player._zoom.value() + 3)
    assert window._zoom.value() == before
    assert player._frame._zoom == player._zoom.value()

    # And Ctrl+wheel out on the backing steps that same spin: a frame is one
    # object in a window sized to be looked at, so most of what is on screen is
    # the surround the gesture would otherwise be dead over.
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    level = player._zoom.value()
    beyond = QPointF(player._frame.width() + 20, player._frame.height() + 20)
    QApplication.sendEvent(
        player._scroll.viewport(),
        QWheelEvent(
            beyond,
            beyond,
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        ),
    )
    assert player._zoom.value() == level - 1
    assert window._zoom.value() == before


def test_space_pans_the_player_from_wherever_its_focus_sits(qtbot, tmp_path) -> None:
    """A key press goes to the focused widget and a hold sends exactly one, so
    handling space as a key event of the frame left the gesture dead everywhere
    else in the window - and the Zoom spin, which is where the user is when the
    frame has just become too big for it, is exactly where they reach for it.

    The picker is the one place it still yields: a QComboBox drops its list open
    on space, and taking that away would leave the list unopenable by keyboard.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_obj_with_sequence(tmp_path, [(2, 0), (2, 3)])))
    window._show_animation()
    player = window._animation
    # Offscreen, setFocus only lands once the window is active.
    QApplication.setActiveWindow(player)

    player._zoom.setFocus()  # where using the spin leaves it
    qtbot.keyPress(player._zoom, Qt.Key.Key_Space)
    assert player._frame._pan_active
    assert not window._canvas._pan_active  # and the window behind is not armed
    qtbot.keyRelease(player._zoom, Qt.Key.Key_Space)
    assert not player._frame._pan_active

    player._sequence.setFocus()
    qtbot.keyPress(player._sequence, Qt.Key.Key_Space)
    assert not player._frame._pan_active
    qtbot.keyRelease(player._sequence, Qt.Key.Key_Space)
    player._sequence.hidePopup()  # the list the key just dropped open

    # A hold that outlives the window's activation: the release goes to whatever
    # was raised over it, so this is the only chance to let go.
    player._frame.setFocus()
    qtbot.keyPress(player._frame, Qt.Key.Key_Space)
    assert player._frame._pan_active
    QApplication.setActiveWindow(window)
    assert not player._frame._pan_active


def test_the_eyedropper_on_a_row_bearing_cell_arms_the_pen_with_what_it_picked(
    qtbot, tmp_path
) -> None:
    """The sampled value already carries the cell's palette row - that is what
    makes it equal the composed picture - so the eyedropper must not add the row a
    second time. Counted twice it selected the swatch two rows on, dragged Subpal
    to double the cell's row, and, once the palette is only as long as the file
    needs, ran off the end: select_index matched nothing and the pen fell back to
    index 0, so the next stroke painted with colour 0.
    """
    from celpix.core.palette import Palette
    from celpix.core.tilemap import Cell

    cells = [Cell(index=1, palette_row=1)]
    window, _bank, entry = _bound_screen(qtbot, tmp_path, cells)
    window._activate_entry(entry)
    # A palette no longer than the file needs, which is what turns the double-add
    # from a wrong swatch into a silent fallback to 0.
    entry.doc.palette = Palette([0xFF000000 + i for i in range(32)])
    _painting_on(window)

    picked = window._sampled_value(0, 0)
    assert picked is not None and picked >= 16  # row 1 really is folded in
    window._eyedrop_at(0, 0)
    assert window._palette_panel.selected_index() == picked
    assert window._pen_value() == picked % 16


def test_the_eyedropper_points_both_panels_at_the_pixel_it_sampled(
    qtbot, tmp_path
) -> None:
    """A pixel of a tilemap belongs to a tile as much as to a colour, and the two
    panels answer one half each: the sheet picks the tile the pixel was drawn
    from - the cell's own ID, which is what that panel is addressed in - and the
    palette grid picks the colour, which on a row-bearing cell carries the row it
    is drawn through. The record travels with the ID, so a stamp made after the
    pick lays the whole cell down, exactly as the stamp tool's own eyedropper.
    """
    from PySide6.QtCore import Qt

    from celpix.core.tilemap import Cell

    cells = [Cell(index=1), Cell(index=2, palette_row=2)]
    window, _bank, entry = _bound_screen(qtbot, tmp_path, cells)
    window._activate_entry(entry)
    window.show()
    window._tile_source_dock.setVisible(True)
    window._tile_source_dock.raise_()
    window._refresh_tile_source()
    _painting_on(window)

    # A right-click one tile across, which is the second cell's first pixel.
    window._on_pixel_pressed(8, 0, Qt.MouseButton.RightButton)

    assert window._source_tile_id == 2
    assert window._source_cell == window._doc.cells[1]
    assert window._tile_source_panel.selected_id() == 2
    assert window._subpalette.value() == 2
    assert window._palette_panel.selected_index() // 16 == 2

    # A pixel document has no cells for a pick to name, and the sheet keeps
    # whatever it was showing.
    window._activate_entry(window._workspace.entries[0])
    _painting_on(window)
    window._on_pixel_pressed(0, 0, Qt.MouseButton.RightButton)
    assert window._source_tile_id == 2

    # A sprite object answers with the piece the pixel hit - it has no slots to
    # divide, and no cell record for a stamp to lay down.
    window, _bank, _obj = _bound_object(qtbot, tmp_path, [(0, 0, 3, 2)])
    _painting_on(window)
    window._on_pixel_pressed(1, 1, Qt.MouseButton.RightButton)
    assert window._source_tile_id == 3
    assert window._source_cell is None
    # Through the entry's own row base, which an object file has (its rows are
    # named in the sprite half of the hardware's palette).
    assert window._subpalette.value() == window._drawn_palette_row(2)


def test_unbinding_a_map_being_painted_on_leaves_pixel_mode(qtbot, tmp_path) -> None:
    """A binding change is where pixel editing can stop being available without
    the view having moved, and the mode is app-wide state nothing else resets.
    Left armed, the canvas went on reporting paint gestures while both mode
    toggles were greyed - so the mode read as "off", was not, and could only be
    escaped by switching entries."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource
    from celpix.ui.tools import EditMode

    window, _bank, entry = _bound_screen(qtbot, tmp_path, [Cell(index=1)])
    window._activate_entry(entry)
    _painting_on(window)
    assert window._edit_mode is EditMode.PIXEL

    window._rebind_tiles(entry, TileSource(mode=TileMode.NONE))
    assert not window._pixel_edit_available()
    assert window._edit_mode is EditMode.TILE


def test_moving_pixels_onto_a_higher_palette_row_keeps_them(qtbot, tmp_path) -> None:
    """Pixels reaching the bank were painted through the destination cell for a
    stroke, but *moved in from another* for a float, a paste or a marquee flip.
    Subtracting the destination cell's row then went negative and clamped to zero,
    so relocating art onto any cell past palette row 0 wrote nothing but index 0 -
    a paste onto such a cell blanked the tile outright.

    A pixel edit settles into the row the destination cell already names rather
    than moving one: the incoming colours are matched inside that row, so the art
    arrives looking like what was dragged and the cell's own field is untouched.
    """
    from celpix.core.index_grid import IndexGrid
    from celpix.core.quantize import color_distance
    from celpix.core.tilemap import Cell

    window, bank, entry = _bound_screen(
        qtbot, tmp_path, [Cell(index=1), Cell(index=2, palette_row=2)]
    )
    window._activate_entry(entry)
    _painting_on(window)
    palette = window._doc.palette
    space = window._index_space()

    # The source cell's tile, as composed on screen: row 0, so absolute == stored.
    source = [window._sampled_value(x, 0) for x in range(8)]
    assert any(source), "the source tile has to hold something to move"

    # Land that row of pixels on cell 1, which draws through palette row 2 - so
    # what arrives carries row 0's absolute indices, not row 2's.
    was = window._doc.cells[1].palette_row
    moved = IndexGrid(8, 8, bytearray(bytes(source) + bytes(56)))
    tiles = [IndexGrid(8, 8), moved]
    edits = window._bank_tiles_from(window._doc, [1], tiles)
    landed = [edits[2].get(x, 0) for x in range(8)]

    assert any(landed), "the tile came back blank"
    assert window._doc.cells[1].palette_row == was  # the edit moved no cell's row
    # Stored row-relative, and each index is the entry on row 2 nearest the colour
    # that was dragged - so nothing is stored that row 2 cannot draw.
    assert all(0 <= v < space for v in landed)
    row = [palette.color(2 * space + i) for i in range(space)]
    for pixel, value in zip(source, landed, strict=True):
        want = palette.color(pixel)
        chosen = color_distance(want, row[value])
        assert chosen == min(color_distance(want, c) for c in row)


def test_clearing_blanks_to_the_row_s_own_first_index(qtbot, tmp_path) -> None:
    """ "Empty" is index 0 **of the row the pixel is drawn through**, not of the
    palette. A cell drawn through row 2 cannot show the palette's first entry at
    all, so blanking to it left the commit matching that colour into row 2 by
    nearest distance — a Clear that painted the closest thing to entry 0 rather
    than clearing, and a Cut and a moved float that left the same behind them.

    A sprite object is the same rule read from the other end: there index 0 is the
    hole a piece leaves for whatever is behind it and the blit composes it
    unbiased, so 0 is both what a cleared pixel stores and what it shows — and the
    row must not be matched onto it either, or moving a piece's transparent pixels
    would fill them in.
    """
    from PySide6.QtCore import QRect

    from celpix.core.tilemap import Cell

    window, bank, entry = _bound_screen(qtbot, tmp_path, [Cell(index=1, palette_row=2)])
    window._activate_entry(entry)
    _painting_on(window)
    space = window._index_space()
    window._marquee = QRect(0, 0, 8, 8)
    window._pixel_clear()

    tile = _bank_tile(window, bank, 1)
    assert [tile.get(x, 0) for x in range(8)] == [0] * 8  # stored row-relative
    grid = window._window_grid()
    assert [grid.get(x, 0) for x in range(8)] == [2 * space] * 8  # shown on row 2

    # The object's piece carries a row too, and clears to transparency through it.
    window, bank, _obj = _bound_object(qtbot, tmp_path, [(0, 0, 2, 2)])
    _painting_on(window)
    assert window._cell_paint_base(window._doc.sprite_frames[0][0]) > 0
    window._marquee = QRect(0, 0, 8, 8)
    window._pixel_clear()

    tile = _bank_tile(window, bank, 2)
    assert [tile.get(x, 0) for x in range(8)] == [0] * 8
    grid = window._window_grid()
    assert [grid.get(x, 0) for x in range(8)] == [0] * 8


# -- pixel editing on a tilemap: the clipboard and the float ----------------
def _painting_tilemap(qtbot, tmp_path, cells=None):
    """A bound tilemap in pixel mode, over cells spread across palette rows."""
    from celpix.core.tilemap import Cell

    if cells is None:
        cells = [Cell(index=i + 1, palette_row=i % 4) for i in range(8)]
    window, entry = _bound_tilemap(qtbot, tmp_path, cells, maker=_pnl_file)
    window._set_edit_mode(EditMode.PIXEL)
    return window, entry


def test_a_tilemap_in_pixel_mode_offers_the_pixel_paste(qtbot, tmp_path) -> None:
    """Paste means cells in tile mode and *pixels* in pixel mode, so the in-app
    cell buffer cannot be what decides whether the action is live: asking it in
    pixel mode greyed out the only paste on offer there."""
    window, _ = _painting_tilemap(qtbot, tmp_path)
    window._marquee = QRect(0, 0, 8, 8)
    window._pixel_copy()  # image-only, onto the system clipboard

    window._sync_edit_actions()
    assert window._paste_action.isEnabled()

    # ...and tile mode still asks the cell buffer, which nothing has filled.
    window._set_edit_mode(EditMode.TILE)
    window._sync_edit_actions()
    assert not window._paste_action.isEnabled()


def test_pasting_pixels_into_a_map_lands_the_colours_that_were_copied(
    qtbot, tmp_path
) -> None:
    """A map's picture is composed in *absolute* indices — every cell's own
    palette row folded in — so fitting a paste into one subpalette row quantized
    every colour off that row onto the nearest one on it, and a copy of the map's
    own pixels came back a different picture."""
    window, _ = _painting_tilemap(qtbot, tmp_path)
    # A colour of its own for every index the four rows can reach, so a paste that
    # came back on the wrong row is a different picture rather than a coincidence.
    window._doc.palette.colors[:] = [0xFF000000 | i * 0x040404 for i in range(64)]
    base = window._window_grid()
    # Four cells across, one per palette row — so the copy spans rows the view's
    # own subpalette window does not reach.
    window._marquee = QRect(0, 0, 32, 8)
    assert len({base.get(x, 0) // 16 for x in range(32)}) > 1
    window._pixel_copy()

    region = window._take_pixel_clipboard()
    assert region is not None
    copied = [base.get(x, y) for y in range(8) for x in range(32)]
    assert [region.get(x, y) for y in range(8) for x in range(32)] == copied


def test_a_float_lands_on_the_colours_it_shows_not_the_offsets_it_holds(
    qtbot, tmp_path
) -> None:
    """A pasted pixel is composed against the *whole* palette, so the index it
    carries names a colour on whatever row that colour was found on. Landing it
    on a cell of another row by remainder kept the offset and threw the colour
    away — grey composed at row 1 offset 5 was stored as offset 5, which on the
    destination row is whatever that row holds there. The row has to come off by
    colour, so the pixels stay the colour they were shown in.
    """
    from PySide6.QtGui import QGuiApplication, QImage

    from celpix.core.tilemap import Cell

    window, _ = _painting_tilemap(
        qtbot, tmp_path, [Cell(index=i + 1, palette_row=3) for i in range(8)]
    )
    grey, red = 0xFF808080, 0xFFFF0000
    window._doc.palette.colors[:] = [0xFF000000 | i * 0x030201 for i in range(64)]
    window._doc.palette.colors[16 + 5] = grey  # where the whole-palette match hits
    window._doc.palette.colors[48 + 2] = grey  # where row 3 keeps the same colour
    window._doc.palette.colors[48 + 5] = red  # ...and what the old remainder took
    window._refresh_view()

    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    image.fill(grey)
    QGuiApplication.clipboard().setImage(image)
    window._pixel_paste()
    assert window._float_grid is not None
    window._float_pos = (0, 0)  # over a cell drawing through row 3
    window._commit_float()

    landed = window._window_grid()
    shown = {
        window._doc.palette.color(landed.get(x, y)) for y in range(4) for x in range(4)
    }
    assert shown == {grey}
    assert landed.get(0, 0) == 48 + 2  # row 3's own grey, not its offset 5


def test_a_lifted_float_keeps_the_colours_the_pixels_were_shown_in(
    qtbot, tmp_path
) -> None:
    """A float is the picture's own pixels lifted off it, so the overlay has to
    resolve them through the table the base was drawn with. Rendering them through
    the view's Subpal instead shifted the selection by however many rows the spin
    sat on — which on a map whose cells carry rows is a row to *assign*, not one
    to draw through — and left index 0 opaque where the base has it clear.
    """
    window, _ = _painting_tilemap(qtbot, tmp_path)
    window._transparent_zero_box.setChecked(True)
    window._subpalette.setValue(2)  # a row to assign; the picture must not move
    window._refresh_view()
    shown = window._canvas._image

    window._marquee = QRect(0, 0, 8, 8)
    window._lift_float(cut=False)
    float_image = window._canvas._float_image
    assert float_image is not None
    assert [float_image.pixel(x, y) for y in range(8) for x in range(8)] == [
        shown.pixel(x, y) for y in range(8) for x in range(8)
    ]
