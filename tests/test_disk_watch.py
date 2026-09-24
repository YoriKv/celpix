"""A file rewritten by another program while it is open: the offer to reload,
and the reload keeping what was edited here (``docs/design/disk-changes.md``).

The look the watcher's rest timer ends in is driven by hand
(``_check_disk_changes``), except in the one test that lets the watcher reach
it, and the app is made to look active, since offscreen nothing ever is."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from celpix.ui.main_window import MainWindow
from celpix.ui.tools import EditMode, Tool
from uihelpers import _make_snes_file


def _rewrite(path, data: bytes) -> None:
    """What another program does: new contents, and a clock that has moved on
    even where the filesystem's is coarse."""
    path.write_bytes(data)
    os.utime(path, ns=(0, os.stat(path).st_mtime_ns + 2_000_000_000))


def _window(qtbot, tmp_path, monkeypatch):
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    monkeypatch.setattr(QApplication, "activeWindow", staticmethod(lambda: window))
    return window, px


def _paint(window, x: int, y: int, index: int) -> None:
    window._set_edit_mode(EditMode.PIXEL)
    window._on_tool_selected(Tool.PENCIL)
    window._palette_row.setValue(0)
    window._palette_panel.select_index(index)
    window._on_pixel_pressed(x, y, Qt.MouseButton.LeftButton)
    window._on_pixel_released(x, y)


def test_a_change_by_another_program_is_offered_once(
    qtbot, tmp_path, monkeypatch, disk_reload_answer
) -> None:
    window, px = _window(qtbot, tmp_path, monkeypatch)
    entry = window._workspace.current
    old = bytes(entry.doc.pixel_data)
    window._check_disk_changes()
    assert disk_reload_answer.asked == []  # nothing happened yet

    _rewrite(px, old)  # touched, but the same bytes: not a change
    window._check_disk_changes()
    assert disk_reload_answer.asked == []

    _rewrite(px, bytes(reversed(old)))
    window._check_disk_changes()
    assert disk_reload_answer.asked == [([str(px)], [])]
    # Declined: the bytes on screen stand, and the same change is not asked
    # about again at the next poll — but the menu row still has it to do.
    assert entry.doc.pixel_data == old
    window._check_disk_changes()
    assert len(disk_reload_answer.asked) == 1
    window._reload_action.trigger()
    assert entry.doc.pixel_data == bytes(reversed(old))

    # Accepted: the document is the file as it is now, and it is on screen.
    disk_reload_answer.reload = True
    _rewrite(px, old[::-1][:100] + bytes(len(old) - 100))
    window._check_disk_changes()
    assert entry.doc.pixel_data == px.read_bytes()
    assert window._doc is entry.doc
    assert "Reloaded" in window.statusBar().currentMessage()


def test_the_watcher_follows_the_open_files_and_reaches_the_prompt(
    qtbot, tmp_path, monkeypatch, disk_reload_answer
) -> None:
    """The whole path for once: the watcher's signal, the rest timer, the look
    at the bytes, the question — and a closed file is no longer watched."""
    window, px = _window(qtbot, tmp_path, monkeypatch)
    assert str(px) in window._fs_watcher.files()
    _rewrite(px, bytes(reversed(px.read_bytes())))
    qtbot.waitUntil(lambda: bool(disk_reload_answer.asked), timeout=5000)
    assert disk_reload_answer.asked == [([str(px)], [])]

    window._workspace.close(window._workspace.current)
    assert window._fs_watcher.files() == []


def test_the_menu_reloads_the_current_file_without_asking(
    qtbot, tmp_path, monkeypatch, disk_reload_answer
) -> None:
    """File ▸ Reload From Disk is the poll's reload on demand: no prompt, the
    pending offer for that file withdrawn, and a slice reloads its file."""
    window, px = _window(qtbot, tmp_path, monkeypatch)
    parent = window._workspace.current
    assert window._reload_action.isEnabled()
    piece = window._workspace.add_slice(str(px), "piece", 64, 64)
    window._activate_entry(piece)
    assert window._reload_action.isEnabled()
    # A file holding what was read is left alone rather than re-derived.
    held = piece.doc
    window._reload_action.trigger()
    assert piece.doc is held and "up to date" in window.statusBar().currentMessage()

    new = bytes(reversed(px.read_bytes()))
    _rewrite(px, new)
    window._disk_state.changed()  # seen by the poll, but not yet offered
    window._pending_disk_changes = [str(px)]
    window._reload_action.trigger()

    assert disk_reload_answer.asked == []
    assert window._pending_disk_changes == []
    assert piece.doc.pixel_data == new[64:128] and window._doc is piece.doc
    assert parent.doc is None or parent.doc.pixel_data == new
    window._check_disk_changes()  # the reload took the fingerprint with it
    assert disk_reload_answer.asked == []


def test_a_reload_keeps_unsaved_edits_over_the_new_contents(
    qtbot, tmp_path, monkeypatch, disk_reload_answer
) -> None:
    """The three-way merge, end to end: a pixel painted here survives, the other
    program's bytes arrive, the entry stays unsaved, and the next Write puts the
    merged region on disk."""
    window, px = _window(qtbot, tmp_path, monkeypatch)
    entry = window._workspace.current
    base = px.read_bytes()
    _paint(window, 2, 3, 5)
    assert entry.pixel_dirty
    painted = bytes(entry.doc.pixel_data)
    ours = [i for i in range(len(base)) if painted[i] != base[i]]
    assert ours and max(ours) < 32  # the stroke landed in tile 0

    theirs = bytearray(base)
    theirs[160:192] = b"\xee" * 32  # tile 5, from outside
    _rewrite(px, bytes(theirs))
    disk_reload_answer.reload = True
    window._check_disk_changes()

    assert disk_reload_answer.asked[-1][1] == [entry.name]  # named as unsaved
    merged = entry.doc.pixel_data
    assert merged[160:192] == b"\xee" * 32
    assert all(merged[i] == painted[i] for i in ours)
    assert entry.pixel_dirty
    assert "Kept" in window.statusBar().currentMessage()

    window._write_current()
    assert px.read_bytes() == merged and not entry.pixel_dirty
    # Our own write is not offered back as someone else's change.
    window._check_disk_changes()
    assert len(disk_reload_answer.asked) == 1


def test_a_slice_edit_rides_its_parent_through_the_reload(
    qtbot, tmp_path, monkeypatch, disk_reload_answer
) -> None:
    """One region, one authority: the slice's unsaved bytes are folded into the
    file, the file is merged once, and the slice is re-derived from it."""
    window, px = _window(qtbot, tmp_path, monkeypatch)
    parent = window._workspace.current
    base = px.read_bytes()
    piece = window._workspace.add_slice(str(px), "piece", 64, 64)
    window._activate_entry(piece)
    _paint(window, 1, 1, 7)
    assert piece.pixel_dirty and parent.pixel_dirty
    painted = bytes(piece.doc.pixel_data)

    theirs = bytearray(base)
    theirs[0:8] = b"\x11" * 8  # outside the slice
    _rewrite(px, bytes(theirs))
    disk_reload_answer.reload = True
    window._check_disk_changes()

    assert set(disk_reload_answer.asked[-1][1]) == {parent.name, piece.name}
    assert parent.doc is not None
    assert parent.doc.pixel_data[0:8] == b"\x11" * 8
    assert parent.doc.pixel_data[64:128] == painted  # the slice's edit, kept
    # The slice on screen shows the same bytes, re-read from the merged file.
    assert window._doc is piece.doc and piece.doc.pixel_data == painted
    assert piece.pixel_dirty and parent.pixel_dirty


def test_a_reload_keeps_unsaved_colours_of_a_palette_file(
    qtbot, tmp_path, monkeypatch, disk_reload_answer
) -> None:
    """A palette carries its edited entries the way its save splices them: the
    file's new colours, with the edited one back over them."""
    from celpix.project.workspace import EntryKind

    pal = tmp_path / "shared.pal"
    pal.write_bytes(bytes(32))
    window, _px = _window(qtbot, tmp_path, monkeypatch)
    window._add_palette_file(str(pal))
    entry = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    window._use_palette_entry(entry)
    graphic = window._workspace.current

    window._palette_panel._select(0)
    window._on_color_changed(0xFFFFFFFF)
    assert entry.palette_dirty

    theirs = bytearray(32)
    theirs[2:4] = b"\xff\x7f"  # entry 1 became white, from outside
    _rewrite(pal, bytes(theirs))
    disk_reload_answer.reload = True
    window._check_disk_changes()

    assert disk_reload_answer.asked[-1][1] == [entry.name]
    colors = entry.doc.palette.colors
    assert colors[0] == 0xFFFFFFFF  # ours, kept
    assert colors[1] == 0xFFFFFFFF  # theirs, arrived
    assert entry.palette_dirty and entry.doc.palette_edits == {0}
    assert graphic.doc.palette is entry.doc.palette  # re-mirrored
    assert Path(pal).read_bytes() == bytes(theirs)


def test_a_map_keeps_its_unsaved_cells_over_a_rewritten_screen(
    qtbot, tmp_path, monkeypatch, disk_reload_answer
) -> None:
    """A tilemap's edits are cell bytes, merged the same way and decoded back
    into cells, so a flip made here and a cell changed outside both show."""
    from celpix.core.tilemap import Cell
    from celpix.ui.main_window.transform import OP_FLIP_H
    from uihelpers import _bound_tilemap, _scr_file

    window, entry = _bound_tilemap(qtbot, tmp_path, [Cell(index=1), Cell(index=2)])
    monkeypatch.setattr(QApplication, "activeWindow", staticmethod(lambda: window))
    window._set_linear_selection(0, 0)
    window._transform_tiles(OP_FLIP_H)
    assert entry.pixel_dirty and window._doc.cells[0] == Cell(index=1, flip_h=True)

    _scr_file(tmp_path, [Cell(index=1), Cell(index=9)])  # cell 1 changed outside
    os.utime(entry.path, ns=(0, os.stat(entry.path).st_mtime_ns + 2_000_000_000))
    disk_reload_answer.reload = True
    window._check_disk_changes()

    assert window._doc is entry.doc
    assert entry.doc.cells[0] == Cell(index=1, flip_h=True)  # ours
    assert entry.doc.cells[1] == Cell(index=9)  # theirs
    assert entry.pixel_dirty


def test_a_palette_file_on_screen_keeps_its_painted_swatch_over_a_reload(
    qtbot, tmp_path, monkeypatch, disk_reload_answer
) -> None:
    """The palette file itself on screen, with a swatch painted: the reload
    merges the painted colour word over the file's new ones, the dock's colours
    are decoded from the merged bytes, and the view comes back as it was left."""
    from celpix.project.workspace import EntryKind

    pal = tmp_path / "shared.pal"
    pal.write_bytes(bytes(64))  # 32 black BGR555 colours
    window, _px = _window(qtbot, tmp_path, monkeypatch)
    window._add_palette_file(str(pal), preset_id="preset.palette.bgr555")
    entry = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    window._activate_entry(entry)
    assert window._doc is entry.doc
    window._columns.setValue(4)
    assert entry.doc.view.columns == 4
    tiles = window._decode_run(3, 1)
    swatch = type(tiles[0])(tiles[0].width, tiles[0].height, bytes(tiles[0].data))
    for y in range(swatch.height):
        for x in range(swatch.width):
            swatch.set(x, y, 0xFF0000FF)
    window._apply_tile_edit(3, [swatch], "paint")
    assert entry.pixel_dirty

    theirs = bytearray(64)
    theirs[2:4] = b"\xff\x7f"  # colour 1 became white, from outside
    _rewrite(pal, bytes(theirs))
    held = entry.doc
    window._reload_action.trigger()

    assert entry.doc is not held  # re-read, not left standing
    assert window._doc is entry.doc and entry.pixel_dirty
    colors = entry.doc.palette.colors
    assert colors[1] == 0xFFFFFFFF  # theirs, arrived
    assert colors[3] == 0xFF0000FF  # ours, kept
    assert window._palette_panel._colors == colors
    assert entry.doc.view.columns == 4


def test_a_disk_reload_lifts_a_failed_entrys_mark_and_tries_it_again(
    qtbot, tmp_path, monkeypatch, captured_alerts
) -> None:
    """A failed entry has no document to re-read, but its file changing is the
    one outside event that can have fixed the failure - so a reload of that
    file lifts the mark, and the entry on screen is tried at once."""
    from celpix.core.errors import Pathway, PipelineError, Stage
    from celpix.pipeline import pipeline

    window, px = _window(qtbot, tmp_path, monkeypatch)
    entry = window._workspace.current
    other = tmp_path / "other.4bpp.sfc"
    other.write_bytes(bytes(32 * 8))
    window._load_pixel(str(other))  # so the failed entry can be re-activated
    window._workspace.drop_document(entry)

    real = pipeline.load_pixel_data

    def refusing(cfg, reg, *args, **kwargs):
        if str(px) in cfg.source.paths:
            raise PipelineError(Stage.CONTAINER, Pathway.PIXEL, "boom", "read")
        return real(cfg, reg, *args, **kwargs)

    monkeypatch.setattr(pipeline, "load_pixel_data", refusing)
    window._activate_entry(entry)
    assert entry.load_failure is not None and window._doc is None

    monkeypatch.setattr(pipeline, "load_pixel_data", real)  # the fault is gone
    _rewrite(px, bytes(reversed(px.read_bytes())))
    window._reload_from_disk([str(px)])

    assert entry.load_failure is None and entry.doc is not None
    assert window._doc is entry.doc and window._write_action.isEnabled()
    assert len(captured_alerts) == 1  # the original failure, nothing since
