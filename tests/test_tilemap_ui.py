"""Opening a tilemap: what it binds to, the tilemap bar that
drives the binding, and the controls its content kind gates."""

from __future__ import annotations

import itertools

from celpix.core.palette import Palette
from celpix.ui.main_window import MainWindow
from celpix.ui.widgets import select_combo_data
from uihelpers import (
    _bound_screen,
    _bound_tilemap,
    _bound_to_slice,
    _cgx_file,
    _make_snes_file,
    _obj_file,
    _pnl_file,
    _scr_file,
    _section_names,
)


def test_a_tilemap_forces_rectangle_selection_and_hands_it_back(
    qtbot, tmp_path
) -> None:
    """A tilemap is edited as the picture it draws, so it imposes Rectangle the
    way pixel mode and the rearrange tool do - and the user's own shape comes
    back on a pixel document. A sprite object goes with it: its sheet is composed
    from frames, so a run over it isn't the byte range Linear exists to name.
    """
    from celpix.core.tilemap import Cell
    from celpix.ui.main_window.selection import SelectionShape

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    preference = window._selection_shape.currentData()
    assert window._selection_shape.isEnabled()

    for tilemap in (
        _scr_file(tmp_path, [Cell(index=1)]),
        _obj_file(tmp_path, [(0, 0, 1)]),
    ):
        window._load_pixel(str(tilemap))
        assert window._selection_shape.currentData() is SelectionShape.RECT
        assert not window._selection_shape.isEnabled()
        # And the S key with it: it presses this picker, so a locked picker is
        # the whole of its answer. Swapping here would have persisted Linear as
        # the preference and handed it back on the next pixel document.
        assert not window._can_toggle_selection_mode()
        window._toggle_selection_mode()
        assert window._selection_shape.currentData() is SelectionShape.RECT

        window._activate_entry(window._workspace.entries[0])  # back to the pixels
        assert window._selection_shape.currentData() is preference
        assert window._selection_shape.isEnabled()


def test_exporting_a_tilemap_writes_the_map_and_not_the_tiles_it_borrows(
    qtbot, tmp_path
) -> None:
    """A tilemap entry's ``pixel_data`` is the *bound* entry's bytes — it sits
    there so every tile path keeps working — so an export that rendered it would
    quietly write the tile bank out under the screen's name."""
    from PySide6.QtGui import QImage

    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource
    from celpix.ui import export

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))  # 8 SNES 4bpp tiles
    scr = _scr_file(tmp_path, [Cell(index=1), Cell(index=2, palette_row=3)])
    window._load_pixel(str(scr))
    entry = window._workspace.current
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    entry.doc = None
    window._activate_entry(window._workspace.entries[0])
    window._activate_entry(entry)

    window._refresh_view()
    image = export.document_image(window._doc, window._registry)
    assert image.format() == QImage.Format.Format_Indexed8
    # The whole map as it is assembled — a screen file's four 32x32 screens laid
    # 2x2, so 64 cells across — and not the 8 tiles of the bank behind it.
    assert (image.width(), image.height()) == (64 * 8, 64 * 8)
    # The table spans every palette row the cells name, because the row is
    # folded into the indices and one image carries one table.
    assert len(image.colorTable()) == 4 * 16

    # Raw export is the same question about the same entry: its own bytes are
    # its cells, and the bank's belong to the bank.
    assert export.raw_bytes(window._doc) == window._doc.tilemap_data


def test_a_sprite_object_opens_and_draws_its_frames_one_after_another(
    qtbot, tmp_path
) -> None:
    """An OBJ is a tilemap entry like a screen — same binding, same palette, same
    save path — but its cells are parts at pixel offsets, so the view lays the
    frames out in a strip instead of composing a grid
    (``docs/design/tilemap-entry.md`` §6)."""
    from celpix.core.capabilities import ContentKind
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    obj = _obj_file(tmp_path, [(0, 0, 1), (24, 5, 2)])
    window._load_pixel(str(obj))

    entry = window._workspace.current
    assert entry.content_kind is ContentKind.TILEMAP
    assert window._doc.is_sprite
    # Cols means frames per row on an object, which is the only reading of "how
    # many across" a sheet of separate pictures has.
    assert window._columns.value() == 8

    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    entry.doc = None  # re-read through the new binding
    window._activate_entry(window._workspace.entries[0])
    window._activate_entry(entry)
    assert window._doc.pixel_data  # the bound entry's tiles, not the object's
    # The strip is as wide as its bounding box needs and no wider: one 8x8 part
    # at the origin and one at x=24 make a 32-pixel box, eight frames across.
    assert window._canvas._image.width() == 8 * 32

    # View-only, and it says so rather than silently doing nothing. Copy is the
    # exception and takes the pixels instead - see the sheet-copy test below.
    window._selected_tile = 0
    window._clear_cells()
    assert "view-only" in window.statusBar().currentMessage()
    window._cut_cells()
    assert "view-only" in window.statusBar().currentMessage()


def test_a_sprite_sheet_selects_its_drawn_tiles_and_backs_its_empty_frames(
    qtbot, tmp_path
) -> None:
    """A sprite object has no cells on the canvas, so both selection shapes fall
    back to the 8x8 tiles of the sheet — read off the same grid the frames were
    placed on, or a drag names a different part of the picture than it covers. The
    backing follows from the same grid: the slots past the last frame are the empty
    frames beside it, which is not where a count of *parts* puts them."""
    from PySide6.QtCore import QPointF, QRect

    from celpix.project.workspace import TileMode, TileSource
    from celpix.ui.main_window.selection import SelectionShape

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    obj = _obj_file(tmp_path, [(0, 0, 1), (24, 5, 2)])
    window._load_pixel(str(obj))
    entry = window._workspace.current
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    entry.doc = None  # re-read through the new binding
    window._activate_entry(window._workspace.entries[0])
    window._activate_entry(entry)

    # One drawn frame. Its two 8x8 parts sit at x=0 and x=24, y=0 and y=5, so the
    # box they share is 32x16 pixels — 4x2 tiles — and Cols=8 lays the sheet out
    # eight frames, 32 tiles, across.
    sheet = window._sprite_sheet()
    assert (sheet.frames, sheet.frame, sheet.columns, sheet.rows) == (1, (4, 2), 32, 2)
    # The frame is the canvas's block, which is what makes the drawn frame's tiles
    # the *leading* 8 slots: the backing is then the seven empty frames beside it,
    # and not the tail of a row cutting through the one frame there is.
    canvas = window._canvas
    zoom = canvas._zoom
    assert (canvas._block_cols, canvas._block_rows) == (4, 2)
    assert canvas._filled_tiles == sheet.slots == 8
    # The symptom, stated directly: nothing inside the drawn frame is painted as
    # backing. A part count read as a slot count put a band of it through the art.
    assert not canvas._background_region().intersects(QRect(0, 0, 32 * zoom, 16 * zoom))

    # A drag over the sheet's top-left 2x2 tiles selects those four and no others.
    select_combo_data(window._selection_shape, SelectionShape.RECT)
    window._on_slots_selected(
        canvas._slot_at(QPointF(1.0 * zoom, 1.0 * zoom)),
        canvas._slot_at(QPointF(9.0 * zoom, 9.0 * zoom)),
    )
    layout = window._view_layout()
    assert {layout.slot_to_pos(slot) for slot in canvas._selected_slots} == {
        (0, 0),
        (1, 0),
        (0, 1),
        (1, 1),
    }

    # And a linear drag over the same two tiles is the run between them, in the
    # order the frame's tiles are laid out.
    select_combo_data(window._selection_shape, SelectionShape.LINEAR)
    window._on_slots_selected(
        canvas._slot_at(QPointF(1.0 * zoom, 1.0 * zoom)),
        canvas._slot_at(QPointF(9.0 * zoom, 1.0 * zoom)),
    )
    assert sorted(canvas._selected_slots) == [0, 1]


def test_transparent_zero_clears_a_blank_cell_on_every_palette_row(
    qtbot, tmp_path
) -> None:
    """How these formats say "empty": a blank cell names a real tile whose pixels
    are all index 0, which the console draws as the backdrop.

    The cell on palette row 2 is the one that matters — a hardware map folds the
    row into the indices, so its blank pixels are index 32, not 0, and clearing
    entry 0 alone would leave them opaque.
    """
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    bank = tmp_path / "bank.4bpp.sfc"
    bank.write_bytes(b"\xff" * 32 + b"\x00" * 32)  # tile 0 solid, tile 1 blank
    screen = _scr_file(
        tmp_path, [Cell(index=0), Cell(index=1), Cell(index=1, palette_row=2)]
    )

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(bank))
    window._load_pixel(str(screen))
    entry = window._workspace.current
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(entry)
    assert not window._transparent_zero_box.isHidden()
    steps = window._undo_stack.count()

    # Off: the blank tile is drawn as whatever colour sits at its row's index 0.
    assert window._canvas._image.pixel(8, 0) >> 24 == 0xFF
    assert window._canvas._image.pixel(16, 0) >> 24 == 0xFF

    window._transparent_zero_box.setChecked(True)
    assert window._doc.view.transparent_zero
    image = window._canvas._image
    assert image.pixel(0, 0) >> 24 == 0xFF  # the solid tile is untouched
    assert image.pixel(8, 0) >> 24 == 0  # blank on row 0
    assert image.pixel(16, 0) >> 24 == 0  # blank on row 2 — the stride case

    # A view toggle, like All Frames beside it: no index moved, so no undo step.
    assert window._undo_stack.count() == steps

    # And it belongs to the entry, not the window: the bank has no answer of its
    # own, so switching to it must not carry this one along.
    window._activate_entry(window._workspace.entries[0])
    assert not window._transparent_zero
    window._activate_entry(entry)
    assert window._transparent_zero and window._transparent_zero_box.isChecked()


def test_navigation_keys_do_nothing_on_a_tilemap(qtbot, tmp_path) -> None:
    """A map is always drawn entire, so it has no view window to move — and the
    nav keys are filtered app-wide rather than bound to the actions the
    NAVIGATION capability hides, so ``_set_offset`` is where they have to stop.

    The bank is what makes this bite: a map's ``pixel_data`` is the bound entry's
    art, so the offset clamp measures a window against *the bank's* tile count. A
    bank bigger than the window left the offset free to move a position nothing
    renders from, and each step pushed an undo command that dirtied the project.
    """
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    bank = tmp_path / "bank.4bpp.sfc"
    bank.write_bytes(bytes((i * 13 + 1) & 0xFF for i in range(32 * 1024)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(bank))
    window._load_pixel(str(_pnl_file(tmp_path, [Cell(index=1), Cell(index=2)])))
    entry = window._workspace.current
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(entry)
    steps = window._undo_stack.count()

    for move in (
        lambda: window._nav_rows(window._row_step()),
        lambda: window._nav_tiles(1),
        window._nav_end,
        lambda: window._nav_bytes(1),
    ):
        move()
        assert (window._offset, window._nudge) == (0, 0)
    assert window._undo_stack.count() == steps

    # The bank itself still pages: the veto is the document's, not the window's.
    window._activate_entry(window._workspace.entries[0])
    window._nav_rows(1)
    assert window._offset == window._columns.value()


def test_a_tilemap_row_is_marked_by_the_layout_it_holds(qtbot, tmp_path) -> None:
    """The picture glyph says "a little graphic", which a map is not. Which of the
    three map layouts it is comes off the *format*, so a row can draw it before
    the entry has been loaded or bound — and it goes on the row whatever the row
    is a window onto, a whole file as much as a slice of a ROM. Keyed on the
    bounding, every map opened as its own file went unmarked.
    """
    from celpix.core.capabilities import ContentKind

    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    panel = window._files_panel

    def marker(entry) -> str:
        icon, what = panel._entry_marker(entry)
        assert not icon.isNull()
        return what

    whole = window._workspace.current  # a pixel file wears no glyph at all
    assert panel._entry_marker(whole)[0].isNull()

    cut = window._workspace.add_slice(str(px), "gfx", 32, 32)
    assert marker(cut) == ""  # the picture glyph, unnamed

    for entry in (cut, whole):
        entry.content_kind = ContentKind.TILEMAP
        assert marker(entry) == "Tilemap"  # no format named yet
        for preset, what in (
            ("preset.tilemap.snes-bg", "Tilemap"),
            ("preset.tilemap.scgcad-object", "Sprite map"),
            ("preset.tilemap.text-8bit", "Fontmap"),
        ):
            entry.tilemap_preset_id = preset
            assert marker(entry) == what

    # A bookmark keeps the ribbon even on a map: it marks a position, not
    # content, and that is what tells it from the slices it sits among.
    from celpix.project.workspace import Entry, EntryKind

    mark = Entry(
        name="here",
        kind=EntryKind.BOOKMARK,
        path=str(px),
        slice_offset=64,
        content_kind=ContentKind.TILEMAP,
    )
    assert marker(mark) == ""


def test_switching_cell_format_redraws_the_rows_layout_marker(qtbot, tmp_path) -> None:
    """The marker comes off the cell format, and a map carved out by hand names
    none — so picking a text run *is* the moment a row becomes a fontmap. The
    binding path is the only thing that moves it, and it is not on the render
    cycle the rest of the bar rides on, so it has to say so itself; the row
    otherwise kept the grid glyph until the project was next opened."""
    from celpix.core.tilemap import Cell

    window, _bank, entry = _bound_screen(
        qtbot, tmp_path, [Cell(index=1), Cell(index=2)]
    )
    panel = window._files_panel
    item = panel._items[entry]
    assert "Fontmap" not in item.toolTip(0)

    combo = window._tilemap_preset
    combo.setCurrentIndex(combo.findData("preset.tilemap.text-8bit"))
    window._on_tilemap_preset_change(combo.currentIndex())

    assert "Fontmap" in item.toolTip(0)
    assert item.icon(0).cacheKey() == panel._entry_marker(entry)[0].cacheKey()


def test_all_frames_is_a_sprite_maps_own_switch_and_grows_the_sheet(
    qtbot, tmp_path
) -> None:
    """A file has room for 32 frame slots and `_obj_file` fills one, so the strip
    stops there. The box shows the rest — and it is the *format*'s control, not the
    content kind's: a grid tilemap has no frames to count, so it is hidden there
    rather than greyed."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_obj_file(tmp_path, [(0, 0, 1)])))
    obj = window._workspace.current
    obj.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(obj)
    assert not window._all_frames.isHidden()
    assert window._sprite_sheet().frames == 1
    steps = window._undo_stack.count()

    window._all_frames.setChecked(True)
    assert window._doc.view.show_all_frames
    # Every slot the file has room for, at its own frame number.
    assert window._sprite_sheet().frames == 32
    assert window._canvas._filled_tiles == window._sprite_sheet().slots

    # Not undoable, unlike its neighbours on the bar: it says how much of the
    # file to look at, not what the file holds.
    assert window._undo_stack.count() == steps

    # A grid tilemap has no frames, so the box is not a feature switched off there.
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    assert window._all_frames.isHidden()

    # The choice belongs to the entry, like the rearrangement and the row base.
    window._activate_entry(obj)
    assert window._all_frames.isChecked() and window._sprite_sheet().frames == 32


def test_the_subsprite_sheet_opens_where_the_player_will_not_and_follows_the_pick(
    qtbot, tmp_path
) -> None:
    """The two second readings a sprite map has, and what tells them apart.

    The player wants a sequence with a step in it and most files have none; every
    sprite map is made of records, so this opens on all of them. What it shows is
    one square per record in frame order — the parts, where the canvas shows the
    object they assemble to — and the canvas is what points at one: the sheet
    rings the picked record and picks nothing itself.
    """
    from PySide6.QtCore import QPoint, Qt

    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    # The third part draws the same tile as the first, somewhere else: one record
    # of two, and one piece of one, which is the whole of what Frames decides.
    window._load_pixel(str(_obj_file(tmp_path, [(0, 0, 1), (24, 5, 2), (8, 0, 1)])))
    entry = window._workspace.current
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    entry.doc = None  # re-read through the new binding
    window._activate_entry(window._workspace.entries[0])
    window._activate_entry(entry)

    assert not window._animation_action.isEnabled()  # no sequence holds a step
    assert window._subsprites_action.isEnabled()

    window._show_subsprites()
    sheet, panel = window._subsprites, window._subsprites._panel
    assert sheet.isVisible()
    # One square per record — not one per frame, and not one per distinct piece.
    assert panel._records == [(0, 0), (0, 1), (0, 2)]

    # The second part, at x=24 y=5: the pick the canvas resolves from the pixel
    # is the one the sheet rings.
    canvas = window._canvas
    zoom = canvas._zoom
    qtbot.mouseClick(
        canvas, Qt.MouseButton.LeftButton, pos=QPoint(int(26 * zoom), int(7 * zoom))
    )
    assert window._picked_subsprite == (0, 1)
    assert panel._marked == (0, 1)

    # Frames off is the inventory: the repeat collapses onto the record it first
    # appeared in, and the captions go with it - a square is several records now
    # and has no one frame to name, so the box that writes them greys out.
    sheet._frames.setChecked(False)
    assert panel._records == [(0, 0), (0, 1)]
    assert not sheet._numbers.isEnabled() and not panel._captions

    # A pick on the *other* occurrence still rings the square its art is in,
    # which is the only reading of the ring that survives the collapse.
    qtbot.mouseClick(
        canvas, Qt.MouseButton.LeftButton, pos=QPoint(int(10 * zoom), int(2 * zoom))
    )
    assert window._picked_subsprite == (0, 2)
    assert panel._marked == (0, 0)

    # ...and back on it is its own square again, captions and all.
    sheet._frames.setChecked(True)
    assert panel._marked == (0, 2)
    assert sheet._numbers.isEnabled() and panel._captions

    # And an entry with no subsprites closes it: the window holds its own copy of
    # the sheet, so one left open would show pieces of a file nowhere on screen.
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    assert not window._subsprites.isVisible()
    assert not window._subsprites_action.isEnabled()


def test_the_subsprite_sheet_takes_the_cols_keys_and_zooms_over_its_backing(
    qtbot, monkeypatch
) -> None:
    """Two gestures aimed at the window rather than at the picture in it.

    Shift+arrow is the main window's Cols key; while this window is the one
    being typed into it lays *this* sheet out, the two never firing together.
    And Ctrl+wheel answers over the empty backing as well as over the squares -
    a short object laid 8 across leaves most of the window grey, which is
    exactly where the pointer is when the pieces want to be bigger.

    Driven on the window alone: it is handed a composed sheet and reads no
    model, so nothing here needs a document behind it.
    """
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QImage, QWheelEvent
    from PySide6.QtWidgets import QApplication

    from celpix.ui.subsprite_window import SubspriteWindow

    sheet = SubspriteWindow()
    qtbot.addWidget(sheet)
    sheet.show_sheet(
        QImage(16, 8, QImage.Format.Format_ARGB32),
        [(0, 0), (0, 1)],
        [(0, 0, 8, 8), (8, 0, 8, 8)],
        (8, 8),
        "Subsprites",
    )
    columns = sheet._columns.value()

    qtbot.keyClick(sheet._panel, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    assert sheet._columns.value() == columns + 1
    qtbot.keyClick(sheet._panel, Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier)
    assert sheet._columns.value() == columns

    # ...but not while a spin box has it: there Shift+arrow is selecting the
    # digits of the number being typed, which is why the sheet takes the focus
    # when the window opens.
    monkeypatch.setattr(
        QApplication, "focusWidget", staticmethod(lambda: sheet._columns)
    )
    qtbot.keyClick(sheet._panel, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    assert sheet._columns.value() == columns
    monkeypatch.undo()

    zoom = sheet._zoom.value()
    panel = sheet._panel
    beyond = QPointF(panel.width() + 20, panel.height() + 20)
    QApplication.sendEvent(
        sheet._scroll.viewport(),
        QWheelEvent(
            beyond,
            beyond,
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        ),
    )
    assert sheet._zoom.value() == zoom + 1 and panel._zoom == zoom + 1


def test_the_subsprite_ring_goes_round_the_piece_and_not_its_square(qtbot) -> None:
    """What the ring on the sheet is round, on an object that mixes sizes.

    A square is the largest subsprite of the object and a smaller piece is
    centred in one, so the square and the record are two different rectangles -
    and a ring on the square would claim the gutter around a small piece is part
    of it. The composed sheet says where each record's art landed and this scales
    that by the zoom, which is also what keeps the ring on the pixel lattice the
    art is drawn on rather than half a pixel off it.
    """
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QImage

    from celpix.ui.subsprite_panel import SubspritePanel

    panel = SubspritePanel()
    qtbot.addWidget(panel)
    panel.set_zoom(3)
    # Two 16x16 squares side by side: a full-size piece, then an 8x8 one centred.
    panel.set_sheet(
        QImage(32, 16, QImage.Format.Format_ARGB32),
        [(0, 0), (0, 1)],
        [(0, 0, 16, 16), (20, 4, 8, 8)],
        (16, 16),
        2,
    )
    assert panel._piece_rect(0) == QRect(0, 0, 48, 48) == panel._cell_rect(0)
    assert panel._piece_rect(1) == QRect(60, 12, 24, 24)
    assert panel._cell_rect(1) == QRect(48, 0, 48, 48)  # the square it sits in


def test_sheet_captions_repaint_in_strips_exactly_as_in_one_pass(qtbot) -> None:
    """Both sheets caption the exposed rows only, and must lose nothing by it.

    A bank is thousands of squares and a scrolled view shows a dozen rows, so
    the caption loops walk the band rather than the run (``_exposed_slots``).
    The canvas's twin of this test says why it is worth pinning down; here it is
    the two panels that draw a caption per square, from opposite edges of it —
    the tile source panel's along the bottom, the subsprite panel's the same,
    with the picked-tile rings on top.
    """
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtGui import QImage, QRegion

    from celpix.ui.subsprite_panel import SubspritePanel
    from celpix.ui.tile_source_panel import TileSourcePanel

    tiles = TileSourcePanel()
    qtbot.addWidget(tiles)
    tiles.set_zoom(4)  # a caption is dropped below LABEL_MIN_PX a square
    tiles.set_sheet(
        QImage(4 * 8, 6 * 8, QImage.Format.Format_ARGB32), list(range(24)), (8, 8), 4
    )
    tiles.set_labels({tile: chr(ord("A") + tile % 26) for tile in range(24)})
    tiles.select_ids([5, 6, 7, 13])

    pieces = SubspritePanel()
    qtbot.addWidget(pieces)
    pieces.set_zoom(4)
    pieces.set_sheet(
        QImage(3 * 16, 5 * 16, QImage.Format.Format_ARGB32),
        [(frame, index) for frame in range(5) for index in range(3)],
        [],
        (16, 16),
        3,
    )
    pieces.set_captions(True)

    for panel in (tiles, pieces):
        whole = QImage(panel.size(), QImage.Format.Format_ARGB32)
        panel.render(whole, QPoint(), QRegion(panel.rect()))
        strips = QImage(panel.size(), QImage.Format.Format_ARGB32)
        step = 13  # narrow, and landing on no square's boundary
        for top in range(0, panel.height(), step):
            band = QRect(0, top, panel.width(), step)
            panel.render(strips, QPoint(0, top), QRegion(band))
        assert strips == whole


def test_copying_a_rectangle_of_a_sprite_sheet_lifts_the_pixels_it_draws(
    qtbot, tmp_path
) -> None:
    """A sprite object has no cells to lift, but the picture under the selection is
    well defined - so Copy takes the *composed sheet*, not the bank tiles behind
    it. The difference is the whole point: a part sits at a signed pixel offset, so
    an 8x8 of the sheet is generally pieces of two source tiles and neither whole."""
    from PySide6.QtCore import QPointF

    from celpix.pipeline import pipeline
    from celpix.project.workspace import TileMode, TileSource
    from celpix.ui import clipboard
    from celpix.ui.main_window.selection import SelectionShape

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    # The second part is at y=5, so it lands across two tile rows of the sheet.
    window._load_pixel(str(_obj_file(tmp_path, [(0, 0, 1), (24, 5, 2)])))
    entry = window._workspace.current
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    entry.doc = None  # re-read through the new binding
    window._activate_entry(window._workspace.entries[0])
    window._activate_entry(entry)

    # The 1x2 rectangle of sheet tiles the offset part is spread over: tile column
    # 3 (pixels 24-32) of both tile rows.
    canvas = window._canvas
    zoom = canvas._zoom
    select_combo_data(window._selection_shape, SelectionShape.RECT)
    window._on_slots_selected(
        canvas._slot_at(QPointF(25.0 * zoom, 1.0 * zoom)),
        canvas._slot_at(QPointF(25.0 * zoom, 9.0 * zoom)),
    )
    assert window._copy_action.isEnabled()
    assert window._copy_selection()

    payload = clipboard.take_payload()
    assert payload is not None
    assert (payload.count, payload.columns) == (2, 1)
    top, bottom = payload.tiles()
    # What was lifted is what is drawn there, pixel for pixel.
    grid = pipeline.tilemap_image(
        window._doc, window._registry, window._tilemap_columns()
    ).grid
    for y in range(8):
        for x in range(8):
            assert top.get(x, y) == grid.get(24 + x, y)
            assert bottom.get(x, y) == grid.get(24 + x, 8 + y)
    # ...which is a part that starts five rows down, and so is neither of the bank
    # tiles it draws from: the top tile's first five rows are untouched sheet.
    assert not any(top.get(x, y) for y in range(5) for x in range(8))
    assert any(top.get(x, y) for y in (5, 6, 7) for x in range(8))
    # The colours travel with the indices, which carry their part's palette row
    # folded in - so a paste elsewhere has a table long enough to re-match against.
    assert payload.max_index < len(payload.colors)


def test_a_screen_file_opens_as_a_tilemap_entry(qtbot, tmp_path) -> None:
    """The container was chosen from the file's own signature, so what it holds
    follows from that rather than from a question put to the user."""
    from celpix.core.capabilities import ContentKind
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))

    entry = window._workspace.entries[0]
    assert entry.container_id == "container.scgcad-scr"
    assert entry.content_kind is ContentKind.TILEMAP
    assert entry.tilemap_preset_id == "preset.tilemap.snes-bg"
    assert _section_names(window._files_panel) == ["Tilemaps"]


def test_an_unbound_tilemap_still_opens_and_draws(qtbot, tmp_path) -> None:
    """The binding is project state no file states, so a map with nowhere to get
    tiles from is the ordinary first moment of one, not a failure."""
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=3)])))

    doc = window._doc
    assert doc is not None and doc.is_tilemap
    assert doc.cells and doc.pixel_data == b""  # cells read, no tiles behind them
    assert not window._canvas._image.isNull()  # ...and it still renders


def test_a_tilemap_bound_to_an_entry_draws_that_entrys_tiles(qtbot, tmp_path) -> None:
    """The whole point of the ENTRY binding: the map draws the *live* document
    of the entry it names, so the art and the map are never out of step."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    tiles = _make_snes_file(tmp_path)  # 8 distinct 8x8 tiles
    window._load_pixel(str(tiles))
    # Cell 1 then cell 0, so a wrong index or a flat read is visible.
    scr = _scr_file(tmp_path, [Cell(index=1), Cell(index=0)])
    window._load_pixel(str(scr))

    entry = window._workspace.find_file(str(scr))
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    entry.doc = None  # re-read through the new binding
    window._activate_entry(window._workspace.entries[0])
    window._activate_entry(entry)

    doc = window._doc
    assert doc is not None and doc.is_tilemap
    assert doc.pixel_data  # the bound entry's bytes, not the map's
    assert doc.tile_width == 8 and doc.bytes_per_tile == 32
    image = window._canvas._image
    assert not image.isNull()
    # Always entire: every one of the 4096 cells is on screen at the width the
    # format's assembly gives, never a window — 64x64 cells of 8px.
    assert (window._canvas._image.width(), window._canvas._image.height()) == (
        64 * 8,
        64 * 8,
    )


def test_a_screen_assembles_its_four_pages_and_edits_land_on_the_right_cell(
    qtbot, tmp_path
) -> None:
    """A screen file is **one 64x64 tilemap in four quadrant blocks**, not four
    maps that might go together somehow: the editor's own ``load_scr`` writes the
    blocks into a single array at row stride 64, top-left, top-right, bottom-left,
    bottom-right (``scgcad-asset-pipeline.md`` §2.7). So the assembly is stated by
    the format and there is no picker to offer.

    The load-bearing half is the way back in: the assembly moves where a cell is
    *drawn* while its position in the file stays put, so a gesture on the
    right-hand quadrant has to reach the cell that quadrant draws. Get that wrong
    and the edit lands somewhere else in the file, which is the one failure here
    that would be written to disk.
    """
    from celpix.core.tilemap import Cell

    # A cell at the start of page 0 and another at the start of page 1.
    cells = [Cell(index=1)] + [Cell()] * 1023 + [Cell(index=2)]
    window, _bank, entry = _bound_screen(qtbot, tmp_path, cells)
    doc = window._doc

    # Fixed at 2x2 by the format, and Cols is the assembly's rather than a free
    # choice, since any other width cuts the quadrants in the wrong place.
    assert doc.pages == 4
    assert doc.stated_pages_across == 2 and doc.pages_across == 2
    assert window._columns.value() == 64 and not window._columns.isEnabled()
    assert window._canvas._image.width() == 64 * 8

    # ...and a stored assembly cannot talk it out of it: the shape is the file's.
    doc.view.pages_across = 1
    assert doc.pages_across == 2 and doc.assembled_columns == 64

    # Column 32 of the top row is page 1's first cell, which lives at 1024.
    assert doc.cell_at(32) == 1024
    window._on_slots_selected(32, 32)
    assert window._selection_byte_range() == (1024 * 2, 2)  # the record, in the dump
    window._set_cell_index(9)
    assert doc.cells[1024].index == 9
    assert doc.cells[32].index == 0  # the cell it is drawn *beside* is untouched
    assert entry.pixel_dirty  # the one edit, still the only change

    # Nothing is stored for it either - a fact the container republishes on every
    # read is not the user's choice to keep.
    assert window._pages_across() == 0

    # A format with no pages has nothing to assemble, so Cols is a free choice
    # there - the lock above is the assembly's and not every tilemap's.
    panel, _ = _bound_tilemap(qtbot, tmp_path, [Cell(index=3)], maker=_pnl_file)
    assert panel._doc.pages == 0
    assert panel._columns.isEnabled()


def test_a_dense_map_that_states_no_width_keeps_cols_live(qtbot, tmp_path) -> None:
    """The other half of the lock above. A dense map's entries are one per stamp
    and no filler — a plain rectangle — so its width is the same free preference
    an ordinary tilemap's is, and the formats that come this way are slices lifted
    out of a ROM or a disk image, with no container to read a header for them. Cols
    is the only thing that can supply it, so it stays live.

    What it does not stay is unconstrained: Cols counts drawn positions and the
    resolution counts entries, so a width between two stamps floors, and the spin
    is set back to what was actually drawn rather than leaving the user reading a
    number the picture does not have."""
    from celpix.core.capabilities import ContentKind
    from celpix.core.errors import Stage
    from celpix.core.tilemap import Cell
    from celpix.plugins.base import Preset
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    # A panel for the map to stamp: its header states the 2x2, which is the one
    # half of this a dense format still reads off the source.
    window._load_pixel(str(_pnl_file(tmp_path, [Cell(index=1)])))
    panel = window._workspace.current
    panel.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(panel)

    path = tmp_path / "area.map"
    path.write_bytes(bytes(range(24)))
    window._load_pixel(str(path), content_kind=ContentKind.TILEMAP)
    entry = window._workspace.current
    window._registry.register_preset(
        Preset(
            id="preset.tilemap.dense-stamps",
            name="One byte per stamp, no stated width",
            stage=Stage.INTERPRET_TILEMAP,
            engine_id="codec.tilemap.packed",
            params={
                "bytes": 1,
                "fields": "iiii iiii",
                "indirect": True,
                "stamp_dense": True,
            },
        )
    )
    entry.tilemap_preset_id = "preset.tilemap.dense-stamps"
    entry.tile_source = TileSource(mode=TileMode.ENTRY, entry=panel)
    window._reload_tilemap(entry)

    doc = window._doc
    assert doc.chain is not None and doc.chain.dense
    assert doc.stated_columns == 0  # nothing spoke for it, which is the point
    assert window._columns.isEnabled()
    # Two cells to a stamp across, so the keys move Cols by two — a step of one
    # would floor straight back and read as a dead shortcut.
    assert window._columns.singleStep() == 2

    window._columns.setValue(8)
    assert doc.stamp_columns == 4  # eight positions across is four entries
    assert doc.drawn_columns == 8
    assert len(doc.drawn_cells) == 24 * 4

    # A width between two stamps draws the whole one below it, and says so.
    window._columns.setValue(7)
    assert doc.stamp_columns == 3
    assert window._columns.value() == 6


def test_a_binding_change_keeps_the_width_the_map_is_being_read_at(
    qtbot, tmp_path
) -> None:
    """The format's stated width seeds Cols on load, and a binding change is a
    re-read — so without the view being handed across, nudging Base tile would
    snap a map back to its format's width every time, discarding the width the
    user was reading it at.

    Undo goes the same way: it re-reads too, and a revert that also resized the
    picture would be answering a question the user did not ask."""
    from celpix.core.tilemap import Cell

    window, entry = _bound_tilemap(
        qtbot, tmp_path, [Cell(index=1), Cell(index=2)], maker=_pnl_file
    )
    assert window._doc.stated_columns == 32  # the panel's own answer, on load
    assert window._columns.value() == 32

    window._columns.setValue(8)
    window._tile_base.setValue(0x10)
    assert window._doc.tile_base_index == 0x10  # the change did land
    assert window._columns.value() == 8

    window._undo_stack.undo()
    assert window._doc.tile_base_index == 0
    assert window._columns.value() == 8


def test_a_binding_change_keeps_the_maps_unsaved_cell_edits(qtbot, tmp_path) -> None:
    """A map's cells live in its document until a save, and a binding change
    re-reads the entry — so without them being handed to that read, nudging Base
    tile would put the file's cells back and lose the edit with nothing said.

    The **buffer** goes across rather than the cell list, so a change of cell
    format reads the edit under the new codec: the same bytes, a different
    question asked of them."""
    from celpix.core.tilemap import Cell

    window, entry = _bound_tilemap(
        qtbot, tmp_path, [Cell(index=1), Cell(index=2)], maker=_pnl_file
    )
    window._on_slots_selected(1, 1)
    window._set_cell_index(7)
    assert [c.index for c in window._doc.cells[:2]] == [1, 7]
    assert entry.pixel_dirty

    window._tile_base.setValue(0x10)
    assert [c.index for c in window._doc.cells[:2]] == [1, 7]
    assert entry.pixel_dirty  # still the one unsaved edit

    # A cell format switch is a re-read too, and the edited bytes are what it
    # re-reads: a one-byte codec cuts the same edited word into two cells, so
    # the number the user typed is still in there - somewhere else, which is
    # what a different reading of the same bytes means.
    combo = window._tilemap_preset
    combo.setCurrentIndex(combo.findData("preset.tilemap.gb-bg"))
    window._on_tilemap_preset_change(combo.currentIndex())
    assert 7 in [c.index for c in window._doc.cells[:4]]
    combo.setCurrentIndex(combo.findData("preset.tilemap.scgcad-panel"))
    window._on_tilemap_preset_change(combo.currentIndex())
    assert [c.index for c in window._doc.cells[:2]] == [1, 7]

    # And undoing the edit still reaches the file's own cell, so the step that
    # was pushed is the one that comes back off.
    for _ in range(3):  # the two format switches, then the base tile
        window._undo_stack.undo()
    window._undo_stack.undo()  # the cell edit
    assert [c.index for c in window._doc.cells[:2]] == [1, 2]
    assert not entry.pixel_dirty


def test_a_binding_change_keeps_a_palette_the_session_has_not_captured(
    qtbot, tmp_path
) -> None:
    """A session is captured on the way out of an entry, so the palette it holds
    for the entry on screen is one switch out of date — and the reload a binding
    change performs carries the colours across by asking it. Set a palette, nudge
    Base tile without leaving the map, and the colours have to still be there.

    The seed is the other half: it fires only on a map still on the default
    palette, and reading the same stale answer would let it replace the palette
    the user had just chosen with the bank's."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import PaletteMode

    window, entry = _bound_tilemap(
        qtbot, tmp_path, [Cell(index=1), Cell(index=2)], maker=_pnl_file
    )
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.CUSTOM)
    )
    window._doc.palette = window._doc.palette.with_color(1, 0xFF010203)
    assert entry.session.palette_mode is PaletteMode.DEFAULT  # not captured yet

    window._tile_base.setValue(0x10)

    assert window._doc.tile_base_index == 0x10
    assert window._doc.palette.colors[1] == 0xFF010203
    assert window._palette_mode is PaletteMode.CUSTOM


def test_binding_seeds_the_maps_palette_from_the_entry_it_binds_to(
    qtbot, tmp_path
) -> None:
    """A map's tiles were authored against the bank's colours, so a fresh binding
    brings them across rather than leaving the map on the built-in default —
    seeded like a slice's, and its own from then on
    (``docs/design/tilemap-entry.md`` §3)."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import PaletteMode, TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    bank = window._workspace.entries[0]

    scr = _scr_file(tmp_path, [Cell(index=1), Cell(index=0)])
    window._load_pixel(str(scr))
    entry = window._workspace.find_file(str(scr))
    assert entry.session.palette_mode is PaletteMode.DEFAULT
    # After the switch away, so the capture it performs cannot overwrite this.
    reds = [0xFF000000 | (i << 16) for i in range(256)]
    bank.doc.palette = Palette(reds)
    bank.session.palette_mode = PaletteMode.CUSTOM
    window._activate_entry(entry)
    window._rebind_tiles(
        entry, TileSource(mode=TileMode.ENTRY, entry=window._workspace.entries[0])
    )

    assert entry.session.palette_mode is PaletteMode.CUSTOM
    assert entry.doc.palette.colors == reds
    # The live mode moves with it: a reload restores no session, so leaving this
    # behind would let the next entry switch capture Default back over the seed.
    assert window._palette_mode is PaletteMode.CUSTOM

    # The seed is part of the step the bind pushed, so it comes back off with it -
    # an undo that put the binding back and left these colours would be half a
    # revert, and the map would render through a palette nothing points at.
    window._undo_stack.undo()
    assert entry.tile_source is None
    assert entry.session.palette_mode is PaletteMode.DEFAULT
    assert entry.doc.palette.colors != reds
    window._undo_stack.redo()
    assert entry.session.palette_mode is PaletteMode.CUSTOM
    assert entry.doc.palette.colors == reds

    # Re-pointing does not re-seed: the palette is the map's own state by now,
    # and a second bank's colours must not silently replace it.
    other = tmp_path / "other.bin"
    other.write_bytes(bytes((i * 7 + 3) & 0xFF for i in range(32 * 8)))
    window._load_pixel(str(other))
    second = window._workspace.find_file(str(other))
    second.doc.palette = Palette([0xFF00FF00] * 256)
    second.session.palette_mode = PaletteMode.CUSTOM
    window._activate_entry(entry)
    window._rebind_tiles(
        entry,
        TileSource(mode=TileMode.ENTRY, entry=second),
    )
    assert entry.doc.palette.colors == reds


def test_an_index_only_tilemap_is_read_through_subpal(qtbot, tmp_path) -> None:
    """A format with no palette field says nothing about which colours its tiles
    index — a Game Boy map entry is a bare tile number — so the palette panel's
    own row is the only answer there is, and Subpal works exactly as it does on a
    pixel document."""
    from celpix.core.tilemap import Cell

    window, _bank, _entry = _bound_screen(
        qtbot, tmp_path, [Cell(index=1), Cell(index=0)]
    )
    combo = window._tilemap_preset
    combo.setCurrentIndex(combo.findData("preset.tilemap.gb-bg"))
    window._on_tilemap_preset_change(combo.currentIndex())
    assert not window._doc.cells_carry_palette_rows
    assert window._subpalette.isEnabled()
    before = window._canvas._image.copy()
    window._subpalette.setValue(1)
    window._refresh_view()
    assert window._canvas._image != before


def test_a_format_with_palette_rows_ignores_subpal_even_at_row_zero(
    qtbot, tmp_path
) -> None:
    """The other half, and the case that makes it a *format* question: a console
    BG cell has a palette field, so the file has answered even where every cell
    answers 0. A view-wide row on top would shift a map that is in the colours it
    was authored in — the way to change one is to edit the cells.

    The spin stays live all the same, and its tooltip is the whole of the
    difference: it is the row being *picked*, which is what the assignment
    gesture writes into those cells. Only the render ignores it."""
    from celpix.core.tilemap import Cell
    from celpix.ui.main_window.interpretation import SUBPAL_CELLS_TIP

    window, _bank, _entry = _bound_screen(
        qtbot, tmp_path, [Cell(index=1), Cell(index=0)]
    )
    assert window._doc.cells_carry_palette_rows
    assert window._subpalette.isEnabled()
    assert window._subpalette.toolTip() == SUBPAL_CELLS_TIP
    before = window._canvas._image.copy()
    window._subpalette.setValue(1)
    window._refresh_view()
    assert window._canvas._image == before


def test_show_tile_ids_labels_each_cell_with_the_tile_it_names(qtbot, tmp_path) -> None:
    """One label per *cell* at the cell's first tile slot, carrying the file's own
    index — not the resolved one, so the numbers hold still when Base tile moves.
    Off for a pixel entry, which has no named tiles to show."""
    from celpix.core.tilemap import Cell

    window, bank, entry = _bound_screen(
        qtbot, tmp_path, [Cell(index=0x1C4), Cell(index=2)]
    )
    assert window._canvas._tile_ids is None  # off by default
    window._show_tile_ids_action.setChecked(True)
    labels = window._canvas._tile_ids
    assert labels[:2] == [0x1C4, 2]
    # A base tile shifts which tile is drawn, never what the cell says it is.
    window._tile_base.setValue(0x10)
    assert window._canvas._tile_ids[:2] == [0x1C4, 2]

    # The pixel entry the map borrows its tiles from gets no labels, and the
    # toggle goes grey there rather than claiming it would do something.
    window._activate_entry(bank)
    assert window._canvas._tile_ids is None
    assert not window._show_tile_ids_action.isEnabled()


def test_tile_id_labels_are_one_per_cell_on_a_metatile_map(qtbot, tmp_path) -> None:
    """A 2x2 cell covers four tile slots and the canvas places in slots, so the
    label rides its *first* one and the other three stay blank — one number per
    cell, not four."""
    from celpix.core.tilemap import Cell

    window, _bank, entry = _bound_screen(
        qtbot, tmp_path, [Cell(index=1), Cell(index=2)]
    )
    entry.doc.cell_tiles = (2, 2)
    window._show_tile_ids_action.setChecked(True)
    window._refresh_view()
    assert window._canvas._tile_ids[:8] == [1, None, None, None, 2, None, None, None]


def test_a_map_selects_every_cell_it_has_not_every_tile_the_bank_has(
    qtbot, tmp_path
) -> None:
    """A tilemap's canvas slots are its *cells*, and the bank it borrows tiles
    from bounds neither how many there are nor which can be pointed at. Bounding
    the selection by ``tile_count`` — the bank's — puts most of an ordinary map
    (4096 cells over a few hundred tiles) out of reach of every gesture that
    starts with selecting something."""
    from celpix.core.tilemap import Cell

    window, _bank, _entry = _bound_screen(
        qtbot, tmp_path, [Cell(index=i % 8) for i in range(100)]
    )
    cells = len(window._doc.cells)  # a screen's own extent - 4096 of them
    assert window._doc.tile_count == 8  # the bank, far short of the map

    window._on_slots_selected(cells - 1, cells - 1)
    assert window._selected_cells() == [cells - 1]
    window._select_all()
    assert len(window._selected_cells()) == cells


def test_a_metatile_map_selects_whole_cells_in_either_shape(qtbot, tmp_path) -> None:
    """Half a 16x16 cell is not something the file has: every gesture downstream
    of a selection works a whole cell at a time, so pointing at one quadrant
    takes the cell. Both shapes snap outward, and the block that comes back
    divides cleanly into cell coordinates — which is what lets one click flip a
    whole cell rather than nothing."""
    from celpix.core.tilemap import Cell
    from celpix.ui.main_window.selection import SelectionShape
    from celpix.ui.widgets import select_combo_data

    window, _bank, entry = _bound_screen(
        qtbot, tmp_path, [Cell(index=i) for i in range(64)]
    )
    entry.doc.cell_tiles = (2, 2)
    window._refresh_view()

    for shape in (SelectionShape.LINEAR, SelectionShape.RECT):
        select_combo_data(window._selection_shape, shape)
        # Slot 3 is the bottom-right quadrant of cell 0; slot 65 the second
        # quadrant of the cell one map-row down.
        for slot, cell in ((3, 0), (65, 16)):
            window._on_slots_selected(slot, slot)
            assert window._selected_cells() == [cell], (shape, slot)
            assert window._cell_rect() == (1, 1, cell % 32, cell // 32)

    # A drag over three quadrants of one cell row still ends on whole cells.
    select_combo_data(window._selection_shape, SelectionShape.RECT)
    window._on_slots_selected(1, 6)
    assert window._selected_cells() == [0, 1]
    assert window._cell_rect() == (2, 1, 0, 0)


def test_the_size_readout_names_the_cell_on_a_tilemap(qtbot, tmp_path) -> None:
    """What the user points at on a map is a cell, so a 2x2-tile one reads 16x16
    and the caption says which unit that is. The bank behind it still reads 8x8 —
    the tile is the pixel view's unit, and that view is the bank's own entry."""
    from celpix.core.tilemap import Cell

    window, bank, entry = _bound_screen(qtbot, tmp_path, [Cell(index=1)])
    assert (window._tile_size_label.text(), window._tile_size.text()) == (
        "Cell:",
        "8×8",
    )
    entry.doc.cell_tiles = (2, 2)
    window._refresh_view()
    assert window._tile_size.text() == "16×16"

    window._activate_entry(bank)
    assert (window._tile_size_label.text(), window._tile_size.text()) == (
        "Tile:",
        "8×8",
    )


def test_the_hex_dump_under_a_tilemap_shows_its_cells_not_its_tiles(
    qtbot, tmp_path
) -> None:
    """A tilemap document holds two files: its own cells, and the bank it borrows
    tiles from riding in ``pixel_data``. The entry is the first, so that is what
    the dump shows — and because a selection names positions in it, the highlight
    lands on the record behind the cell that was clicked."""
    from celpix.core.tilemap import Cell

    window, _bank, _entry = _bound_screen(
        qtbot, tmp_path, [Cell(index=0x1C4), Cell(index=2)]
    )
    window.show()
    window._hex_dock.setVisible(True)

    # The cells, little-endian, from the screen's payload at file offset 0 - not
    # the 4bpp tile bytes the bank behind the map begins with.
    first = window._hex_panel._view.toPlainText().splitlines()[0].split()
    assert first[:5] == ["0x000000", "c4", "01", "02", "00"]

    assert window._selection_byte_range() is None
    window._on_slots_selected(1, 1)
    assert window._selected_cells() == [1]
    assert window._selection_byte_range() == (2, 2)  # cell 1, one 2-byte word
    assert "<span" in window._hex_panel._view.toHtml()


def test_a_cell_edit_reaches_the_bytes_the_dump_and_export_raw_read(
    qtbot, tmp_path
) -> None:
    """The cells are the source of truth and the buffer is what they were read
    from, so an edit has to carry into it or everything reading the *bytes* shows
    the file as it was opened: the dump, and Export Raw. Undo puts them back."""
    from celpix.core.tilemap import Cell
    from celpix.ui import export

    window, _bank, _entry = _bound_screen(
        qtbot, tmp_path, [Cell(index=1), Cell(index=2)]
    )
    window.show()
    window._hex_dock.setVisible(True)
    window._on_slots_selected(0, 0)
    window._set_cell_index(5)

    assert window._doc.tilemap_data[:4] == b"\x05\x00\x02\x00"
    assert export.raw_bytes(window._doc)[:2] == b"\x05\x00"
    assert window._hex_panel._view.toPlainText().splitlines()[0].split()[1] == "05"
    # The trailing bytes the decode never claimed are left exactly as they were:
    # a screen's payload is a whole number of cells, so this is the whole file.
    assert len(window._doc.tilemap_data) == len(window._doc.cells) * 2

    window._undo_stack.undo()
    assert window._doc.tilemap_data[:2] == b"\x01\x00"


def test_a_tilemaps_subpalette_is_sized_by_the_bound_entrys_format(
    qtbot, tmp_path
) -> None:
    """A tilemap has no pixel format of its own and the picker is hidden on it, so
    the combo holds whatever the toolbar showed when the map was opened. The row
    size has to come from the format the *tiles* are read under, or Subpal steps
    in blocks of a bit depth nothing on screen is using."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource
    from celpix.ui.widgets import select_combo_data

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))  # the bank, left at 4bpp

    # A second entry the user switches to 2bpp — what the map's session is seeded
    # from, and what its (hidden) picker goes on holding.
    other = tmp_path / "other.bin"
    other.write_bytes(bytes((i * 7 + 3) & 0xFF for i in range(32 * 8)))
    window._load_pixel(str(other))
    select_combo_data(window._pixel_preset, "preset.pixel.snes-2bpp")

    scr = _scr_file(tmp_path, [Cell(index=1), Cell(index=0)])
    window._load_pixel(str(scr))
    entry = window._workspace.find_file(str(scr))
    window._activate_entry(entry)
    window._rebind_tiles(
        entry, TileSource(mode=TileMode.ENTRY, entry=window._workspace.entries[0])
    )

    assert entry.session.pixel_preset_id == "preset.pixel.snes-2bpp"
    assert window._doc.pixel_config.interpret_preset_id == "preset.pixel.snes-4bpp"
    assert window._index_space() == 16


def test_binding_from_a_file_opens_it_as_an_entry_first(
    qtbot, tmp_path, monkeypatch
) -> None:
    """Picking a file registers it in the list rather than hiding a path inside
    the binding — the move a palette file already makes. It then carries its own
    format and its own Write, and the map reads it through both."""
    from PySide6.QtWidgets import QFileDialog

    from celpix.core.capabilities import ContentKind
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode

    window = MainWindow()
    qtbot.addWidget(window)
    tiles = _make_snes_file(tmp_path)
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    entry = window._workspace.current
    # A row base of its own first, which is what makes the bind below a real test
    # of the re-read: this path points the entry at its source before the step
    # applies, so an apply that asked the *entry* what had moved would see only a
    # row base, land in place, and leave the tiles it just opened unread.
    window._row_base.setValue(1)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(tiles), ""))
    )

    window._bind_tiles_from_file(entry)

    bound = window._workspace.find_file(str(tiles))
    assert bound is not None and bound.content_kind is ContentKind.PIXELS
    assert entry.tile_source.mode is TileMode.ENTRY
    assert entry.tile_source.entry is bound
    # The view comes back to the map: tiles were asked for *for* it.
    assert window._workspace.current is entry
    assert window._doc.is_tilemap and window._doc.pixel_data


def test_binding_from_a_file_whose_own_source_is_a_tilemap_is_refused(
    qtbot, tmp_path, monkeypatch
) -> None:
    """One hop: a map already drawing through a tilemap has no tiles of its own
    to lend, so it cannot be a source. The file still opens — it was asked for —
    but the binding is left alone rather than pointed somewhere useless."""
    from PySide6.QtWidgets import QFileDialog

    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    # A chain that is already one deep: other.SCR draws through screen.SCR.
    other = _scr_file(tmp_path, [Cell(index=2)], name="other.SCR")
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    window._load_pixel(str(other))
    window._workspace.current.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    entry = window._workspace.entries[0]
    window._activate_entry(entry)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(other), ""))
    )

    window._bind_tiles_from_file(entry)

    assert entry.tile_source is None  # nothing was bound
    assert window._workspace.current is entry
    # ...and it is not offered either, so the two cannot disagree.
    labels = [
        window._tile_binding.itemText(i) for i in range(window._tile_binding.count())
    ]
    assert "other.SCR" not in labels


def test_the_bottom_bar_swaps_to_the_binding_controls_for_a_tilemap(
    qtbot, tmp_path
) -> None:
    """A tilemap has no view window to move, so the offset controls are replaced
    rather than left on screen dead."""
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    assert window._nav_stack.currentWidget() is window._navbar

    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    assert window._nav_stack.currentWidget() is window._tilemap_bar

    # ...and back, so the swap is driven by the entry rather than one-way.
    window._activate_entry(window._workspace.entries[0])
    assert window._nav_stack.currentWidget() is window._navbar


def test_closing_a_tilemap_takes_its_binding_bar_with_it(qtbot, tmp_path) -> None:
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    assert window._nav_stack.currentWidget() is window._tilemap_bar
    window._workspace.close(window._workspace.entries[0])
    assert window._nav_stack.currentWidget() is window._navbar


def test_the_binding_combo_offers_pixel_entries_but_not_the_map(
    qtbot, tmp_path
) -> None:
    """A tilemap cannot supply its own tiles, and offering it would bind an
    entry to its own bytes."""
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))

    combo = window._tile_binding
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert labels == ["(none)", "s.4bpp.sfc", "From file..."]
    assert combo.currentIndex() == 0  # unbound to start


def test_choosing_an_entry_in_the_bar_binds_and_redraws(qtbot, tmp_path) -> None:
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1), Cell(index=0)])))
    entry = window._workspace.current
    assert window._doc.pixel_data == b""  # nothing bound yet

    combo = window._tile_binding
    combo.setCurrentIndex(combo.findText("s.4bpp.sfc"))
    window._on_tile_binding_change(combo.currentIndex())

    assert entry.tile_source.mode is TileMode.ENTRY
    assert entry.tile_source.entry is window._workspace.entries[0]
    assert window._doc.pixel_data  # re-read through the binding
    assert not window._canvas._image.isNull()


def test_the_base_tile_shifts_which_tiles_the_cells_draw(qtbot, tmp_path) -> None:
    """A map indexing from partway into a bank resolves without its cells being
    rewritten - the formats carry this as a header field."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=0)])))
    entry = window._workspace.current
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(entry)

    window._tile_base.setValue(3)
    window._on_tile_base_change(3)
    assert entry.tile_source.base_index == 3
    assert window._doc.tile_base_index == 3
    assert window._doc.cell_tile_indices(Cell(index=0)) == [3]


def test_the_base_row_moves_a_map_onto_the_palette_that_was_loaded(
    qtbot, tmp_path
) -> None:
    """The colour twin of Base tile: a sprite's cells count their rows from CGRAM
    row 8 and the format says so, but which palette got loaded is the user's
    answer - the object half on its own puts those same rows at 0. So the spin
    opens on the format's word and overrides it, and the entry keeps the override
    so a re-read does not undo it."""
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_obj_file(tmp_path, [(0, 0, 1)])))
    obj = window._workspace.current
    # The format's answer, in force and on screen without anyone setting it.
    assert window._doc.palette_row_base == 8
    assert window._row_base.value() == 8
    assert not window._row_base.isHidden()

    window._row_base.setValue(0)
    assert obj.palette_row_base == 0  # the entry's, for the project to keep
    assert window._doc.palette_row_base == 0
    window._reload_tilemap(obj)
    assert window._doc.palette_row_base == 0  # and it outlives the re-read

    # A format with no palette row field has no row for a base to shift: the
    # colours are the view's Subpal there, and the two are never both live.
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    combo = window._tilemap_preset
    combo.setCurrentIndex(combo.findData("preset.tilemap.scgcad-std"))
    window._on_tilemap_preset_change(combo.currentIndex())
    assert not window._doc.cells_carry_palette_rows
    assert window._row_base.isHidden()
    assert window._row_base_label.isHidden()
    assert window._subpalette.isEnabled()


def test_a_bank_that_states_a_row_base_beats_the_sprite_preset(qtbot, tmp_path) -> None:
    """A sprite object names a 3-bit palette row and carries nothing to count it
    from, so the preset's 8 is standing in for the commonest case. The bank its
    subsprites draw from does state a base - the same origin its own per-tile row
    table counts from - so the art wins over the constant, and the entry's own
    answer still wins over both."""
    window = MainWindow()
    qtbot.addWidget(window)
    # col_half 0: the BG half, so this bank's tiles count from row 0 and an
    # object bound to it must too. Picked *because* it disagrees with the
    # preset's 8 — a bank on the OBJ half would agree with it by luck and prove
    # nothing about which one answered.
    window._load_pixel(str(_cgx_file(tmp_path, col=(0, 0))))
    window._load_pixel(str(_obj_file(tmp_path, [(0, 0, 1)])))
    obj = window._workspace.current
    combo = window._tile_binding
    combo.setCurrentIndex(combo.findData(window._workspace.entries[0]))
    window._on_tile_binding_change(combo.currentIndex())
    assert window._doc.palette_row_base == 0
    assert window._row_base.value() == 0

    # And the cell beside the half is 2bpp-only, so a 4bpp bank carrying one is
    # still the half's answer alone: 300 of 2,650 banks carry a cell, and adding
    # it to a 4bpp base is what put fourteen of them past the end of CGRAM.
    window._load_pixel(str(_cgx_file(tmp_path, col=(1, 1), name="cell.CGX")))
    window._activate_entry(obj)  # loading a bank makes it current; go back
    combo.setCurrentIndex(combo.findData(window._workspace.entries[2]))
    window._on_tile_binding_change(combo.currentIndex())
    assert window._doc.palette_row_base == 8

    # Still the user's to overrule, and the override outlives the re-read that
    # would otherwise put the bank's answer back.
    window._row_base.setValue(3)
    assert obj.palette_row_base == 3
    window._reload_tilemap(obj)
    assert window._doc.palette_row_base == 3


def test_a_bank_with_no_header_leaves_the_sprite_preset_to_answer(
    qtbot, tmp_path
) -> None:
    """A base read off a header that is not there would be an invention. Thirty-
    nine banks of the corpus carry no signature at all, and a raw pixel file has
    no header to carry - both leave the format's own word standing."""
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_obj_file(tmp_path, [(0, 0, 1)])))
    combo = window._tile_binding
    combo.setCurrentIndex(combo.findData(window._workspace.entries[0]))
    window._on_tile_binding_change(combo.currentIndex())
    assert window._doc.palette_row_base == 8


def test_a_screen_states_its_own_row_base_over_the_banks(qtbot, tmp_path) -> None:
    """Both files answer here, and the map's own header is the closer authority:
    a screen carries the colour bytes for the cells *it* draws, where the bank
    carries them for tiles that any number of maps may share."""
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_cgx_file(tmp_path, col=(1, 1))))
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    scr = window._workspace.current
    combo = window._tile_binding
    combo.setCurrentIndex(combo.findData(window._workspace.entries[0]))
    window._on_tile_binding_change(combo.currentIndex())
    # The screen's own header is all zero, and a stated 0 is an answer.
    assert scr.palette_row_base is None
    assert window._doc.palette_row_base == 0


def test_changing_the_cell_codec_rereads_the_map(qtbot, tmp_path) -> None:
    """The two byte orders disagree, so this is a real re-read and not a redraw."""
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=0x034, palette_row=7)])))
    entry = window._workspace.current
    assert window._doc.cells[0].index == 0x034

    combo = window._tilemap_preset
    combo.setCurrentIndex(combo.findData("preset.tilemap.snes-bg-swapped"))
    window._on_tilemap_preset_change(combo.currentIndex())

    assert entry.tilemap_preset_id == "preset.tilemap.snes-bg-swapped"
    # The same bytes read the other way: 0x1C34 becomes 0x341C.
    assert window._doc.cells[0].index == 0x01C
    assert window._doc.cells[0].palette_row == 5

    # And it takes a step, like the pixel format switch it is the twin of: the
    # re-read is what makes a wrong pick worth taking back in one gesture.
    window._undo_stack.undo()
    assert entry.tilemap_preset_id == "preset.tilemap.snes-bg"
    assert window._doc.cells[0].index == 0x034
    assert window._tilemap_preset.currentData() == "preset.tilemap.snes-bg"


def test_the_codecs_bar_swaps_its_format_pickers_by_content_kind(
    qtbot, tmp_path
) -> None:
    """A tilemap's bytes are cells, so the pixel format and the compression
    preview say nothing about it — the cell format takes their place rather than
    joining them (``docs/design/tilemap-entry.md`` §4)."""
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    assert window._pixel_codec_action.isVisible()
    assert window._compression_action.isVisible()
    assert not window._tilemap_codec_action.isVisible()

    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    assert not window._pixel_codec_action.isVisible()
    assert not window._compression_action.isVisible()
    assert window._tilemap_codec_action.isVisible()

    # Back again, and enabled with it: a hidden group that came back grey would
    # be worse than one that never left.
    window._activate_entry(window._workspace.entries[0])
    assert window._pixel_codec_action.isVisible()
    assert window._pixel_codec_action.isEnabled()


def test_the_bar_says_where_the_tiles_come_from(qtbot, tmp_path) -> None:
    """The pixel format is the bound entry's own, so the bar reports it rather
    than offering a control that would fight that entry's own picker."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=0)])))
    entry = window._workspace.current
    assert "No tiles bound" in window._tile_binding_note.text()

    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(entry)
    note = window._tile_binding_note.text()
    assert "s.4bpp.sfc" in note and "SNES 4bpp" in note


def test_the_bar_jumps_to_the_entry_the_tiles_come_from(qtbot, tmp_path) -> None:
    """The binding is the one control whose value is another entry, so the button
    beside it goes and shows that entry - and the window's Back comes back."""
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=0)])))
    tiles, tilemap = window._workspace.entries
    assert not window._tile_binding_jump.isEnabled()  # unbound: nowhere to go

    tilemap.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(tilemap)
    assert window._tile_binding_jump.isEnabled()
    assert tiles.name in window._tile_binding_jump.toolTip()

    window._tile_binding_jump.click()
    assert window._workspace.current is tiles
    window._history_step(-1)
    assert window._workspace.current is tilemap


def test_a_tilemap_switches_off_the_controls_it_has_no_capability_for(
    qtbot, tmp_path
) -> None:
    """One declared table in place of a dozen "...and not on a tilemap" clauses
    (docs/design/tilemap-entry.md §4)."""
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    # The pixel entry keeps everything: its capability set is the full one, so
    # the sync is a no-op there and nothing it enabled has been taken away.
    # (The panel's *enabled* state is edit mode's business, not this gate's.)
    assert not window._tools_panel.isHidden()
    assert window._edit_mode_action.isEnabled()

    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    # The brushes stay: a tilemap *is* painted on, through the entry it borrows
    # its tiles from. What decides whether they can be reached is per document
    # rather than per kind, so it is the mode toggle that goes grey — this map
    # has nothing bound, and a brush with no bank under it would write nowhere.
    assert not window._tools_panel.isHidden()
    assert not window._edit_mode_action.isEnabled()
    assert not window._rearrange_action.isEnabled()
    assert not window._pin_palette_action.isEnabled()
    assert not window._import_png_action.isEnabled()


def test_a_missing_entry_is_gated_as_the_kind_it_is(qtbot, tmp_path) -> None:
    """The unavailable state runs the gate too, and it has an entry to gate by.

    It renders nothing, so before it did the pass was the only state that kept
    the *previous* document's furniture: a missing tilemap wore the pixel format
    picker and the position bar (an accent rail, greyed but in full colour over a
    document that has no window), a missing pixel file wore the cell format. The
    two menu toggles the gate owns outright sit on no toolbar, so they said the
    wrong thing rather than merely looking inert.
    """
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    px = _make_snes_file(tmp_path)
    window._load_pixel(str(px))
    scr = _scr_file(tmp_path, [Cell(index=1)])
    window._load_pixel(str(scr))
    pixels, tilemap = window._workspace.entries

    # A pixel file on screen, then the tilemap's file is gone: gated as a tilemap.
    window._activate_entry(pixels)
    scr.unlink()
    window._activate_entry(tilemap)
    assert window._tilemap_codec_action.isVisible()
    assert not window._pixel_codec_action.isVisible()
    assert not window._compression_action.isVisible()
    assert window._tile_offset_bar.isHidden()
    assert window._show_tile_ids_action.isEnabled()
    assert not window._show_palette_regions_action.isEnabled()

    # ...and the other way round, which is the half a fixed default would miss.
    px.unlink()
    window._activate_entry(pixels)
    assert not window._tilemap_codec_action.isVisible()
    assert window._pixel_codec_action.isVisible()
    assert not window._show_tile_ids_action.isEnabled()
    assert window._show_palette_regions_action.isEnabled()


def test_the_navigate_menu_loses_its_window_rows_on_a_tilemap(qtbot, tmp_path) -> None:
    """The menu is the third surface onto the view window, and the only one that
    was left behind: the position bar hides and the nav bar is replaced, while
    every row that moves or resizes the window stayed live and inert.

    The column rows are not window rows - a map's cell width is its own setting,
    which is why the kind's refusal names the row count and the position.
    """
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    assert all(action.isEnabled() for action in window._nav_window_actions)

    window._load_pixel(str(_pnl_file(tmp_path, [Cell(index=1)])))
    assert not any(action.isEnabled() for action in window._nav_window_actions)
    # Columns are still the map's to set, through the same menu.
    was = window._columns.value()
    window._adjust_spin(window._columns, 1)
    assert window._columns.value() == was + 1


def test_selecting_a_cell_does_not_hand_back_import_from_png(qtbot, tmp_path) -> None:
    """``_sync_edit_actions`` runs on every selection change and the gating pass
    does not, so a veto applied at the end of the last render was handed straight
    back by the next click on a cell - and the row stayed live until something
    re-rendered."""
    from celpix.core.tilemap import Cell

    window, _ = _bound_tilemap(qtbot, tmp_path, [Cell(index=1), Cell(index=2)])
    assert not window._import_png_action.isEnabled()
    window._select_tiles(0, 0)
    assert not window._import_png_action.isEnabled()


def test_new_slice_from_view_is_refused_where_there_is_no_view(
    qtbot, tmp_path, monkeypatch
) -> None:
    """A tilemap is shown entire, so there is no window for this to cover - and
    the prefill it computed was measured in the *bound bank's* bytes and then
    written into a slice of the map. Both the action and the gesture, because
    the files dock builds a row of its own for it."""
    from celpix.core.tilemap import Cell

    window, _ = _bound_tilemap(qtbot, tmp_path, [Cell(index=1)])
    assert not window._new_slice_from_view_action.isEnabled()

    opened: list = []
    monkeypatch.setattr(
        MainWindow,
        "_create_slice_via_dialog",
        lambda self, entry, **kw: opened.append(entry),
        raising=False,
    )
    window._new_slice_from_view()
    assert not opened

    # The pixel entry it borrows from still has one, so this is the kind's answer
    # and not a document that stopped being sliceable.
    window._activate_entry(window._workspace.entries[0])
    assert window._new_slice_from_view_action.isEnabled()
    window._new_slice_from_view()
    assert opened == [window._workspace.entries[0]]


def test_removing_the_bound_bank_puts_the_edit_mode_toggle_down(
    qtbot, tmp_path, monkeypatch
) -> None:
    """Closing the bank takes a map's pixels away without the view moving, so no
    render follows to re-ask. ``_drop_unavailable_edit_mode`` only acts when
    pixel mode was on; from tile mode the toggle was left offering a brush over a
    map with nothing to deposit into."""
    from PySide6.QtWidgets import QMessageBox

    from celpix.core.tilemap import Cell

    window, tilemap = _bound_tilemap(qtbot, tmp_path, [Cell(index=1)])
    assert window._toggle_edit_mode_action.isEnabled()

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    window._remove_entry(window._workspace.entries[0])
    assert window._workspace.current is tilemap
    assert not window._toggle_edit_mode_action.isEnabled()
    assert not window._edit_mode_action.isEnabled()


def test_removing_the_bound_bank_takes_its_art_off_the_maps(
    qtbot, tmp_path, monkeypatch
) -> None:
    """A map holds a decoded *copy* of its bank, so closing the bank changes
    nothing about the map until it is read again - it went on drawing art out of
    a file no longer in the list, where an unresolved binding is supposed to show
    as no tiles bound. Off screen as well as on: the copy is the map's, not the
    view's. Undo is the mirror, because the binding held the entry itself and the
    re-insert makes it resolve again."""
    from PySide6.QtWidgets import QMessageBox

    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window, shown = _bound_tilemap(qtbot, tmp_path, [Cell(index=1)])
    bank = window._workspace.entries[0]
    art = bytes(bank.doc.pixel_data)
    assert art

    # A second map on the same bank, deliberately left off screen.
    window._load_pixel(str(_pnl_file(tmp_path, [Cell(index=2)])))
    offscreen = window._workspace.current
    offscreen.tile_source = TileSource(mode=TileMode.ENTRY, entry=bank)
    window._reload_tilemap(offscreen)
    window._activate_entry(shown)
    assert bytes(window._doc.pixel_data) == art
    assert bytes(offscreen.doc.pixel_data) == art

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    window._remove_entry(bank)
    assert window._doc.pixel_data == b""
    assert offscreen.doc.pixel_data == b""
    # The cells are the map's own and survive the re-read - only the borrowed
    # half of the document was re-resolved.
    assert window._doc.cells[0].index == 1
    assert offscreen.doc.cells[0].index == 2

    window._undo_stack.undo()
    assert bytes(window._doc.pixel_data) == art
    assert bytes(offscreen.doc.pixel_data) == art
    assert window._toggle_edit_mode_action.isEnabled()


def test_leaving_a_tilemap_gives_the_pixel_controls_back(qtbot, tmp_path) -> None:
    """The veto has to lift, or one tilemap would disable the tools for the
    rest of the session."""
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    assert not window._edit_mode_action.isEnabled()  # nothing bound to paint on

    window._activate_entry(window._workspace.entries[0])
    assert not window._tools_panel.isHidden()
    assert window._edit_mode_action.isEnabled()


def test_a_tilemap_can_be_flipped_but_not_turned(qtbot, tmp_path) -> None:
    """Squareness is about the tile; the capability is about the document. A
    hardware cell carries mirror bits and no transpose bit, so both conditions
    are needed and neither implies the other."""
    from celpix.core.capabilities import Capability, ContentKind, supports
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    assert supports(ContentKind.TILEMAP, Capability.CELL_FLIP)
    assert window._can(Capability.CELL_FLIP)
    # Rotate is designed *out*: a hardware cell has no transpose bit.
    assert not supports(ContentKind.TILEMAP, Capability.CELL_ROTATE)
    assert not window._can(Capability.CELL_ROTATE)

    window._set_linear_selection(0, 0)
    for action in window._tile_group.rotates:
        assert not action.isEnabled()


def test_a_tilemap_puts_the_rearrange_tool_down_rather_than_greying_it(
    qtbot, tmp_path
) -> None:
    """A rearrangement is display state because it moves no bytes; a tilemap's
    bytes *are* the arrangement. The capability table greys the switches, but an
    armed tool has to be put *down* — otherwise the canvas keeps reading drags as
    rearrange gestures behind three disabled buttons."""
    from celpix.core.capabilities import Capability
    from celpix.core.tilemap import Cell
    from celpix.core.tilerearrangement import TileRearrangement
    from celpix.ui.main_window.rearrange import REARRANGE_TILEMAP_TIP

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    pixels = window._workspace.current
    window._set_tile_rearrangement(TileRearrangement().swap(0, 5))
    window._set_rearranging(True)

    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    assert not window._can(Capability.TILE_REARRANGE)
    assert not window._rearrange_available()
    assert not window._rearranging  # disarmed, not merely greyed
    assert not window._canvas._rearranging
    for action in (
        window._rearrange_action,
        window._toggle_rearrange_action,
        window._show_rearranged_action,
    ):
        assert not action.isEnabled()
    assert window._rearrange_action.toolTip() == REARRANGE_TILEMAP_TIP
    # The keys press these actions, so disabling them covers the keyboard too.
    window._toggle_rearranging()
    assert not window._rearranging
    assert window._active_tile_rearrangement().is_identity()

    # The pixel entry's rearrangement was kept, not discarded.
    window._activate_entry(pixels)
    assert window._rearrange_action.isEnabled()
    assert window._show_rearranged_action.isEnabled()
    assert window._active_tile_rearrangement() == TileRearrangement().swap(0, 5)


def test_the_empty_state_is_gated_like_any_other_content_kind(
    qtbot, tmp_path, monkeypatch
) -> None:
    """The gating pass used to run only from the render, which needs a document -
    so the cell format picker sat on the codecs bar before anything was open, and
    came back when the last entry was closed. Nothing open reads as pixels, so the
    bars that configure the next open stay and the tilemap ones go."""
    from PySide6.QtWidgets import QMessageBox

    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    # Startup: the pixel half of the bar is what a next open would use.
    assert not window._tilemap_codec_action.isVisible()
    assert window._pixel_codec_action.isVisible()
    assert window._compression_action.isVisible()

    # A tilemap swaps them, which is the control on the assertion above.
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    assert window._tilemap_codec_action.isVisible()
    assert not window._pixel_codec_action.isVisible()

    # ...and closing back down to nothing puts the empty state back, rather than
    # leaving the last entry's bar describing a window with no document.
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Yes
    )
    for entry in list(window._workspace.entries):
        window._remove_entry(entry)
    assert window._workspace.current is None
    assert not window._tilemap_codec_action.isVisible()
    assert window._pixel_codec_action.isVisible()


def test_the_capability_gate_only_ever_takes_away(qtbot, tmp_path) -> None:
    """It runs last, so it must not switch anything *on* that an earlier pass
    disabled for its own good reasons - here, paste with an empty clipboard."""
    from PySide6.QtWidgets import QApplication

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    QApplication.clipboard().clear()
    window._sync_selection_actions()
    was = window._paste_action.isEnabled()
    window._sync_capabilities()
    assert window._paste_action.isEnabled() == was


def test_every_capability_says_where_it_is_gated() -> None:
    """The drift guard: TILE_BINDING and STAMP were declared, granted to
    tilemaps, and gated nothing at all for a whole feature's worth of work,
    because nothing forced the question to be answered. A capability is now in
    exactly one of the three buckets, so adding one means saying which."""
    from celpix.core.capabilities import Capability
    from celpix.ui.main_window import capability_sync as sync

    buckets = (
        frozenset(sync._GATES),
        sync._GATED_IN_PLACE,
        sync._UNGATED,
    )
    covered = set().union(*buckets)
    assert covered == set(Capability), (
        "unclassified: add it to _GATES, _GATED_IN_PLACE or _UNGATED - "
        f"{sorted(c.name for c in set(Capability) - covered)}"
    )
    for one, other in itertools.combinations(buckets, 2):
        assert not one & other, sorted(c.name for c in one & other)


def test_every_kind_can_do_the_gestures_it_implements() -> None:
    """The invariant tying gating to dispatch, which neither half shows on its
    own: a kind with an implementation of a gesture has to declare the capability
    that gates it, or the control is switched off over working code.

    The method names go the same way. They are resolved by ``getattr``, so a
    rename that missed the table would not fail here at all - a paste on a
    tilemap would quietly fall through to the pixel one and write pixels."""
    from celpix.core.capabilities import supports
    from celpix.ui.main_window import capability_sync as sync

    for kind, gestures in sync._BEHAVIOURS.items():
        for gesture, name in gestures.items():
            capability = sync._GESTURE_CAPABILITY[gesture]
            assert supports(kind, capability), f"{kind.name} lacks {capability.name}"
            assert hasattr(MainWindow, name), f"{kind.name}: no {name}()"
    assert set(sync._GESTURE_CAPABILITY) == set(sync.Gesture)


def test_a_dropped_png_is_refused_on_a_tilemap(qtbot, tmp_path) -> None:
    """The Import action is capability-gated, but a drop is a gesture with no
    control to disable - and a tilemap's ``pixel_data`` is the *bound* entry's
    art, so an unguarded import painted over another file and marked the wrong
    entry dirty for a change its Write could never emit."""
    from PySide6.QtGui import QImage

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
    png = tmp_path / "picture.png"
    image = QImage(16, 16, QImage.Format.Format_ARGB32)
    image.fill(0xFF00FF00)
    image.save(str(png))

    before, depth = window._doc.pixel_data, window._undo_stack.count()
    window._import_dropped_png(str(png))

    assert window._doc.pixel_data == before  # the bank it borrows is untouched
    assert window._undo_stack.count() == depth
    assert not entry.pixel_dirty
    assert "import into the entry" in window.statusBar().currentMessage()


def test_a_sprite_map_offers_a_size_pair_and_nothing_else_does(qtbot, tmp_path) -> None:
    """The pair a part's size bit chooses between was a PPU register the scene set,
    so no sprite file records it and the format can only name the commonest. That
    makes it the user's, per entry — and meaningless on a grid tilemap, which has
    no parts and no size bit, so the control goes rather than greys."""
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_obj_file(tmp_path, [(0, 0, 1)])))

    assert not window._size_small.isHidden()
    assert not window._size_large.isHidden()
    # Opens on the pair in force, which is the format's until the user speaks.
    assert (
        window._size_small.value(),
        window._size_large.value(),
    ) == window._doc.sprite_size_pair

    # The two are multiples of the tile size, so a part's side in tiles is the
    # number itself - the change is remembered on the entry and reaches the
    # document, because it decides how the records decode, not how they are drawn.
    entry = window._workspace.current
    window._size_large.setValue(4)
    assert entry.sprite_size_pair == (1, 4)
    assert window._doc.sprite_size_pair == (1, 4)

    # A grid tilemap has no parts to size, so the controls are not there at all.
    from celpix.core.capabilities import ContentKind
    from celpix.core.tilemap import Cell

    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    assert window._size_small.isHidden()
    assert window._size_large.isHidden()
    assert window._size_pair_label.isHidden()

    # Nor on a sprite format whose records **state** each part's rectangle: there
    # is no size bit to resolve, so the pair would be a spin that redraws the same
    # picture. The gate is the format's declaration, so an unbound entry answers.
    records = tmp_path / "object.bin"
    records.write_bytes(bytes((0xF8, 0x05, 0x03, 0xC8, 0xF0, 0x08)))
    stated = window._workspace.add_slice(str(records), "object", 0, 6)
    stated.content_kind = ContentKind.TILEMAP
    stated.tilemap_preset_id = "preset.tilemap.md-sprite"
    window._activate_entry(stated)
    assert window._doc.is_sprite
    assert window._size_small.isHidden()
    assert window._size_pair_label.isHidden()


def test_every_binding_control_is_one_undoable_step(qtbot, tmp_path) -> None:
    """The bar sets project state no file records, and a mis-set base or a wrong
    source is exactly what Ctrl+Z is for. One snapshot type carries all four, so
    what matters per control is only whether its undo re-reads the entry or
    patches the document - and that the two routes agree about the result."""
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_obj_file(tmp_path, [(0, 0, 1)])))
    obj = window._workspace.current
    declared_pair = window._doc.sprite_size_pair

    combo = window._tile_binding
    combo.setCurrentIndex(combo.findData(window._workspace.entries[0]))
    window._on_tile_binding_change(combo.currentIndex())
    assert obj.tile_source.entry is window._workspace.entries[0]
    window._undo_stack.undo()
    assert obj.tile_source is None  # unbound is *no* source, not a blank one
    window._undo_stack.redo()
    assert obj.tile_source.entry is window._workspace.entries[0]

    window._tile_base.setValue(0x40)
    assert obj.tile_source.base_index == 0x40
    window._undo_stack.undo()
    assert obj.tile_source.base_index == 0
    assert (
        obj.tile_source.entry is window._workspace.entries[0]
    )  # the binding around it is untouched

    # The row base is the one control applied *without* a re-read, so its undo is
    # the one that could quietly take a different route back.
    assert window._doc.palette_row_base == 8  # the format's, unstated by the entry
    window._row_base.setValue(0)
    assert window._doc.palette_row_base == 0
    window._undo_stack.undo()
    # Back to what the format says rather than to an 8 pinned on the entry: the
    # entry stated nothing before, and undo has to hand that back too - or the
    # project would remember a number the user never chose.
    assert obj.palette_row_base is None
    assert window._doc.palette_row_base == 8

    window._size_large.setValue(4)
    assert obj.sprite_size_pair == (1, 4)
    window._undo_stack.undo()
    assert obj.sprite_size_pair is None
    assert window._doc.sprite_size_pair == declared_pair


def test_a_binding_that_cannot_be_read_leaves_nothing_changed(
    qtbot, tmp_path, captured_alerts
) -> None:
    """The re-read *is* how a binding takes effect, so one that fails has not
    happened. The document it dropped goes back, the fields it set come off, and
    the step never reaches the stack — a half-applied binding would leave the bar
    describing a read that did not happen over a canvas still drawn the old way,
    and the entry holding no document while the window still showed one."""
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    scr = _scr_file(tmp_path, [Cell(index=1)])
    window._load_pixel(str(scr))
    entry = window._workspace.current
    doc, steps = window._doc, window._undo_stack.count()

    scr.unlink()  # the re-read now has nowhere to go
    window._tile_base.setValue(5)

    assert captured_alerts  # the user was told why
    # The entry still holds the document the window is showing. Dropping it and
    # never replacing it is the failure this guards: from there the entry and the
    # window answer differently about the same map, and a save would write
    # through a document the entry has disowned.
    assert entry.doc is doc
    assert window._doc is doc
    assert entry.tile_source is None  # the field came back off with it
    assert window._tile_base.value() == 0  # ...and so did the spin
    assert window._undo_stack.count() == steps  # nothing to undo, so no step


def test_a_sprite_maps_size_pair_survives_the_project(qtbot, tmp_path) -> None:
    """Per-entry project state, like the two bases: the pair is not in the file, so
    losing it on reload would lose the only record of it."""
    from celpix.project.projectfile import load_project, save_project

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_obj_file(tmp_path, [(0, 0, 1)])))
    window._size_large.setValue(3)

    path = tmp_path / "p.celpix"
    save_project(window._workspace, str(path))
    assert load_project(str(path)).entries[0].sprite_size_pair == (1, 3)

    # And a format that never had one writes nothing, so an ordinary project is
    # unchanged by the control existing.
    from celpix.core.tilemap import Cell

    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    save_project(window._workspace, str(path))
    import json

    written = json.loads(path.read_text())
    assert "sprite_size_pair" not in written["entries"][-1]


def test_a_panel_opens_at_the_width_its_format_states(qtbot, tmp_path) -> None:
    """The file knows it is 32 cells across, so the user should not have to
    guess - a wrong width shears the picture instead of failing."""
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._columns.setValue(7)  # some unrelated width from a previous entry
    window._load_pixel(str(_pnl_file(tmp_path, [Cell(index=1)])))

    assert window._columns.value() == 32
    assert window._doc.cell_tiles == (1, 1)  # from the header's tile-size byte
    # ...and it is a starting point, not a lock.
    window._columns.setValue(16)
    window._refresh_view()
    assert window._tilemap_columns() == 16


def test_a_tilemap_hides_the_position_bar_and_disables_the_row_count(
    qtbot, tmp_path
) -> None:
    """Both address a view window, and a tilemap is always shown entire - so
    there is no window to set the height of or scroll through.

    The bar is *hidden* rather than greyed: its handle is styled and would still
    be sized from the bound bank's tile count, so a disabled one reads as a live
    navigator part-way through a file.
    """
    from celpix.core.tilemap import Cell

    # Big enough to have somewhere to scroll: the bar is also disabled when a
    # file fits in one page, which would make the assertion below prove nothing.
    big = tmp_path / "big.4bpp.sfc"
    big.write_bytes(bytes((i * 13 + 1) & 0xFF for i in range(32 * 4096)))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(big))
    assert window._rows.isEnabled()
    assert window._tile_offset_bar.isEnabled()
    assert not window._tile_offset_bar.isHidden()  # the window is never shown

    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    assert not window._rows.isEnabled()
    assert not window._rows_label.isEnabled()
    assert window._tile_offset_bar.isHidden()
    # ...and Rows says why, rather than borrowing Entire File's reason.
    assert "always shown whole" in window._rows.toolTip()

    # Back to pixels: the veto lifts, and Rows says what it normally says.
    window._activate_entry(window._workspace.entries[0])
    assert window._rows.isEnabled()
    assert not window._tile_offset_bar.isHidden()
    assert window._tile_offset_bar.isEnabled()
    assert "always shown whole" not in window._rows.toolTip()


def test_cols_says_what_it_counts_on_each_kind_of_document(qtbot, tmp_path) -> None:
    """A column is a tile, a map cell or a whole frame depending on what is open,
    and the number itself cannot say which. The sprite object is the reading that
    matters most: its Cols lays out the strip of *frames*, so a tip promising
    tiles per row describes the one thing it does not do."""
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    assert window._columns.toolTip() == "Tiles per row"

    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    assert window._columns.toolTip().startswith("Cells per row")

    window._load_pixel(str(_obj_file(tmp_path, [(0, 0, 1)])))
    assert window._columns.toolTip().startswith("Frames per row")
    # The caption answers the same hover, being half the control's target.
    assert window._columns_label.toolTip() == window._columns.toolTip()

    # Back on pixels, and it counts tiles again.
    window._activate_entry(window._workspace.entries[0])
    assert window._columns.toolTip() == "Tiles per row"


def test_a_tilemap_can_draw_from_a_slice(qtbot, tmp_path, monkeypatch) -> None:
    """The art a map draws from is routinely a region *inside* a bigger file, so
    a slice has to be bindable like any other pixel entry — and it must supply
    the sliced bytes, not its parent's whole file."""
    from celpix.core.tilemap import Cell

    window, tilemap, sliced = _bound_to_slice(
        qtbot, tmp_path, monkeypatch, [Cell(index=i) for i in range(16)]
    )
    doc = tilemap.doc
    assert len(doc.pixel_data) == 64 * 32  # the slice's extent, not the parent's
    assert not window._canvas._image.isNull()

    # And the binding picker offers it, or the user could never have got here.
    window._sync_tilemap_bar()
    offered = {
        window._tile_binding.itemText(i) for i in range(window._tile_binding.count())
    }
    assert {"art.bin", "art"} <= offered


def test_the_base_tile_control_takes_a_negative_value(
    qtbot, tmp_path, monkeypatch
) -> None:
    """The direction a slice needs, set by hand where the scan does not fire: a
    spin that stopped at zero could not express it at all, and the shift has to
    reach the document rather than only the entry."""
    from celpix.core.tilemap import Cell

    # A map that fits its 64-tile slice as it stands, so nothing is shifted for
    # it automatically — the control is the only thing moving the base here.
    window, tilemap, _ = _bound_to_slice(
        qtbot, tmp_path, monkeypatch, [Cell(index=8 + i) for i in range(4)]
    )
    assert tilemap.tile_source.base_index == 0

    window._tile_base.setValue(-8)
    assert tilemap.tile_source.base_index == -8
    assert tilemap.doc.cell_tile_indices(Cell(index=8)) == [0]
    assert tilemap.doc.cell_tile_indices(Cell(index=11)) == [3]
    assert not window._canvas._image.isNull()


def test_a_map_that_overflows_its_slice_is_shifted_onto_it(
    qtbot, tmp_path, monkeypatch
) -> None:
    """The scan the base index is for: a map numbering from $100 bound to a
    64-tile slice cannot be indexing it absolutely, and its own lowest index is
    the amount by which it overshoots — so binding lands it without the user
    working the offset out."""
    from celpix.core.tilemap import Cell

    window, tilemap, _ = _bound_to_slice(
        qtbot, tmp_path, monkeypatch, [Cell(index=0x100 + i) for i in range(16)]
    )
    assert tilemap.tile_source.base_index == -0x100
    assert tilemap.doc.cell_tile_indices(Cell(index=0x100)) == [0]
    assert window._tile_base.value() == -0x100


def test_an_absolutely_indexed_map_is_left_alone(qtbot, tmp_path, monkeypatch) -> None:
    """The guess has a wrong answer too. A map that already fits its source is
    indexing it absolutely, and shifting it would move every cell off the tile
    it names — so the scan only fires where the map does *not* fit as it is."""
    from celpix.core.tilemap import Cell

    # Lowest index 8, highest 23 — inside the 64-tile slice, so no shift.
    window, tilemap, _ = _bound_to_slice(
        qtbot, tmp_path, monkeypatch, [Cell(index=8 + i) for i in range(16)]
    )
    assert tilemap.tile_source.base_index == 0
    assert tilemap.doc.cell_tile_indices(Cell(index=8)) == [8]


def test_the_scan_never_overrides_a_base_the_user_set(
    qtbot, tmp_path, monkeypatch
) -> None:
    """A typed base is the user's answer, and a re-read must not walk over it —
    the rule the container's own hint already follows."""
    from celpix.core.tilemap import Cell

    window, tilemap, _ = _bound_to_slice(
        qtbot, tmp_path, monkeypatch, [Cell(index=0x100 + i) for i in range(16)]
    )
    window._tile_base.setValue(-0x80)
    tilemap.doc = None
    window._activate_entry(window._workspace.entries[0])
    window._activate_entry(tilemap)
    assert tilemap.tile_source.base_index == -0x80


def test_a_chain_grown_too_deep_after_binding_owns_no_art(qtbot, tmp_path) -> None:
    """The depth rule is checked where a binding is *made*, and a source can gain
    a binding of its own afterwards: bind a to b while b is unbound, then bind b
    to c, and a is three deep with nothing having re-asked.

    Every consumer has to re-ask per hop, because the ungated walk found art at
    the end of a chain the resolution refuses - so the map read a file of
    coordinates through a pixel codec, drew it as art, and a pen stroke deposited
    into a real art file the user was not looking at. One click cost a tile.
    """
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource

    window, bank, a = _bound_screen(qtbot, tmp_path, [Cell(index=1)])
    b = window._workspace.find_file(str(_scr_file(tmp_path, [Cell(index=2)], "b.SCR")))
    if b is None:
        window._load_pixel(str(_scr_file(tmp_path, [Cell(index=2)], "b.SCR")))
        b = window._workspace.find_file(str(tmp_path / "b.SCR"))
    c = _scr_file(tmp_path, [Cell(index=3)], "c.SCR")
    window._load_pixel(str(c))
    c_entry = window._workspace.find_file(str(c))

    # a -> b is allowed while b is unbound; c -> bank is an ordinary binding.
    window._rebind_tiles(a, TileSource(mode=TileMode.ENTRY, entry=b))
    window._rebind_tiles(c_entry, TileSource(mode=TileMode.ENTRY, entry=bank))
    # ...and now b -> c, which puts a three deep without a ever being consulted.
    window._rebind_tiles(b, TileSource(mode=TileMode.ENTRY, entry=c_entry))

    # The owner walk and the resolution agree, and neither reaches the art.
    assert window._tile_bank_owner(a) is None
    assert window._bound_tilemap(a) is None
    window._activate_entry(a)
    # So no pixel edit is offered, which is what keeps a stroke out of the bank.
    assert not window._pixel_edit_available()


def test_the_arrangement_bar_is_furniture_for_a_pixel_document(qtbot, tmp_path) -> None:
    """Pattern, block, order and the 2D walk all say how a linear run of bytes is
    cut and grouped. A tilemap places nothing linearly — its block is the cell the
    map states — so every axis on that row reads as 1x1 however it is set."""
    window, _ = _bound_tilemap(qtbot, tmp_path, [], maker=_pnl_file)
    assert window._arrange_toolbar.isHidden()

    # And back, still live: the bar has owners of its own for enablement, so a
    # veto here would have stranded it grey (capability_sync._VISIBILITY_ONLY).
    window._activate_entry(window._workspace.entries[0])
    assert not window._arrange_toolbar.isHidden()
    assert window._arrange_toolbar.isEnabled()
