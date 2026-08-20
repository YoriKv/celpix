"""Selecting tiles on the canvas and what the clipboard,
PNG import/export and the transforms do with the selection."""

from __future__ import annotations

from celpix.core.index_grid import IndexGrid
from celpix.ui.main_window import MainWindow
from uihelpers import (
    _combo_ids,
    _drag_payload,
    _fresh_settings,
    _make_snes_file,
    _open_big,
)


def test_click_selects_tile_and_selection_survives_scrolling(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtCore import QPoint, Qt

    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)

    # Tile 2 at zoom 4 spans x 64..95 in widget coords.
    assert window._zoom.value() == 4
    qtbot.mouseClick(window._canvas, Qt.MouseButton.LeftButton, pos=QPoint(65, 1))
    assert window._selected_tile == 2
    assert window._canvas._selected_slots == {2}
    assert window._palette_from_selection_action.isEnabled()

    # Scrolling away hides the highlight but keeps the selection; scrolling back
    # restores it.
    window._nav_rows(1)
    assert window._selected_tile == 2
    assert not window._canvas._selected_slots
    window._nav_rows(-1)
    assert window._canvas._selected_slots == {2}

    # Switching to another file leaves the selection behind (a tile index from
    # one file means nothing in another); the fresh entry starts unselected.
    window._load_pixel(str(_make_snes_file(tmp_path)))
    assert window._selected_tile is None
    assert not window._palette_from_selection_action.isEnabled()
    # Re-opening the first file is a no-op-in-place activation of its entry —
    # and switching back restores its remembered selection.
    window._open_pixel()
    assert window._selected_tile == 2


def test_click_on_blank_padding_is_ignored(qtbot, tmp_path, monkeypatch) -> None:
    # 8-tile file in a 32-slot window: slot 10 is padding past the file's end.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=8)
    window._columns.setValue(16)
    window._rows.setValue(2)
    window._on_slots_selected(10, 10)
    assert window._selected_tile is None


# -- clipboard: copy / cut / paste -----------------------------------------
def _open_pixels(qtbot, tmp_path, data: bytes | None = None):
    px = tmp_path / "clip.4bpp.sfc"
    px.write_bytes(data or bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    return window


def test_switching_pixel_format_keeps_unsaved_edits(qtbot, tmp_path) -> None:
    """A format switch reinterprets the bytes in memory, it doesn't re-read them.

    The edits only exist in that buffer, so re-running the pathway would put the
    file's own bytes back over them - a silent loss with nothing to undo.
    """
    window = _open_pixels(qtbot, tmp_path)
    window._select_tiles(0, 0)
    window._clear_selection_contents()
    edited = bytes(window._doc.pixel_data)
    assert edited != (tmp_path / "clip.4bpp.sfc").read_bytes()  # unsaved: memory only

    combo = window._pixel_preset
    other = next(p for p in _combo_ids(combo) if p != window._pixel_preset_id())
    combo.setCurrentIndex(combo.findData(other))

    assert bytes(window._doc.pixel_data) == edited
    window._undo_stack.undo()  # back to the original format, still edited
    assert bytes(window._doc.pixel_data) == edited


def test_the_tile_clipboard_round_trip(qtbot, tmp_path) -> None:
    """Cut/copy/paste/clear at tile granularity, on one 8-tile file.

    One test rather than five because these are one mechanism seen from five
    sides - every one of them is "which bytes moved where, and what is selected
    afterwards" - and running them in sequence additionally proves they compose:
    what a cut put on the clipboard is what a later paste lays down.
    """
    window = _open_pixels(qtbot, tmp_path)  # 8 tiles
    original = window._doc.pixel_data
    size = len(original)

    # The actions arm off the selection: nothing selected, nothing to copy, and
    # Paste stays dead until something is on the clipboard.
    assert not window._copy_action.isEnabled()
    window._select_tiles(0, 0)
    assert window._copy_action.isEnabled()
    assert window._cut_action.isEnabled()
    assert window._copy_selection()
    assert window._paste_action.isEnabled()

    # Paste onto tile 3: indices move verbatim within one format, the paste
    # selects what it landed on (so the next one stamps forward), and undo is exact.
    window._select_tiles(3, 3)
    window._paste()
    assert window._doc.pixel_data[96:128] == original[:32]
    assert window._doc.pixel_data[:96] == original[:96]
    assert window._workspace.current.pixel_dirty
    assert (window._selected_tile, window._selected_last) == (3, 3)
    window._undo_stack.undo()
    assert window._doc.pixel_data == original

    # Clear blanks exactly the selected tiles and nothing beyond them.
    window._select_tiles(2, 3)
    window._clear_selection_contents()
    assert window._doc.pixel_data[64:128] == bytes(64)
    assert window._doc.pixel_data[128:160] != bytes(32)  # tile 4 untouched

    # Cut copies before it blanks - what it took is on the clipboard, so pasting
    # it elsewhere restores those exact bytes.
    window._select_tiles(1, 1)
    window._cut_selection()
    assert window._doc.pixel_data[32:64] == bytes(32)
    window._select_tiles(5, 5)
    window._paste()
    assert window._doc.pixel_data[160:192] == original[32:64]

    # A paste that would run past the end is clipped, never extending the file.
    window._select_tiles(0, 3)  # four tiles …
    assert window._copy_selection()
    window._select_tiles(6, 6)  # … onto the last two: two must be dropped
    window._paste()
    assert len(window._doc.pixel_data) == size
    assert window._selected_last == 7


def test_paste_of_an_external_image_matches_the_active_palette(qtbot, tmp_path) -> None:
    from PySide6.QtGui import QGuiApplication, QImage

    from celpix.pipeline import pipeline

    window = _open_pixels(qtbot, tmp_path)
    # An image straight from another program: nothing but pixels on the
    # clipboard, painted in a color the palette holds exactly.
    color = window._doc.palette.color(5)
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(color)
    QGuiApplication.clipboard().setImage(image)

    window._select_tiles(1, 1)
    window._paste()
    tile = pipeline.decode_tiles(window._doc, window._registry, 1, 1)[0]
    assert set(tile.data) == {5}


def test_paste_into_a_narrower_format_refits_by_color(qtbot, tmp_path) -> None:
    from celpix.pipeline import pipeline

    window = _open_pixels(qtbot, tmp_path)
    window._select_tiles(0, 0)
    source = pipeline.decode_tiles(window._doc, window._registry, 0, 1)[0]
    assert max(source.data) > 3  # the 4bpp tile really does use high indices
    assert window._copy_selection()

    # 2bpp can only reference four colors; the copied indices no longer fit, so
    # the paste re-matches them through the palette instead of writing garbage.
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.snes-2bpp")
    )
    window._select_tiles(1, 1)
    window._paste()
    pasted = pipeline.decode_tiles(window._doc, window._registry, 1, 1)[0]
    assert max(pasted.data) <= 3


def _rect_shape(window, tmp_path) -> None:
    """Switch the Shape picker to Rectangle.

    QSettings is redirected to a throwaway INI first: the switch persists the
    choice app-wide, so neither the developer's real config nor a later test in
    this process may inherit it.
    """
    from celpix.ui.main_window.selection import SelectionShape

    _fresh_settings(tmp_path)
    combo = window._selection_shape
    combo.setCurrentIndex(combo.findData(SelectionShape.RECT))


def test_rectangle_drag_selects_a_block_and_shape_switch_collapses(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(8)

    # A linear drag first: switching shape must not reinterpret it as a block.
    window._on_slots_selected(0, 9)
    assert window._selection_tiles() == list(range(10))
    assert not window._canvas._selection_as_rect
    # A run filling whole rows fills a rectangle, but was picked as a run and
    # must keep outlining row by row.
    window._on_slots_selected(0, 15)
    assert not window._canvas._selection_as_rect
    _rect_shape(window, tmp_path)
    assert (window._selected_tile, window._selected_last) == (0, 0)

    # Slots 0..9 now read as the corners of a 2x2 cell block, so the selection
    # is two runs of two tiles a row apart — not the ten tiles between them.
    window._on_slots_selected(0, 9)
    assert window._rect_size == (2, 2)
    assert window._selection_tiles() == [0, 1, 8, 9]
    assert window._canvas._selected_slots == {0, 1, 8, 9}
    assert window._canvas._selection_as_rect


def test_select_all_makes_a_rectangle_in_rectangle_shape(
    qtbot, tmp_path, monkeypatch
) -> None:
    """Select All takes the window in the shape the picker is on. A run instead
    would be a selection none of the rectangle-only edits (a block transform, a
    cell paste, a stamp) could act on - and the user asked for a rectangle."""
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(4)
    _rect_shape(window, tmp_path)

    window._select_all()
    assert window._rect_size == (8, 4)  # the whole window, as one block
    assert window._selection_tiles() == list(range(32))
    assert window._canvas._selection_as_rect

    # And under a 2x2 block arrangement, where a row of blocks is two canvas rows
    # deep: the rectangle is still the window, only its tiles arrive block by
    # block rather than row by row.
    window._block_cols.setValue(2)
    window._block_rows.setValue(2)
    window._select_all()
    assert window._rect_size == (8, 4)
    assert sorted(window._selection_tiles()) == list(range(32))

    # Scoped to the window, not the file: the rectangle starts where the view is.
    window._block_cols.setValue(1)
    window._block_rows.setValue(1)
    window._nav_rows(6)
    assert window._offset == 32
    window._select_all()
    assert window._rect_size == (8, 4)
    assert window._selection_tiles() == list(range(32, 64))


def test_rectangle_collapses_when_the_view_reshuffles_its_tiles(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(8)
    _rect_shape(window, tmp_path)
    window._on_slots_selected(0, 9)  # tiles 0, 1, 8, 9

    # Half the columns: those same four cells now sit over tiles 0, 1, 4, 5, so
    # the rectangle no longer covers what was selected and drops to its corner.
    window._columns.setValue(4)
    assert window._rect_size is None
    assert (window._selected_tile, window._selected_last) == (0, 0)
    assert window._canvas._selected_slots == {0}


def test_new_slice_from_selection_refuses_a_disjoint_rectangle(
    qtbot, tmp_path, monkeypatch, captured_alerts
) -> None:
    from celpix.ui.slice_dialog import SliceDialog

    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(8)
    _rect_shape(window, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        SliceDialog,
        "get_slice",
        staticmethod(lambda *_args, **kwargs: captured.update(kwargs)),
    )

    window._on_slots_selected(0, 9)  # 2x2 — its rows sit apart in the file
    window._new_slice_from_selection()
    assert not captured
    assert "continuous run" in captured_alerts[-1][1]

    # Full width: the rows are back-to-back, so it is one run and is offered.
    window._on_slots_selected(0, 15)
    window._new_slice_from_selection()
    assert (captured["offset"], captured["length"]) == (0, 16 * 32)


def test_rectangle_copy_paste_and_clear_touch_only_their_cells(
    qtbot, tmp_path, monkeypatch
) -> None:
    from celpix.ui import clipboard

    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(8)
    _rect_shape(window, tmp_path)
    original = window._doc.pixel_data
    tb = window._doc.bytes_per_tile

    def tile_bytes(data, index):
        return data[index * tb : (index + 1) * tb]

    window._on_slots_selected(0, 9)  # the 2x2 block of tiles 0, 1, 8, 9
    assert window._copy_selection()
    payload = clipboard.take_payload()
    assert (payload.count, payload.columns) == (4, 2)

    # Anchored on tile 4 the copy lands as a 2x2 block, not a run of four.
    window._on_slots_selected(4, 4)
    window._paste()
    for src, dst in ((0, 4), (1, 5), (8, 12), (9, 13)):
        assert tile_bytes(window._doc.pixel_data, dst) == tile_bytes(original, src)
    assert tile_bytes(window._doc.pixel_data, 6) == tile_bytes(original, 6)

    # Clear blanks the rectangle's own cells and leaves the gap between rows.
    window._on_slots_selected(0, 9)
    window._clear_selection_contents()
    for blanked in (0, 1, 8, 9):
        assert tile_bytes(window._doc.pixel_data, blanked) == bytes(tb)
    assert tile_bytes(window._doc.pixel_data, 2) == tile_bytes(original, 2)


# -- image import ----------------------------------------------------------
def _fill_png(path, window, index: int, width: int, height: int) -> str:
    """Write a solid PNG in the color the view's palette holds at ``index``."""
    from PySide6.QtGui import QImage

    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(window._doc.palette.color(index))
    assert image.save(str(path), "PNG")
    return str(path)


def test_paste_of_an_odd_sized_image_keeps_the_pixels_it_never_covered(
    qtbot, tmp_path
) -> None:
    from PySide6.QtGui import QGuiApplication, QImage

    from celpix.pipeline import pipeline

    window = _open_pixels(qtbot, tmp_path)
    before = pipeline.decode_tiles(window._doc, window._registry, 0, 2)
    # A tile and a half wide: the second tile's right half is not the image's to
    # write, and padding it black would erase art the source never spoke for.
    image = QImage(12, 8, QImage.Format.Format_ARGB32)
    image.fill(window._doc.palette.color(5))
    QGuiApplication.clipboard().setImage(image)

    window._select_tiles(0, 0)
    window._paste()

    after = pipeline.decode_tiles(window._doc, window._registry, 0, 2)
    assert set(after[0].data) == {5}
    for y in range(8):
        for x in range(4):
            assert after[1].get(x, y) == 5
        for x in range(4, 8):
            assert after[1].get(x, y) == before[1].get(x, y)


def test_image_paste_into_a_block_view_lands_as_the_picture_it_shows(
    qtbot, tmp_path, monkeypatch
) -> None:
    """A pasted image is pixels, not a run of tiles: it has to end up looking the
    way it looked, so it is the *arrangement* that decides which storage tile each
    cell of the picture goes to.

    The image is a 4×2 grid of solid cells numbered 1..8 in reading order, pasted
    into a view of 8 columns of 2×2 row-major blocks. There storage tiles 0..3 are
    block 0's cells (0,0),(1,0),(0,1),(1,1) and tiles 4..7 are block 1's, so a
    faithful paste stores the picture's cells 1,2 / 5,6 then 3,4 / 7,8. Plain
    row-major storage (1..8) is the scrambled result — bytes in reading order that
    the block layout then displays as a shuffle.

    The Shape picker deliberately stays on Linear: an image is a picture whatever
    shape a *tile* paste would have taken, so it stamps as a block regardless.
    """
    from PySide6.QtGui import QGuiApplication, QImage

    from celpix.pipeline import pipeline
    from celpix.ui.main_window.selection import SelectionShape

    # A fresh window reads the persisted shape preference; isolate it so "the
    # default is Linear" is true of the run and not of the developer's config.
    _fresh_settings(tmp_path)
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(8)
    window._block_cols.setValue(2)
    window._block_rows.setValue(2)
    window._block_order.setCurrentIndex(window._block_order.findData("row"))
    assert window._selection_shape.currentData() is SelectionShape.LINEAR

    image = QImage(32, 16, QImage.Format.Format_ARGB32)
    for cy in range(2):
        for cx in range(4):
            color = window._doc.palette.color(1 + cy * 4 + cx)
            for y in range(8):
                for x in range(8):
                    image.setPixel(cx * 8 + x, cy * 8 + y, color)
    QGuiApplication.clipboard().setImage(image)

    window._select_tiles(0, 0)
    window._paste()

    tiles = pipeline.decode_tiles(window._doc, window._registry, 0, 8)
    assert [set(tile.data) for tile in tiles] == [
        {index} for index in (1, 2, 5, 6, 3, 4, 7, 8)
    ]


def test_import_png_from_the_files_list_lands_at_the_start_of_the_file(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QFileDialog

    from celpix.pipeline import pipeline

    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(4)
    original = window._doc.pixel_data
    png = _fill_png(tmp_path / "sprite.png", window, 3, 16, 16)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (png, ""))
    )

    window._nav_rows(2)  # the view is elsewhere in the file …
    assert window._offset > 0
    window._import_png_into(window._workspace.current)

    # … but the import goes to tile 0 and brings the view back to see it. The
    # 2×2-tile image lands as a block, not as a run of four tiles.
    assert window._offset == 0
    tiles = pipeline.decode_tiles(window._doc, window._registry, 0, 10)
    for index in (0, 1, 8, 9):
        assert set(tiles[index].data) == {3}
    tb = window._doc.bytes_per_tile
    assert window._doc.pixel_data[2 * tb : 3 * tb] == original[2 * tb : 3 * tb]

    window._undo_stack.undo()
    assert window._doc.pixel_data == original


def test_import_png_from_the_canvas_lands_on_the_selection(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QFileDialog

    from celpix.pipeline import pipeline

    window = _open_pixels(qtbot, tmp_path)
    before = pipeline.decode_tiles(window._doc, window._registry, 5, 2)
    png = _fill_png(tmp_path / "half.png", window, 5, 12, 8)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (png, ""))
    )

    window._select_tiles(5, 5)
    window._import_png_here()

    after = pipeline.decode_tiles(window._doc, window._registry, 5, 2)
    assert set(after[0].data) == {5}
    # Same partial-tile rule as paste: the half the image didn't reach is the
    # file's own pixels, not padding.
    for y in range(8):
        assert after[1].get(0, y) == 5
        assert after[1].get(7, y) == before[1].get(7, y)


def test_dropped_png_imports_onto_the_selection_instead_of_opening(qtbot, tmp_path):
    """A PNG is picture data, not a binary to read graphics out of: dropping one
    imports it where the canvas menu would, and never joins the files list."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QDropEvent

    from celpix.pipeline import pipeline

    window = _open_pixels(qtbot, tmp_path)
    png = _fill_png(tmp_path / "sprite.png", window, 5, 8, 8)
    window._select_tiles(5, 5)

    mime = _drag_payload(png)  # must outlive the event, or Qt reads freed memory
    event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(event)

    assert len(window._workspace.entries) == 1  # the PNG did not become an entry
    imported = pipeline.decode_tiles(window._doc, window._registry, 5, 1)
    assert set(imported[0].data) == {5}


def test_every_stamp_snaps_an_offscreen_selection_into_view(
    qtbot, tmp_path, monkeypatch
):
    """A stamp lands on the selected tile - but if that selection has scrolled
    off-screen the anchor resolves off the grid and nothing would land.

    All three stamping entry points (paste, Import from PNG, a dropped PNG) go
    through one guard, ``_paste_anchor``, which first pulls the selection onto the
    visible top-left tile. They are checked together because the risk is not the
    guard's arithmetic - it is an entry point reaching for the raw anchor instead
    and silently writing nothing, which only shows up per path.
    """
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QDropEvent, QGuiApplication, QImage
    from PySide6.QtWidgets import QFileDialog

    from celpix.pipeline import pipeline

    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(2)  # a 16-tile window
    png = _fill_png(tmp_path / "sprite.png", window, 5, 8, 8)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (png, ""))
    )
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(window._doc.palette.color(5))
    QGuiApplication.clipboard().setImage(image)

    def dropped_png() -> None:
        mime = _drag_payload(png)  # must outlive the event, or Qt reads freed memory
        window.dropEvent(
            QDropEvent(
                QPointF(10, 10),
                Qt.DropAction.CopyAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )

    for landing, stamp in enumerate(
        (window._paste, window._import_png_here, dropped_png)
    ):
        # Select a tile, then scroll it off-screen. Each pass stamps onto a
        # different visible page, so no two of them can pass on the last one's
        # bytes: page 2 (tile 16), then page 3, then page 4.
        offscreen = 2
        window._select_tiles(offscreen, offscreen)
        visible = 16 * (landing + 1)
        window._set_offset(visible)
        assert window._selection_offscreen()
        before = pipeline.decode_tiles(window._doc, window._registry, offscreen, 1)

        stamp()

        # The selection moved onto the visible top-left tile and the image landed
        # there - not at the off-screen tile, which is left untouched.
        assert window._selected_tile == visible
        landed = pipeline.decode_tiles(window._doc, window._registry, visible, 1)
        assert set(landed[0].data) == {5}
        untouched = pipeline.decode_tiles(window._doc, window._registry, offscreen, 1)
        assert untouched[0].data == before[0].data


def test_tile_and_block_transforms(qtbot, tmp_path) -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QDropEvent

    from celpix.core import transform as _tf
    from celpix.pipeline import pipeline
    from celpix.ui.main_window.selection import SelectionShape
    from celpix.ui.widgets import select_combo_data

    px = _make_snes_file(tmp_path)  # 8 tiles, 8×8 4bpp (square tiles)
    window = MainWindow()
    qtbot.addWidget(window)
    mime = _drag_payload(px)
    event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(event)
    window._columns.setValue(4)  # a 4×2 window addresses all 8 tiles
    window._rows.setValue(2)

    tile, block = window._tile_group, window._block_group

    def decode(first):
        return pipeline.decode_tiles(window._doc, window._registry, first, 1)[0].data

    # -- Tile group: any selection; a rotate needs square tiles (these are 8×8) --
    window._select_tiles(0, 0)  # a lone linear tile
    assert tile.flip_h.isEnabled() and tile.rotate_cw.isEnabled()
    # A single tile enables block transforms too, in any shape - it expands to its
    # arrangement block (here 1×1, so the block is just that one tile).
    assert block.flip_h.isEnabled() and block.rotate_cw.isEnabled()

    # Index-preserving: the flip mirrors palette *indices* exactly - it must never
    # route through ARGB and quantize back (which could remap indices sharing a
    # color). The decoded tile stays an IndexGrid, not an ArgbGrid.
    pre = pipeline.decode_tiles(window._doc, window._registry, 0, 1)[0]
    assert isinstance(pre, IndexGrid)
    tile.flip_h.trigger()
    assert decode(0) == _tf.flip_horizontal(pre).data
    window._undo_stack.undo()

    # -- A 2×2 Rectangle selection, made the way the canvas makes one --
    select_combo_data(window._selection_shape, SelectionShape.RECT)
    window._on_slots_selected(0, 5)  # cells (0,0)..(1,1) → tiles 0,1,4,5
    assert window._rect_tiles == (0, 1, 4, 5)
    assert block.flip_h.isEnabled() and block.rotate_cw.isEnabled()  # square block

    t0, t1 = decode(0), decode(1)
    # Tile flip over a rectangle leaves positions alone: each tile flips in place.
    tile.flip_h.trigger()
    assert decode(0) == _tf.flip_horizontal(pre.__class__(8, 8, t0)).data
    window._undo_stack.undo()

    # Block flip swaps the two columns *and* flips each tile: tile 0 ← flip(tile 1).
    block.flip_h.trigger()
    assert decode(0) == _tf.flip_horizontal(pre.__class__(8, 8, t1)).data
    assert decode(1) == _tf.flip_horizontal(pre.__class__(8, 8, t0)).data
    window._undo_stack.undo()  # one step restores the whole block
    assert decode(0) == t0

    # -- A non-square rectangle: block rotate off, block flip on; tile rotate on --
    window._on_slots_selected(0, 1)  # a 2×1 rectangle
    assert window._rect_size == (2, 1)
    assert block.flip_h.isEnabled()
    assert not block.rotate_cw.isEnabled() and not block.rotate_ccw.isEnabled()
    assert tile.rotate_cw.isEnabled()

    # -- Nothing selected: every transform is off --
    window._clear_selection()
    for action in (*tile.flips, *tile.rotates, *block.flips, *block.rotates):
        assert not action.isEnabled()


def test_block_transform_of_single_tile_uses_arrangement_block(qtbot, tmp_path) -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QDropEvent

    from celpix.core import transform as _tf
    from celpix.pipeline import pipeline
    from celpix.ui.main_window.selection import SelectionShape
    from celpix.ui.widgets import select_combo_data

    px = _make_snes_file(tmp_path)  # 8 tiles, 8×8 4bpp
    window = MainWindow()
    qtbot.addWidget(window)
    mime = _drag_payload(px)
    window.dropEvent(
        QDropEvent(
            QPointF(10, 10),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )
    window._columns.setValue(4)
    window._rows.setValue(2)
    # A 2×2 tile-block arrangement: the first block groups tiles 0,1,2,3.
    window._block_cols.setValue(2)
    window._block_rows.setValue(2)

    block = window._block_group

    def grid(first):
        return pipeline.decode_tiles(window._doc, window._registry, first, 1)[0]

    orig = [grid(i) for i in range(4)]

    # Select a single tile in the block; in Rectangle mode that is a 1×1 selection.
    select_combo_data(window._selection_shape, SelectionShape.RECT)
    window._on_slots_selected(0, 0)
    assert len(window._selection_tiles()) == 1
    # A square arrangement block means block rotate is available off one tile.
    assert block.flip_h.isEnabled() and block.rotate_cw.isEnabled()

    # Block flip-H treats the whole 2×2 block as selected: the columns swap and
    # every tile flips. tile0 ← flip(tile1), tile1 ← flip(tile0), tile2 ← flip(tile3).
    block.flip_h.trigger()
    assert grid(0).data == _tf.flip_horizontal(orig[1]).data
    assert grid(1).data == _tf.flip_horizontal(orig[0]).data
    assert grid(2).data == _tf.flip_horizontal(orig[3]).data
    assert grid(3).data == _tf.flip_horizontal(orig[2]).data
    window._undo_stack.undo()  # one step restores all four tiles
    assert grid(0).data == orig[0].data and grid(3).data == orig[3].data

    # The same works in Linear mode: a lone tile still expands to its 2×2 block.
    select_combo_data(window._selection_shape, SelectionShape.LINEAR)
    window._select_tiles(0, 0)
    assert block.flip_h.isEnabled() and block.rotate_cw.isEnabled()
    block.flip_h.trigger()
    assert grid(0).data == _tf.flip_horizontal(orig[1]).data
    assert grid(1).data == _tf.flip_horizontal(orig[0]).data
    window._undo_stack.undo()
    assert grid(0).data == orig[0].data

    # A non-square arrangement block (1×2) can't rotate as a block off one tile,
    # but a block flip is still fine.
    window._block_cols.setValue(1)
    window._block_rows.setValue(2)
    window._on_slots_selected(0, 0)
    assert block.flip_h.isEnabled()
    assert not block.rotate_cw.isEnabled()


def test_a_solid_block_of_cells_gets_one_outline() -> None:
    from celpix.ui.canvas import Canvas

    # A rectangle selection is one shape on screen and must read as one box.
    assert Canvas._solid_rect({0: [2, 3], 1: [2, 3]}) == (2, 0, 2, 2)
    assert Canvas._solid_rect({4: [0, 1, 2]}) == (0, 4, 3, 1)
    # Anything ragged has no single box: rows that don't line up, a hole in a
    # row, or a skipped row all fall back to per-row outlines.
    assert Canvas._solid_rect({0: [1, 2], 1: [0, 1, 2]}) is None
    assert Canvas._solid_rect({0: [0, 2]}) is None
    assert Canvas._solid_rect({0: [0], 2: [0]}) is None
    assert Canvas._solid_rect({}) is None
