"""Where a palette comes from and what editing it touches:
the palette dock, its source modes, and the pinned palette regions."""

from __future__ import annotations

from pathlib import Path

from celpix.project.workspace import PaletteMode
from celpix.ui.main_window import MainWindow
from uihelpers import (
    _bound_screen,
    _bound_tilemap,
    _cgx_file,
    _combo_ids,
    _drag_payload,
    _fresh_settings,
    _make_snes_file,
    _map_file,
    _open_big,
    _pnl_file,
    _select_address_format,
)


def test_slice_offset_palette_reads_parent_file_absolute(qtbot, tmp_path) -> None:
    # BGR555 white at absolute offset 32 — *before* the slice, so a successful
    # read proves the offset is parent-file-absolute, not slice-relative.
    data = bytearray(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))
    data[32:34] = b"\xff\x7f"
    px = tmp_path / "p.4bpp.sfc"
    px.write_bytes(bytes(data))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    slice_entry = window._workspace.add_slice(str(px), "gfx", 64, 64)
    window._activate_entry(slice_entry)

    assert window._load_palette_at_offset(32)
    doc = window._doc
    assert doc.palette.colors[0] == 0xFFFFFFFF
    assert doc.palette_config.source.path == str(px)  # the parent file
    assert doc.palette_config.source.offset == 32
    # Offset palettes are edited in place: Write is armed, bounded to the
    # palette's own bytes in the parent file.
    assert doc.palette_config.write_enabled is True


def test_palette_export_writes_a_pal_and_registers_it(qtbot, tmp_path, monkeypatch):
    """An Offset palette lives buried in the pixel file; exporting is the only
    way it becomes a file of its own, and the export joins Palettes so it is
    re-applicable (and travels with the project) without a second gesture."""
    from PySide6.QtWidgets import QFileDialog

    from celpix.pipeline import pipeline
    from celpix.pipeline.pathway import PathwayConfig
    from celpix.plugins.base import FileRef
    from celpix.project.workspace import EntryKind

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    # Default is generated and File already *is* a .pal - neither can export.
    assert window._palette_mode == "default"
    assert not window._export_palette_action.isEnabled()

    assert window._load_palette_at_offset(32)
    assert window._export_palette_action.isEnabled()

    out = tmp_path / "gfx.pal"
    asked: list[str] = []

    def _save(_parent, _title, default, *a, **k):
        asked.append(default)
        return (str(out), "")

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(_save))
    # The palette is read as BGR555, so that is what the export writes: two
    # bytes an entry, not the three a plain RGB dump would take.
    assert window._palette_preset_id() == "preset.palette.bgr555"
    window._export_palette_file()
    # One pixel file yields as many palettes as it has offsets, so the offset -
    # not just the source file - is what makes the suggested name unique.
    assert Path(asked[0]).name == "s.4bpp_0x000020.pal"
    assert out.stat().st_size == 2 * len(window._doc.palette)

    # The file holds the whole palette on screen - every entry, not just the
    # ones an edit touched, as a save-back into an existing file would.
    reloaded = pipeline.load_palette(
        PathwayConfig(
            source=FileRef(str(out)),
            interpret_preset_id="preset.palette.bgr555",
        ),
        window._registry,
    )
    assert reloaded.palette.colors == window._doc.palette.colors
    palettes = [e for e in window._workspace.entries if e.kind is EntryKind.PALETTE]
    assert [e.name for e in palettes] == ["gfx.pal"]
    # Registered as what was written, so the double-click round-trips: applying
    # it decodes BGR555 and lands the very colors that were exported.
    assert palettes[0].palette_preset_id == "preset.palette.bgr555"

    # Exporting over an already-registered path re-stamps it: the entry has to
    # describe the bytes now on disk, not the file they replaced.
    palettes[0].palette_preset_id = "preset.palette.rgb888"
    window._export_palette_file()
    assert palettes[0].palette_preset_id == "preset.palette.bgr555"

    window._use_palette_entry(palettes[0])
    assert window._palette_preset_id() == "preset.palette.bgr555"
    assert window._doc.palette.colors == reloaded.palette.colors


def test_open_palette_applies_colors(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QFileDialog

    px = _make_snes_file(tmp_path)
    pl = tmp_path / "s.4bpp.sfc.pal"
    pl.write_bytes(bytes((i * 7 + 2) & 0xFF for i in range(2 * 16)))

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(px), ""))
    )
    window = MainWindow()
    qtbot.addWidget(window)
    window._open_pixel()

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(pl), ""))
    )
    window._open_palette()
    assert window._palette_mode.is_real
    assert len(window._doc.palette) == 16


def test_dropped_pal_becomes_a_palette_entry_applied_on_use(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QDropEvent

    from celpix.project.workspace import EntryKind

    px = _make_snes_file(tmp_path)
    pal = tmp_path / "colors.pal"
    pal.write_bytes(bytes((i * 7 + 2) & 0xFF for i in range(2 * 16)))  # 16 BGR555

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))

    # A dropped .pal registers as a PALETTE entry, not pixel data — the view
    # stays on the pixel file.
    mime = _drag_payload(pal)
    event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(event)
    palettes = [e for e in window._workspace.entries if e.kind is EntryKind.PALETTE]
    assert len(palettes) == 1
    palette_entry = palettes[0]
    assert palette_entry.palette_preset_id == window._palette_preset_id()
    assert not window._palette_mode.is_real  # registered, not yet applied

    # Re-dropping the same file doesn't duplicate it (path is identity).
    window.dropEvent(event)
    assert sum(e.kind is EntryKind.PALETTE for e in window._workspace.entries) == 1

    # Using it applies the colors to the current view in File mode.
    window._use_palette_entry(palette_entry)
    assert window._palette_mode.is_real
    assert window._palette_mode == "file"
    assert len(window._doc.palette) == 16


def test_palette_format_change_restamps_the_palette_entry(qtbot, tmp_path) -> None:
    """Re-picking the format while a registered .pal is on screen re-stamps the
    entry, so the next double-click decodes the way the user just chose - and
    undo takes the entry back with the palette."""
    from celpix.project.workspace import EntryKind

    px = _make_snes_file(tmp_path)
    pal = tmp_path / "colors.pal"
    pal.write_bytes(bytes((i * 7 + 2) & 0xFF for i in range(2 * 16)))  # 16 BGR555

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._add_palette_file(str(pal))
    entry = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    registered = entry.palette_preset_id
    window._use_palette_entry(entry)

    # Move the dropdown as a user would - same entry size, so the re-decode
    # succeeds and the commit sticks.
    other = "preset.palette.rgb565"
    assert other != registered
    window._palette_preset.setCurrentIndex(window._palette_preset.findData(other))
    assert entry.palette_preset_id == other

    # The format is a property of the entry, not of the dropdown's last
    # position: moving away and re-applying comes back to it.
    window._use_default_palette()
    window._use_palette_entry(entry)
    assert window._palette_preset_id() == other

    window._undo_stack.undo()  # back off the re-apply
    window._undo_stack.undo()  # back off the default
    window._undo_stack.undo()  # back off the format change
    assert entry.palette_preset_id == registered


def test_use_bookmark_as_palette_reads_offset_from_parent(qtbot, tmp_path) -> None:
    from celpix.project.workspace import EntryKind

    # BGR555 white at absolute offset 32 — the offset a bookmark there points at.
    data = bytearray(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))
    data[32:34] = b"\xff\x7f"
    px = tmp_path / "p.4bpp.sfc"
    px.write_bytes(bytes(data))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.find_file(str(px))

    # A bookmark at byte 32 (where the white palette entry sits). Shrink the
    # window first so the view can actually scroll a tile down to it.
    window._columns.setValue(2)
    window._rows.setValue(1)
    window._set_byte_position(32)
    assert window._byte_position() == 32
    window._new_bookmark_for(entry)
    bookmark = next(
        e for e in window._workspace.entries if e.kind is EntryKind.BOOKMARK
    )
    assert bookmark.slice_offset == 32
    # Move the view away — Use as Palette must not move it back, only set colors.
    window._set_byte_position(0)
    assert window._byte_position() == 0

    window._use_bookmark_as_palette(bookmark)
    assert window._palette_mode == "offset"
    assert window._doc.palette_config.source.offset == 32
    assert window._doc.palette.colors[0] == 0xFFFFFFFF
    assert window._byte_position() == 0  # the view position is untouched


def test_use_bookmark_as_palette_stays_on_the_current_slice(qtbot, tmp_path) -> None:
    from celpix.project.workspace import EntryKind, slice_of

    # BGR555 white at absolute offset 32, outside the slice's own window - a
    # slice's palette offsets are parent-absolute and reach past it by design.
    data = bytearray(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))
    data[32:34] = b"\xff\x7f"
    px = tmp_path / "p.4bpp.sfc"
    px.write_bytes(bytes(data))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.find_file(str(px))
    window._new_bookmark_for(entry)  # at byte 0, then retargeted below
    bookmark = next(
        e for e in window._workspace.entries if e.kind is EntryKind.BOOKMARK
    )
    bookmark.slice_offset = 32

    sl = slice_of(entry, "gfx", 0, 32)
    window._apply_add_entry(sl)  # adds and activates
    assert window._workspace.current is sl

    window._use_bookmark_as_palette(bookmark)
    assert window._workspace.current is sl  # no detour through the parent
    assert window._palette_mode == "offset"
    assert window._doc.palette_config.source.offset == 32
    assert window._doc.palette.colors[0] == 0xFFFFFFFF


def test_offset_palette_step_buttons_move_by_one_tile(qtbot, tmp_path) -> None:
    # snes-4bpp: 32 bytes per tile. Start an offset palette at byte 32, then step.
    data = bytearray(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))
    px = tmp_path / "p.4bpp.sfc"
    px.write_bytes(bytes(data))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    assert window._load_palette_at_offset(32)
    assert window._doc.bytes_per_tile == 32

    # Forward one tile: +32 bytes.
    window._step_palette_offset(1)
    assert window._doc.palette_config.source.offset == 64
    # Back one tile: −32 bytes, returning to 32.
    window._step_palette_offset(-1)
    assert window._doc.palette_config.source.offset == 32
    # Stepping back past byte 0 clamps and stops there (no further movement, no
    # alert): from 32 one tile back is 0, and another is still 0.
    window._step_palette_offset(-1)
    assert window._doc.palette_config.source.offset == 0
    window._step_palette_offset(-1)
    assert window._doc.palette_config.source.offset == 0


def test_palette_dock_header_tracks_mode(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    # Default: neither the offset field nor the format row is shown.
    assert not window._palette_offset_edit.isVisibleTo(window)
    assert not window._palette_preset.isVisibleTo(window)
    assert not window._palette_file_label.isVisibleTo(window)

    assert not window._palette_offset_prev.isVisibleTo(window)

    # Offset mode decodes raw bytes: the offset field, its step arrows, and the
    # format row all appear.
    window._on_slots_selected(1, 1)
    window._load_palette_from_selection()
    assert window._palette_mode == "offset"
    assert window._palette_offset_edit.isVisibleTo(window)
    assert window._palette_offset_prev.isVisibleTo(window)
    assert window._palette_offset_next.isVisibleTo(window)
    assert window._palette_preset.isVisibleTo(window)


def test_palette_grid_never_scrolls_a_full_palette(qtbot) -> None:
    """The dock floors its grid at a full 16x16 palette, squeezed or not.

    The scroll area around the grid is a guard for pathological palettes, not
    something a 256-color one may hit: crush the dock to nothing and the grid
    still stands at its full size, with no scroll bar either way.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QScrollArea

    from celpix.core.palette import FULL_PALETTE_COUNT, Palette

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    window._palette_panel.set_colors(Palette.default(FULL_PALETTE_COUNT).colors)
    window.resizeDocks([window._palette_dock], [1], Qt.Orientation.Vertical)
    window.resizeDocks([window._palette_dock], [1], Qt.Orientation.Horizontal)
    qtbot.wait(10)

    holder = window._palette_dock.findChild(QScrollArea)
    assert (
        holder.viewport().size().expandedTo(window._palette_panel.size())
        == holder.viewport().size()
    )
    assert not holder.verticalScrollBar().isVisible()
    assert not holder.horizontalScrollBar().isVisible()


def _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch) -> MainWindow:
    """An 8-tile SNES-4bpp file whose tile 1 starts with BGR555 white."""
    from PySide6.QtWidgets import QFileDialog

    data = bytearray(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))
    data[32:34] = b"\xff\x7f"  # BGR555 0x7FFF = white, little-endian
    px = tmp_path / "p.4bpp.sfc"
    px.write_bytes(bytes(data))
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(px), ""))
    )
    window = MainWindow()
    qtbot.addWidget(window)
    window._open_pixel()
    return window


def test_load_palette_from_selection(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_slots_selected(1, 1)  # byte offset 32
    window._load_palette_from_selection()

    doc = window._doc
    # 256 bytes - 32 offset = 224 bytes = 112 BGR555 entries (256-entry cap unhit).
    assert len(doc.palette) == 112
    assert doc.palette.colors[0] == 0xFFFFFFFF
    assert doc.palette_config.source.offset == 32
    assert doc.palette_config.source.length == 224
    assert doc.palette_config.write_enabled is True  # edited in place
    assert window._palette_mode.is_real
    # The dock reflects the switch to Offset mode, with the offset field armed.
    assert window._palette_mode_combo.currentData() == "offset"
    assert window._palette_offset_edit.isEnabled()
    assert window._palette_offset_edit.text() == "0x000020"

    # Reloading pixels must not clobber the from-selection palette...
    window._apply_pixel_config(window._pixel_preset_id(), window._byte_position())
    assert len(window._doc.palette) == 112
    # ...and Write covers the palette too, since an Offset palette is edited in
    # place and saved back into the bytes it was read from.
    window._write_current()
    assert "pixel + palette" in window.statusBar().currentMessage()


def test_palette_preset_switch_refloors_from_selection_window(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_slots_selected(1, 1)
    window._load_palette_from_selection()

    window._palette_preset.setCurrentIndex(
        window._palette_preset.findData("preset.palette.rgb888")
    )
    doc = window._doc
    # 224 bytes floored to whole 3-byte entries = 74 entries / 222 bytes.
    assert len(doc.palette) == 74
    assert doc.palette_config.source.length == 222
    assert doc.palette_config.write_enabled is True  # still edited in place


def test_palette_panel_click_maps_to_palette_row(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtCore import QPoint, Qt

    from celpix.ui.palette_panel import SWATCH_SIZE, PalettePanel

    panel = PalettePanel()
    qtbot.addWidget(panel)
    panel.set_colors(list(range(256)))
    panel.set_active_range(8, 4)  # 2bpp: 4-entry palette rows, a quarter-row range
    got: list[int] = []
    panel.palette_row_selected.connect(got.append)
    # Swatch 40 = display row 2, col 8; with 4-entry palette rows that's
    # palette row 10 — the index space sizes the mapping, not the 16-wide display.
    qtbot.mouseClick(
        panel,
        Qt.MouseButton.LeftButton,
        pos=QPoint(8 * SWATCH_SIZE + 1, 2 * SWATCH_SIZE + 1),
    )
    assert got == [10]

    # Window-level wiring: the panel's signal drives the palette row spin. Needs
    # a palette that actually has row 5 (the view clamps rows to the palette).
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_slots_selected(0, 0)
    window._load_palette_from_selection()  # 128 colors = rows 0..7
    window._palette_panel.palette_row_selected.emit(5)
    assert window._palette_row.value() == 5


def test_palette_mode_starts_default_and_default_restores_fallback(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    assert window._palette_mode_combo.currentData() == "default"
    # The offset field is hidden outside Offset mode (isVisibleTo reports the
    # intended visibility even though this test never shows the window).
    assert not window._palette_offset_edit.isVisibleTo(window)

    window._on_slots_selected(1, 1)
    window._load_palette_from_selection()
    assert window._palette_mode.is_real
    # Offset mode reveals the field.
    assert window._palette_offset_edit.isVisibleTo(window)

    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.DEFAULT)
    )
    colors = window._doc.palette.colors
    assert colors[0] == 0xFF000000 and colors[1] == 0xFFFFFFFF  # fallback again
    assert not window._palette_mode.is_real
    assert window._doc.palette_config.source.path == ""
    assert not window._palette_offset_edit.isVisibleTo(window)
    assert window._palette_offset_edit.text() == ""


def _fceux_nes_state(tmp_path, first_index: int) -> object:
    """A minimal FCEUX (.fc0) NES state whose PPU palette RAM starts with
    ``first_index`` — an index into the NES 64-color master palette. Just the
    "FCSX" header and a PPU section (type 3) carrying the "PRAM" field."""
    import struct

    def u32(v):
        return struct.pack("<I", v)

    pram = bytes([first_index]) + b"\x00" * 31
    ppu = b"PRAM" + u32(len(pram)) + pram
    payload = bytes([3]) + u32(len(ppu)) + ppu
    header = b"FCSX" + u32(len(payload)) + u32(0) + u32(0xFFFFFFFF)
    state = tmp_path / "game.fc0"
    state.write_bytes(header + payload)
    return state


def _mesen_state(tmp_path) -> object:
    """A minimal Mesen (.mss) SNES state on disk: the MSS header, a dummy zlib
    screenshot and ROM name, then the zlib-compressed record stream carrying a
    ``ppu.cgram`` entry with three distinct colors. Exercises the extract path
    (parse header → zlib → read CGRAM by label) and the inline-bytes source."""
    import struct
    import zlib

    def u32(v):
        return struct.pack("<I", v)

    colors = [(0xF8, 0, 0), (0, 0xF8, 0), (0, 0, 0xF8)]  # R, G, B
    words = [((b >> 3) << 10) | ((g >> 3) << 5) | (r >> 3) for (r, g, b) in colors]
    cgram = struct.pack("<256H", *(words + [0] * (256 - len(words))))
    records = b"ppu.cgram\x00" + u32(len(cgram)) + cgram
    comp = zlib.compress(records)

    screenshot = zlib.compress(b"\x00" * 16)
    header = b"MSS" + u32(0) + u32(4) + u32(0)  # versions + console 0 (SNES)
    video = u32(16) + u32(2) + u32(2) + u32(100) + u32(len(screenshot)) + screenshot
    blob = b"\x01" + u32(len(records)) + u32(len(comp)) + comp
    state = tmp_path / "game.mss"
    state.write_bytes(header + video + u32(4) + b"game" + blob)
    return state


def test_mesen_state_extracts_snes_palette(qtbot, tmp_path, monkeypatch) -> None:
    # Mesen's palette lives inside a zlib-compressed record stream, not at a file
    # offset — this drives the inline-bytes source path through the pipeline.
    from PySide6.QtWidgets import QFileDialog

    state = _mesen_state(tmp_path)
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(state), ""))
    )
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.EMULATOR)
    )

    assert window._palette_mode == "emulator"
    assert window._palette_preset_id() == "preset.palette.bgr555"  # SNES CGRAM
    assert window._doc.palette_config.write_enabled is False  # view-only state
    colors = window._doc.palette.colors
    assert len(colors) == 256
    # The three CGRAM colors decoded to three distinct non-black entries.
    assert len({colors[0], colors[1], colors[2]}) == 3
    window._undo_stack.undo()
    assert window._palette_mode == "default"


def test_emulator_state_loads_and_switches_codec(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QFileDialog

    # index 0x30 in the NES master table is white — an easy color to assert on.
    state = _fceux_nes_state(tmp_path, 0x30)
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    assert window._palette_preset_id() == "preset.palette.bgr555"  # SNES default

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(state), ""))
    )
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.EMULATOR)
    )

    assert window._palette_mode == "emulator"
    # The console was auto-detected, so the palette codec is now the NES table...
    assert window._palette_preset_id() == "preset.palette.nes-indexed"
    assert (
        window._doc.palette_config.interpret_preset_id == "preset.palette.nes-indexed"
    )
    # ...and the 32 index bytes decoded through it, first one white.
    assert len(window._doc.palette) == 32
    assert window._doc.palette.colors[0] == 0xFFFFFFFF
    assert window._doc.palette_config.write_enabled is False  # view-only
    assert window._palette_mode.is_real
    # Undo returns to the previous (default) palette and its codec.
    window._undo_stack.undo()
    assert window._palette_mode == "default"
    assert window._palette_preset_id() == "preset.palette.bgr555"


def test_emulator_state_unrecognised_reverts_with_message(
    qtbot, tmp_path, monkeypatch, captured_alerts
) -> None:
    from PySide6.QtWidgets import QFileDialog

    junk = tmp_path / "not.state"
    junk.write_bytes(b"random bytes, no known signature")
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(junk), ""))
    )
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.EMULATOR)
    )
    assert window._palette_mode == "default"
    # The failure is surfaced as a modal alert, not a status line.
    assert any("Unrecognised" in message for _title, message in captured_alerts)


def test_palette_offset_failure_alerts_not_status(
    qtbot, tmp_path, monkeypatch, captured_alerts
) -> None:
    # A palette load that can't size even one entry (offset past EOF) used to
    # fail with only a status line; it now blocks with a modal so the user
    # can't miss that nothing loaded.
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    assert not window._load_palette_at_offset(1 << 20)
    assert any("Not enough data" in message for _title, message in captured_alerts)


def test_offset_palette_on_a_tilemap_reads_the_file_the_map_cuts_into(
    qtbot, tmp_path, captured_alerts
) -> None:
    """An Offset palette is in the owning **file entry's** coordinates — for a
    map exactly as for a graphic, since the map's cells are cut from that file
    too.

    A tilemap's ``pixel_config`` is the *bound bank's*, so resolving the offset
    through it read the wrong file — and an **unbound** map offers no file at
    all, which turned every offset into "not enough data" and dropped the entry
    to the default palette on the way in. The palette is the map's own answer and
    must survive having no tiles yet, which is the state a map is in until it is
    bound.
    """
    from celpix.core.capabilities import ContentKind

    # Deep into a ROM-sized file, where the palettes of a real project sit: the
    # wrong file is not merely a different palette, it is one no offset this
    # large fits inside, which is what turned the bug into a hard failure.
    data = bytearray((i * 7 + 3) & 0xFF for i in range(0x8000))
    data[0x7000:0x7002] = b"\xff\x7f"  # BGR555 white, the map's first color
    rom = tmp_path / "rom.bin"
    rom.write_bytes(bytes(data))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(rom))
    cut = window._workspace.add_slice(str(rom), "map", 0x100, 0x40)
    cut.content_kind = ContentKind.TILEMAP
    cut.tilemap_preset_id = "preset.tilemap.snes-bg"
    window._activate_entry(cut)
    assert window._doc.is_tilemap and not window._doc.pixel_data  # nothing bound

    assert window._load_palette_at_offset(0x7000)
    assert window._doc.palette.color(0) == 0xFFFFFFFF

    # And again on the way back in, which is where a project restore reads it.
    window._capture_session()
    window._workspace.drop_document(cut)
    window._activate_entry(cut)
    assert window._doc.palette.color(0) == 0xFFFFFFFF
    assert captured_alerts == []


def test_emulator_state_redetects_on_restore(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QFileDialog

    from celpix.project.workspace import EntryKind

    state = _fceux_nes_state(tmp_path, 0x30)  # NES white
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    entry = window._workspace.current

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(state), ""))
    )
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.EMULATOR)
    )
    assert window._palette_mode == "emulator"

    # Bookmark the emulator-state view: the snapshot stores only the state path.
    window._new_bookmark_for(entry)
    bookmark = next(
        e for e in window._workspace.entries if e.kind is EntryKind.BOOKMARK
    )
    assert bookmark.session.palette_mode == "emulator"
    assert bookmark.pending_palette is not None and bookmark.pending_palette.path

    # Drive the parent back to the default palette, then jump: restoring must
    # re-detect the state (offset + NES codec are not stored) and re-decode it.
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.DEFAULT)
    )
    window._jump_to_bookmark(bookmark)
    assert window._palette_mode == "emulator"
    assert window._palette_preset_id() == "preset.palette.nes-indexed"
    assert window._doc.palette.colors[0] == 0xFFFFFFFF


def test_emulator_format_row_is_live_and_reinterprets(
    qtbot, tmp_path, monkeypatch
) -> None:
    # Emulator mode auto-detects the console's codec but still offers the format
    # dropdown, so the user can reinterpret how the state's palette bytes are read.
    from PySide6.QtWidgets import QFileDialog

    state = _mesen_state(tmp_path)  # SNES CGRAM: 256 BGR555 colors = 512 bytes
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(state), ""))
    )
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.EMULATOR)
    )
    assert window._palette_mode == "emulator"
    # The row is shown and live (unlike Default, which hides it).
    assert window._palette_preset.isVisibleTo(window)
    assert window._palette_preset.isEnabled()
    assert window._palette_preset_id() == "preset.palette.bgr555"
    assert len(window._doc.palette) == 256

    # Reinterpret the same 512 bytes as RGB888 (3 bytes/entry): 512 // 3 = 170,
    # so the inline CGRAM is re-floored to a whole number of entries.
    window._palette_preset.setCurrentIndex(
        window._palette_preset.findData("preset.palette.rgb888")
    )
    assert window._palette_mode == "emulator"  # still the state, read differently
    assert window._palette_preset_id() == "preset.palette.rgb888"
    assert window._doc.palette_config.interpret_preset_id == "preset.palette.rgb888"
    assert len(window._doc.palette) == 170
    # Undo restores the console's codec and its 256 colors.
    window._undo_stack.undo()
    assert window._palette_preset_id() == "preset.palette.bgr555"
    assert len(window._doc.palette) == 256


def test_custom_carries_a_live_format(qtbot, tmp_path, monkeypatch) -> None:
    # A Custom palette carries a color format, and the combo is live: picking a
    # format rebases its colors rather than reinterpreting bytes it doesn't have.
    # Forked off the generated default it inherits the session default (RGB888).
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    assert window._palette_mode == "default"
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.CUSTOM)
    )
    assert window._palette_mode == "custom"
    assert window._palette_preset.isVisibleTo(window)
    assert window._palette_preset.isEnabled()
    assert window._palette_preset_id() == "preset.palette.rgb888"
    assert window._doc.palette_config.interpret_preset_id == "preset.palette.rgb888"


def test_custom_format_change_relabels_without_touching_colors(
    qtbot, tmp_path, monkeypatch
) -> None:
    # A Custom palette stores ARGB verbatim, so picking a format only records the
    # target the Quantize button would snap to - the colors stay exactly as set.
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.CUSTOM)
    )
    assert window._palette_preset_id() == "preset.palette.rgb888"
    window._doc.palette = window._doc.palette.with_color(0, 0xFF010203)

    window._palette_preset.setCurrentIndex(
        window._palette_preset.findData("preset.palette.bgr555")
    )

    assert window._palette_mode == "custom"
    assert window._palette_preset_id() == "preset.palette.bgr555"
    assert window._doc.palette_config.interpret_preset_id == "preset.palette.bgr555"
    assert window._doc.palette.colors[0] == 0xFF010203  # unchanged - only relabelled


def test_the_quantize_button_snaps_custom_colors_to_the_format(
    qtbot, tmp_path, monkeypatch
) -> None:
    """Quantize is the explicit one-shot conversion of stored colors onto what the
    selected format can hold - where it is offered, what it moves, and what it
    costs on the undo stack.

    One window: which mode is showing is the very thing being varied, so the
    passes have to run against the same palette dock anyway.
    """
    from celpix.pipeline import pipeline

    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)

    # Offered for Custom alone - the one mode whose colors are stored raw. A
    # raw-bytes palette already holds values its format can store, so there would
    # be nothing to snap.
    assert window._palette_mode == "default"
    assert not window._quantize_palette_action.isVisibleTo(window)
    assert window._load_palette_at_offset(32)  # Offset mode
    assert not window._quantize_palette_action.isVisibleTo(window)

    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.CUSTOM)
    )
    window._palette_preset.setCurrentIndex(
        window._palette_preset.findData("preset.palette.bgr555")
    )
    assert window._quantize_palette_action.isVisibleTo(window)

    # A color whose low channel bits BGR555 can't keep, so Quantize must move it.
    window._doc.palette = window._doc.palette.with_color(0, 0xFF010203)
    expected = pipeline.quantize_color(
        0xFF010203, "preset.palette.bgr555", window._registry
    )
    assert expected != 0xFF010203  # BGR555 really is lossy for this color

    window._quantize_palette_action.click()

    assert window._palette_mode == "custom"  # still project-stored ARGB
    assert window._doc.palette.colors[0] == expected
    window._undo_stack.undo()  # one step back to the pre-quantize colors
    assert window._doc.palette.colors[0] == 0xFF010203

    # Quantizing colors that already fit is a no-op, and must not leave a dead
    # undo entry the user has to walk back past.
    window._quantize_palette_action.click()  # everything now sits on BGR555 values
    depth = window._undo_stack.index()
    window._quantize_palette_action.click()
    assert window._undo_stack.index() == depth


def test_custom_forked_from_offset_keeps_the_source_format(
    qtbot, tmp_path, monkeypatch
) -> None:
    # A Custom palette forked from a raw-bytes source keeps that source's format
    # rather than falling back to the session default.
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    assert window._load_palette_at_offset(32)  # decodes BGR555 (the dropdown's default)
    assert window._palette_mode == "offset"
    assert window._palette_preset_id() == "preset.palette.bgr555"
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.CUSTOM)
    )
    assert window._palette_mode == "custom"
    assert window._palette_preset_id() == "preset.palette.bgr555"
    assert window._doc.palette_config.interpret_preset_id == "preset.palette.bgr555"


def test_session_default_format_follows_the_last_selection(
    qtbot, tmp_path, monkeypatch
) -> None:
    # The session default a Custom-from-default fork inherits tracks the last
    # format actually chosen (an import here), not a fixed RGB888.
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    assert window._session_palette_format == "preset.palette.rgb888"  # untouched start
    assert window._load_palette_at_offset(32)  # a BGR555 import
    assert window._session_palette_format == "preset.palette.bgr555"
    # Back to the generated default, then fork Custom: it inherits BGR555 now.
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.DEFAULT)
    )
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.CUSTOM)
    )
    assert window._palette_mode == "custom"
    assert window._palette_preset_id() == "preset.palette.bgr555"


def test_palette_offset_box_commit_loads_at_offset(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    # Switching to Offset mode with no selection loads at the window's top-left
    # (byte 0 here).
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.OFFSET)
    )
    assert window._palette_mode == "offset"
    assert window._doc.palette_config.source.offset == 0

    # Typing an offset re-loads there (tile 1 starts with BGR555 white).
    window._palette_offset_edit.setText("0x20")
    window._palette_offset_edit.commit()
    assert window._doc.palette.colors[0] == 0xFFFFFFFF
    assert window._doc.palette_config.source.offset == 32
    assert window._doc.palette_config.write_enabled is True
    assert window._palette_offset_edit.text() == "0x000020"  # normalised


def test_palette_mode_file_cancel_reverts_dropdown(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QFileDialog

    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_slots_selected(1, 1)
    window._load_palette_from_selection()
    before = list(window._doc.palette.colors)

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("", ""))
    )
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.FILE)
    )
    assert window._palette_mode == "offset"
    assert window._palette_mode_combo.currentData() == "offset"
    assert window._doc.palette.colors == before


def test_palette_offset_box_follows_address_format(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_slots_selected(1, 1)
    window._load_palette_from_selection()
    _select_address_format(window, "snes-lorom")
    assert window._palette_offset_edit.text() == "$00:8020"


def test_palette_panel_arrows_move_selection_and_palette_row_follows(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from celpix.ui.palette_panel import PalettePanel

    panel = PalettePanel()
    qtbot.addWidget(panel)
    panel.set_colors(list(range(64)))  # 4 palette rows of 16
    panel.set_active_range(16, 16)  # row 1 active
    got: list[int] = []
    panel.palette_row_selected.connect(got.append)

    # Up/Down move the *selection* one display row; the palette row follows it.
    # With no selection yet, movement starts from the active range's first entry.
    qtbot.keyClick(panel, Qt.Key.Key_Down)  # selects 32 -> palette row 2
    qtbot.keyClick(panel, Qt.Key.Key_Up)  # selects 16 -> palette row 1
    assert (panel.selected_index(), got) == (16, [2, 1])

    # No display row above/below: the selection (and its column) stays put.
    panel._select(3)
    qtbot.keyClick(panel, Qt.Key.Key_Up)
    assert panel.selected_index() == 3
    panel._select(51)
    qtbot.keyClick(panel, Qt.Key.Key_Down)
    assert panel.selected_index() == 51

    # While the panel is focused, the window's global nav filter defers to it
    # (same contract as the other arrow-consuming inputs).
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    monkeypatch.setattr(
        QApplication, "focusWidget", staticmethod(lambda: window._palette_panel)
    )
    down = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier
    )
    assert window._handle_nav_key(down) is False
    assert window._offset == 0


def test_p_key_loads_palette_from_selection(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_slots_selected(1, 1)
    press_p = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_P, Qt.KeyboardModifier.NoModifier
    )

    # Focused text input keeps the letter (it may be typing).
    monkeypatch.setattr(
        QApplication, "focusWidget", staticmethod(lambda: window._address_edit)
    )
    assert window._handle_nav_key(press_p) is False
    assert not window._palette_mode.is_real

    # Otherwise P triggers Palette > Palette from Selection.
    monkeypatch.setattr(
        QApplication, "focusWidget", staticmethod(lambda: window._canvas)
    )
    assert window._handle_nav_key(press_p) is True
    assert window._palette_mode == "offset"
    assert window._doc.palette.colors[0] == 0xFFFFFFFF


def test_palette_panel_color_selection_click_and_arrows(qtbot) -> None:
    from PySide6.QtCore import QPoint, Qt

    from celpix.ui.palette_panel import SWATCH_SIZE, PalettePanel

    panel = PalettePanel()
    qtbot.addWidget(panel)
    panel.set_colors(list(range(32)))  # two rows of 16
    panel.set_active_range(16, 16)
    picked: list[int] = []
    panel.color_selected.connect(picked.append)

    # Click selects the color (and still selects its palette row — separate signal).
    qtbot.mouseClick(
        panel,
        Qt.MouseButton.LeftButton,
        pos=QPoint(3 * SWATCH_SIZE + 1, SWATCH_SIZE + 1),
    )
    assert panel.selected_index() == 19
    assert picked == [19]

    # Left/Right move the selection freely across rows, capped only at the
    # palette's ends.
    qtbot.keyClick(panel, Qt.Key.Key_Right)
    qtbot.keyClick(panel, Qt.Key.Key_Left)
    qtbot.keyClick(panel, Qt.Key.Key_Left)
    assert picked == [19, 20, 19, 18]
    panel._select(16)
    qtbot.keyClick(panel, Qt.Key.Key_Left)  # crosses into the previous row
    assert panel.selected_index() == 15
    panel._select(0)
    qtbot.keyClick(panel, Qt.Key.Key_Left)  # palette start: no change
    assert panel.selected_index() == 0
    panel._select(31)
    qtbot.keyClick(panel, Qt.Key.Key_Right)  # palette end: no change
    assert panel.selected_index() == 31

    # (Up/Down movement + the palette row following the selection are covered by
    # test_palette_panel_arrows_move_selection_and_palette_row_follows.)

    # With no selection, Right starts from the active palette row's first entry.
    fresh = PalettePanel()
    qtbot.addWidget(fresh)
    fresh.set_colors(list(range(32)))
    fresh.set_active_range(16, 16)
    qtbot.keyClick(fresh, Qt.Key.Key_Right)
    assert fresh.selected_index() == 17

    # A shrunken palette clamps a stranded selection back inside (or clears it
    # when nothing is left).
    panel.set_colors(list(range(8)))
    assert panel.selected_index() == 7
    panel.set_colors([])
    assert panel.selected_index() is None


def test_palette_panel_drag_scrubs_selection_and_clamps(qtbot) -> None:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent, QPointingDevice

    from celpix.ui.palette_panel import SWATCH_SIZE, PalettePanel

    panel = PalettePanel()
    qtbot.addWidget(panel)
    panel.set_colors(list(range(20)))  # row of 16 + a short second row (4)
    picked: list[int] = []
    panel.color_selected.connect(picked.append)
    rows: list[int] = []
    panel.palette_row_selected.connect(rows.append)
    panel.set_active_range(0, 4)  # 4-wide palette rows, so a drag crosses several

    device = QPointingDevice.primaryPointingDevice()

    def move_event(x_px: float, y_px: float, held: bool) -> QMouseEvent:
        point = QPointF(x_px, y_px)
        return QMouseEvent(
            QMouseEvent.Type.MouseMove,
            point,
            point,  # global == local is fine; the handler only reads position()
            Qt.MouseButton.NoButton,  # no *new* button on a move
            Qt.MouseButton.LeftButton if held else Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            device,
        )

    def drag_to(x_px: float, y_px: float) -> None:
        panel.mouseMoveEvent(move_event(x_px, y_px, held=True))

    # Press to start on swatch 2, then drag across the row.
    qtbot.mouseClick(
        panel, Qt.MouseButton.LeftButton, pos=QPoint(2 * SWATCH_SIZE + 1, 1)
    )
    assert panel.selected_index() == 2
    drag_to(5 * SWATCH_SIZE + 1, 1)  # → swatch 5
    drag_to(9 * SWATCH_SIZE + 1, 1)  # → swatch 9
    assert panel.selected_index() == 9
    assert picked == [2, 5, 9]
    assert rows == [0, 1, 2]  # palette row (index // 4) follows the drag

    # Off the right/bottom edge clamps to the nearest real swatch, never None:
    # past the last (short) row lands on the final color.
    drag_to(100 * SWATCH_SIZE, 100 * SWATCH_SIZE)
    assert panel.selected_index() == 19
    # Off the left/top edge clamps to swatch 0.
    drag_to(-50, -50)
    assert panel.selected_index() == 0

    # A drag with no button held does nothing (hover must not select).
    panel.mouseMoveEvent(move_event(6 * SWATCH_SIZE + 1, 1, held=False))
    assert panel.selected_index() == 0

    # The eyedropper stays click-only: a drag neither selects nor samples.
    sampled: list[object] = []
    panel.color_picked.connect(sampled.append)
    panel.set_eyedropper(True)
    drag_to(6 * SWATCH_SIZE + 1, 1)
    assert panel.selected_index() == 0 and sampled == []


def test_palette_clipboard_round_trip_and_hex_parsing(qtbot) -> None:
    from celpix.ui import clipboard

    clipboard.put_colors([0xFF112233, 0x80FF0000])
    assert clipboard.take_colors() == [0xFF112233, 0x80FF0000]
    assert clipboard.has_colors()
    # color_text: opaque drops the alpha, translucent keeps it.
    assert clipboard.color_text(0xFF112233) == "#112233"
    assert clipboard.color_text(0x80112233) == "#80112233"
    # Foreign hex text: 6-digit is opaque, 8-digit carries alpha, junk ignored,
    # and a longer hex run is not a color token.
    assert clipboard._parse_hex_colors("#ff0000 00ff00 zz 80112233") == [
        0xFFFF0000,
        0xFF00FF00,
        0x80112233,
    ]
    assert clipboard._parse_hex_colors("deadbeefcafe") == []


def test_palette_panel_copy_paste_keys_emit(qtbot) -> None:
    from PySide6.QtCore import Qt

    from celpix.ui.palette_panel import PalettePanel

    panel = PalettePanel()
    qtbot.addWidget(panel)
    panel.set_colors(list(range(16)))
    panel._select(3)
    events: list[str] = []
    panel.copy_requested.connect(lambda: events.append("copy"))
    panel.paste_requested.connect(lambda: events.append("paste"))
    panel.copy_palette_row_requested.connect(lambda: events.append("copy-sub"))
    panel.paste_palette_row_requested.connect(lambda: events.append("paste-sub"))
    ctrl = Qt.KeyboardModifier.ControlModifier
    ctrl_shift = ctrl | Qt.KeyboardModifier.ShiftModifier
    qtbot.keyClick(panel, Qt.Key.Key_C, ctrl)
    qtbot.keyClick(panel, Qt.Key.Key_V, ctrl)
    qtbot.keyClick(panel, Qt.Key.Key_C, ctrl_shift)  # Ctrl+Shift → palette row
    qtbot.keyClick(panel, Qt.Key.Key_V, ctrl_shift)
    assert events == ["copy", "paste", "copy-sub", "paste-sub"]


def test_palette_panel_right_click_selects(qtbot) -> None:
    from PySide6.QtCore import QPoint, Qt

    from celpix.ui.palette_panel import SWATCH_SIZE, PalettePanel

    panel = PalettePanel()
    qtbot.addWidget(panel)
    panel.set_colors(list(range(32)))
    # Right-clicking a swatch moves the selection onto it (so the menu acts there).
    qtbot.mouseClick(
        panel, Qt.MouseButton.RightButton, pos=QPoint(5 * SWATCH_SIZE + 1, 1)
    )
    assert panel.selected_index() == 5


def test_palette_copy_paste_color_and_undo(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_slots_selected(1, 1)
    window._load_palette_from_selection()  # Offset mode: editable in place
    panel = window._palette_panel

    # Copy entry 0 (white), paste it onto entry 1.
    panel._select(0)
    source = window._doc.palette.color(0)
    window._copy_palette_color()
    panel._select(1)
    before = window._doc.palette.color(1)
    assert before != source  # the test only means something if they differ
    window._paste_palette_color()
    assert window._doc.palette.color(1) == source

    # Paste is one undoable step.
    window._undo_stack.undo()
    assert window._doc.palette.color(1) == before

    # Cross-application: a plain hex string on the clipboard pastes too.
    from PySide6.QtCore import QMimeData
    from PySide6.QtGui import QGuiApplication

    mime = QMimeData()
    mime.setText("#123456")
    QGuiApplication.clipboard().setMimeData(mime)
    panel._select(2)
    window._paste_palette_color()
    assert window._doc.palette.color(2) == 0xFF123456


def test_palette_copy_paste_palette_row_and_undo(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_slots_selected(1, 1)
    window._load_palette_from_selection()  # Offset mode: editable in place
    space = window._index_space()

    # Copy palette row 0, paste it over palette row 1.
    window._palette_row.setValue(0)
    source = [window._doc.palette.color(k) for k in range(space)]
    window._copy_palette_row()
    window._palette_row.setValue(1)
    before = [window._doc.palette.color(space + k) for k in range(space)]
    assert before != source  # the test only means something if they differ

    base = window._undo_stack.count()
    window._paste_palette_row()
    assert [window._doc.palette.color(space + k) for k in range(space)] == source
    # The whole range is one undo step (a macro), not one per color.
    assert window._undo_stack.count() - base == 1
    window._undo_stack.undo()
    assert [window._doc.palette.color(space + k) for k in range(space)] == before


def test_mode_switch_resets_row_and_selection_into_palette(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    # The generated default runs the full 256, so it is the *longest* palette a
    # session sees - which is what leaves a row to clamp on the way to any other.
    assert len(window._doc.palette) == 256  # 16 palette rows at 4bpp
    window._palette_row.setValue(12)
    window._palette_panel._select(200)

    # Switching to a shorter palette (128 colors = rows 0..7) has to pull both
    # the row and the swatch selection back inside it.
    window._on_slots_selected(0, 0)
    window._load_palette_from_selection()
    assert len(window._doc.palette) == 128
    assert window._palette_row.value() == 7
    assert window._doc.view.palette_row == 7
    assert window._palette_panel.selected_index() == 127
    assert "Palette Row 7 · Color 15" in window._color_details.text()


def test_pixel_mode_switch_reanchors_palette_row_on_selection(
    qtbot, tmp_path, monkeypatch
) -> None:
    # The palette row index is relative to the format's color count, so a
    # preset switch recomputes it from the selected color: entry 20 is row 1
    # under 4bpp (16-entry rows) but row 5 under 2bpp (4-entry rows).
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_slots_selected(0, 0)
    window._load_palette_from_selection()  # 128 colors
    window._palette_panel._select(20)
    window._palette_row.setValue(1)
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.gb-2bpp")
    )
    assert window._palette_row.value() == 5
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.snes-4bpp")
    )
    assert window._palette_row.value() == 1

    # Without a color selection the old base anchors instead, so the view
    # keeps showing the same palette region.
    window._palette_panel.set_colors([])  # drops the selection
    window._palette_panel.set_colors(window._doc.palette.colors)
    window._palette_row.setValue(2)  # base 32 under 4bpp
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.gb-2bpp")
    )
    assert window._palette_row.value() == 8  # base 32 under 2bpp


def test_color_details_show_selected_color(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    assert window._color_details.text() == "No color selected"

    # Fallback palette entry 1 is white; selecting it fills the readout. The
    # position reads as palette row + color-within-it (4bpp: 16-entry rows).
    window._palette_panel._select(1)
    assert "#FFFFFFFF" in window._color_details.text()
    assert "Palette Row 0 · Color 1 ($1)" in window._color_details.text()
    assert "R 255  G 255  B 255  A 255" in window._color_details.text()

    # A palette reload recolors the same index; the readout follows on refresh.
    window._on_slots_selected(1, 1)
    window._load_palette_from_selection()
    assert "#FFFFFFFF" not in window._color_details.text()  # index 1 changed


def test_offset_palette_under_a_reshape_reads_the_joined_bytes(qtbot, tmp_path):
    """An Offset palette addresses the same coordinate space the view does, so
    under a reshape it reads the *joined* buffer rather than whatever the file
    happens to hold at that number. Reading the file instead is the regression
    this guards, and it fails quietly: the wrong bytes still decode to plausible
    colors (docs/design/palette-editing.md §2).
    """
    # split-planes-2 lays the first half on the even positions and the second on
    # the odd ones, so joined 32..33 comes from file bytes 16 and 80 — and the
    # file's own 32..33 is something else entirely.
    data = bytearray((i * 13 + 1) & 0xFF for i in range(128))
    data[16], data[80] = 0xFF, 0x7F
    assert bytes(data[32:34]) != b"\xff\x7f"  # the bytes the old read would find
    px = tmp_path / "pair.4bpp.sfc"
    px.write_bytes(bytes(data))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.current
    entry.reshape_id = "reshape.split-planes-2"
    window._capture_session()
    window._workspace.drop_document(entry)
    window._on_current_entry_changed(entry)

    assert window._load_palette_at_offset(32)
    doc = window._doc
    assert doc.palette_base_bytes[:2] == b"\xff\x7f"
    assert doc.palette.colors[0] == 0xFFFFFFFF
    assert doc.palette_config.source.data is not None  # cut from the joined buffer
    # Reordered bytes have no file offset for a splice to land on.
    assert doc.palette_config.write_enabled is False


def test_offset_palette_edit_in_reshaped_region_saves_through_the_owner(
    qtbot, tmp_path
):
    """A color edit on a buffer-backed Offset palette persists through the
    owner's pixel pathway: spliced into its buffer (as undoable pixel dirt),
    then carried through unshape by the owner's ordinary Write — landing each
    byte on the chip it came from (docs/design/palette-editing.md §2).
    """
    # split-planes-2 on one file: joined 32..33 come from file bytes 16 and 80.
    data = bytearray((i * 13 + 1) & 0xFF for i in range(128))
    data[16], data[80] = 0xFF, 0x7F  # BGR555 white at joined offset 32
    px = tmp_path / "pair.4bpp.sfc"
    px.write_bytes(bytes(data))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.current
    entry.reshape_id = "reshape.split-planes-2"
    window._capture_session()
    window._workspace.drop_document(entry)
    window._on_current_entry_changed(entry)

    assert window._load_palette_at_offset(32)
    assert window._doc.palette.color(0) == 0xFFFFFFFF
    window._palette_panel._select(0)
    window._on_color_changed(0xFFFF0000)  # pure red -> BGR555 0x001F

    doc = window._doc
    assert doc.palette.color(0) == 0xFFFF0000
    assert doc.pixel_data[32:34] == b"\x1f\x00"  # landed in the owner's buffer
    assert entry.pixel_dirty  # ...as pixel dirt on the owner
    assert not entry.palette_dirty  # the palette pathway never writes here

    # Tokened through undo exactly as palette dirt is.
    window._undo_stack.undo()
    assert window._doc.pixel_data[32:34] == b"\xff\x7f"
    assert not entry.pixel_dirty
    window._undo_stack.redo()
    assert window._doc.pixel_data[32:34] == b"\x1f\x00"
    assert entry.pixel_dirty

    # The owner's Write unshapes the whole region: each half of the edited
    # entry returns to the file byte it came from, everything else untouched.
    assert window._write_entry(entry)
    out = px.read_bytes()
    assert (out[16], out[80]) == (0x1F, 0x00)
    expect = bytearray(data)
    expect[16], expect[80] = 0x1F, 0x00
    assert out == bytes(expect)
    assert not entry.pixel_dirty


def test_offset_palette_edit_on_slice_loads_and_dirties_the_parent(qtbot, tmp_path):
    """From a slice, a buffer-backed Offset palette edit lands on the *parent*:
    its buffer holds the bytes, so it is loaded if closed and its pixel pathway
    is what goes dirty - the slice itself stays clean (it is view-only under a
    reordered parent, and the palette was never its bytes to begin with).
    """
    data = bytearray((i * 13 + 1) & 0xFF for i in range(128))
    data[16], data[80] = 0xFF, 0x7F  # BGR555 white at joined offset 32
    px = tmp_path / "pair.4bpp.sfc"
    px.write_bytes(bytes(data))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    parent = window._workspace.current
    parent.reshape_id = "reshape.split-planes-2"
    window._capture_session()
    window._workspace.drop_document(parent)
    window._on_current_entry_changed(parent)

    slice_entry = window._workspace.add_slice(str(px), "gfx", 64, 32)
    window._activate_entry(slice_entry)
    window._workspace.drop_document(parent)  # the closed-parent branch
    assert parent.doc is None

    assert window._load_palette_at_offset(32)
    assert window._doc.palette.color(0) == 0xFFFFFFFF
    window._palette_panel._select(0)
    window._on_color_changed(0xFFFF0000)

    # The first edit loaded the parent and spliced into *its* buffer.
    assert parent.doc is not None
    assert parent.doc.pixel_data[32:34] == b"\x1f\x00"
    assert parent.pixel_dirty
    assert not slice_entry.pixel_dirty and not slice_entry.palette_dirty


def test_format_switch_refloors_buffer_backed_offset_palette_in_place(qtbot, tmp_path):
    """Switching the palette format re-cuts a buffer-backed Offset window from
    the owner's buffer with its base intact. The re-floor used to rebuild the
    ref against the raw file (and drop ``data_base``), so with a container skip
    in front the redecode silently read a different window.
    """
    # Copier header (base 512) + split-planes-2 over the 1024-byte body:
    # joined 32..33 come from body bytes 16 and 512+16.
    data = bytearray((i * 13 + 1) & 0xFF for i in range(512 + 1024))
    data[512 + 16], data[512 + 512 + 16] = 0xFF, 0x7F  # white at joined 512+32
    px = tmp_path / "pair.4bpp.sfc"
    px.write_bytes(bytes(data))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.current
    entry.container_id = "container.copier-header"
    entry.reshape_id = "reshape.split-planes-2"
    window._capture_session()
    window._workspace.drop_document(entry)
    window._on_current_entry_changed(entry)
    # Reshaped: the display shows 0-based buffer positions, and a palette
    # offset is one of those numbers - the container's skip is already inside
    # the buffer, not something the offset re-adds.
    assert window._anchor_base() == 0
    assert window._load_palette_at_offset(32)
    assert window._doc.palette.color(0) == 0xFFFFFFFF

    window._palette_preset.setCurrentIndex(
        window._palette_preset.findData("preset.palette.bgr555-be")
    )
    # Same window, same bytes, re-decoded: the re-floor was cut from the
    # owner's buffer again, not re-read from the raw file at that number.
    src = window._doc.palette_config.source
    assert (src.offset, src.data_base) == (32, 0)
    assert window._doc.palette_base_bytes[:2] == b"\xff\x7f"


def test_offset_palette_lands_on_the_offset_shown_past_a_header(qtbot, tmp_path):
    """A container that only skips a header leaves offsets naming file bytes, so
    an Offset palette reads the file at exactly the number the offset box shows
    — and stays writable. The skip was previously added a second time on the way
    in, putting every Palette from Selection a header's width past the bytes it
    pointed at.
    """
    # One 16 KB PRG bank, then CHR: the view — and so the palette offset — starts
    # at 0x4010 rather than 0.
    chr_start = 16 + 0x4000
    rom = bytearray([*b"NES\x1a", 1, 1, 0, 0] + [0] * 8)
    rom += bytes((i * 7) & 0xFF for i in range(0x4000 + 0x2000))
    rom[chr_start + 32 : chr_start + 34] = b"\xff\x7f"
    nes = tmp_path / "g.nes"
    nes.write_bytes(bytes(rom))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(nes))
    assert window._anchor_base() == chr_start

    assert window._load_palette_at_offset(chr_start + 32)
    doc = window._doc
    assert doc.palette_base_bytes[:2] == b"\xff\x7f"
    assert doc.palette_config.source.offset == chr_start + 32
    assert doc.palette_config.source.data is None  # read straight from the file
    assert doc.palette_config.write_enabled is True

    # A slice offset is written in those same coordinates, so New Slice… must
    # prefill the address on screen rather than the config's requested 0 — which
    # is what it did, putting every slice of a headered file a header short.
    assert window._offset_text() == f"0x{chr_start:06X}"
    assert window._slice_prefill_offset() == chr_start


# -- color editing (docs/design/palette-editing.md) ------------------------
def _open_for_color_edit(qtbot, tmp_path, monkeypatch):
    """A window on the default palette with entry 3 selected for editing."""
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._palette_panel._select(3)
    return window


def test_editing_the_default_palette_forks_a_custom_one(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_for_color_edit(qtbot, tmp_path, monkeypatch)
    assert window._palette_mode == "default"
    before_len = len(window._doc.palette)
    assert before_len == 256  # the generated default is always full length

    window._on_color_changed(0xFF123456)

    # The edit forked to a project-stored Custom palette - full length like the
    # default it forked from, so the fork changes the palette's size not at all -
    # and landed on the selected entry.
    assert window._palette_mode == "custom"
    assert window._palette_mode_combo.currentData() == "custom"
    assert len(window._doc.palette) == 256
    assert window._doc.palette.color(3) == 0xFF123456
    # A custom palette has no file behind it, so Write must never target one.
    assert window._doc.palette_config.write_enabled is False
    # ...and the entry isn't "dirty": it is saved with the project, not by Write.
    assert not window._workspace.current.pixel_dirty


def test_custom_fork_undo_peels_the_edit_then_the_fork(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_for_color_edit(qtbot, tmp_path, monkeypatch)
    stack = window._undo_stack
    original = window._doc.palette.color(3)

    window._on_color_changed(0xFF123456)
    assert stack.count() >= 2  # the fork and the edit are separate steps

    stack.undo()  # the color edit
    assert window._palette_mode == "custom"
    assert window._doc.palette.color(3) == original

    stack.undo()  # the fork itself
    assert window._palette_mode == "default"
    assert len(window._doc.palette) == 256

    stack.redo()
    stack.redo()
    assert window._palette_mode == "custom"
    assert window._doc.palette.color(3) == 0xFF123456


def test_consecutive_edits_to_one_entry_merge_into_a_step(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_for_color_edit(qtbot, tmp_path, monkeypatch)
    before_3 = window._doc.palette.color(3)
    window._on_color_changed(0xFF111111)  # forks to custom, then edits
    stack = window._undo_stack
    count = stack.count()

    # A slider drag emits on every step; the whole run collapses into the one
    # step already on the stack, exactly as consecutive view moves do.
    for value in (0xFF222222, 0xFF333333, 0xFF444444):
        window._on_color_changed(value)
    assert stack.count() == count
    assert window._doc.palette.color(3) == 0xFF444444

    # A different entry breaks the run rather than merging into it.
    window._palette_panel._select(4)
    before_4 = window._doc.palette.color(4)
    window._on_color_changed(0xFF555555)
    assert stack.count() == count + 1

    # Undo peels them in reverse: entry 4, then entry 3's entire run at once.
    stack.undo()
    assert window._doc.palette.color(4) == before_4
    stack.undo()
    assert window._doc.palette.color(3) == before_3


def test_edit_run_returning_to_its_start_leaves_no_step(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_for_color_edit(qtbot, tmp_path, monkeypatch)
    window._on_color_changed(0xFF111111)  # fork + first edit
    stack = window._undo_stack
    count = stack.count()
    start = window._doc.palette.color(3)

    window._on_color_changed(0xFF222222)
    window._on_color_changed(start)  # dragged back to where it began

    assert stack.count() == count  # the empty step dropped itself
    assert window._doc.palette.color(3) == start


def test_editing_a_file_palette_dirties_the_palette_entry(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QFileDialog

    from celpix.project.workspace import EntryKind

    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    graphic = window._workspace.current
    pal = tmp_path / "colors.pal"
    pal.write_bytes(bytes(32))  # 16 BGR555 entries, all black
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(pal), ""))
    )
    assert window._open_palette()
    assert window._palette_mode == "file"

    # Picking a .pal from the dropdown registers it in the Palettes list, linked
    # to the graphic by path — it is owned there, not by the graphic.
    palette_entry = window._workspace.find_palette(str(pal))
    assert palette_entry is not None and palette_entry.kind is EntryKind.PALETTE

    window._palette_panel._select(1)
    window._on_color_changed(0xFFFFFFFF)

    # A file palette is edited in place — no fork — and the edit belongs to the
    # PALETTE entry, while the graphic that renders it stays perfectly clean.
    assert window._palette_mode == "file"
    assert window._doc.palette.color(1) == 0xFFFFFFFF  # the graphic shows it
    assert palette_entry.doc.palette_config.write_enabled is True
    assert palette_entry.palette_dirty
    assert not graphic.palette_dirty and not graphic.pixel_dirty

    # Write the palette entry back to its own .pal (a graphic Write touches pixels
    # only). BGR555 white at entry 1 = 0x7FFF little-endian, in the second slot.
    window._write_entry_checked(palette_entry)
    assert pal.read_bytes()[2:4] == b"\xff\x7f"
    assert not palette_entry.palette_dirty


def test_editing_a_file_palette_leaves_the_graphic_untouched(
    qtbot, tmp_path, monkeypatch
) -> None:
    # A file palette lives in its own file; editing and saving it must never mark
    # the graphic dirty nor rewrite a single byte of it.
    from PySide6.QtWidgets import QFileDialog

    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    graphic_entry = window._workspace.current
    graphic = Path(graphic_entry.path)
    pal = tmp_path / "colors.pal"
    pal.write_bytes(bytes(32))
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(pal), ""))
    )
    assert window._open_palette()
    palette_entry = window._workspace.find_palette(str(pal))
    window._palette_panel._select(1)

    before = graphic.read_bytes()
    mtime = graphic.stat().st_mtime_ns
    window._on_color_changed(0xFFFFFFFF)

    assert palette_entry.palette_dirty
    assert not graphic_entry.pixel_dirty and not graphic_entry.palette_dirty

    window._write_entry_checked(palette_entry)
    # Byte-identical *and* untouched — the graphic was never in the write path.
    assert graphic.read_bytes() == before
    assert graphic.stat().st_mtime_ns == mtime
    assert not palette_entry.palette_dirty


def test_the_dock_previews_a_palette_file_read_only_with_nothing_open(
    qtbot, tmp_path
) -> None:
    """Opening a .pal with nothing else open shows its colors, and only shows them.

    A palette is written back through the document that owns it, so with no
    document there is nowhere for an edit to go: the dock displays the file and
    every write path declines rather than pretending to land somewhere. Opening
    a graphic hands the dock back to that graphic's palette.
    """
    from celpix.core.palette import FULL_PALETTE_COUNT, Palette
    from celpix.project.workspace import EntryKind, PaletteMode

    pal = tmp_path / "standalone.pal"
    # 4 BGR555 entries, none of them the default palette's colors.
    pal.write_bytes(bytes([0x1F, 0x00, 0xE0, 0x03, 0x00, 0x7C, 0xFF, 0x7F]))
    window = MainWindow()
    qtbot.addWidget(window)

    # Nothing open at all: the grid sits on the generated default rather than
    # being blank, and the load modes that need a document are disabled.
    assert window._palette_panel._colors == list(
        Palette.default(FULL_PALETTE_COUNT).colors
    )
    modes = {
        PaletteMode.parse(
            window._palette_mode_combo.itemData(i)
        ): window._palette_mode_combo.model().item(i).isEnabled()
        for i in range(window._palette_mode_combo.count())
    }
    assert modes == {
        PaletteMode.DEFAULT: True,
        PaletteMode.FILE: True,
        PaletteMode.OFFSET: False,
        PaletteMode.EMULATOR: False,
        PaletteMode.CUSTOM: False,
    }

    # Open the .pal: its colors fill the grid, and the dock names the file.
    assert window._open_palette_data(str(pal))
    entry = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    assert window._preview_palette is entry
    assert window._palette_panel._colors == list(entry.doc.palette.colors)
    # The label is middle-elided to a fixed pixel width, so the visible text is
    # font-dependent (a wider system font chops this name); the tooltip carries
    # the whole path either way.
    assert window._palette_file_label.isVisibleTo(window)
    assert Path(window._palette_file_label.toolTip()).name == "standalone.pal"
    # Previewing is display state: no undo step, and it never becomes current.
    assert window._workspace.current is None
    assert window._undo_stack.undoText() == "add palette standalone.pal"

    # Reading it is fine - the readout names the selected color.
    window._palette_panel._select(1)
    assert f"#{entry.doc.palette.color(1):08X}" in window._color_details.text()

    # Editing it is not: no editor, no undo step, no color moved.
    before = list(entry.doc.palette.colors)
    window._open_color_editor(1)
    assert window._color_editor is None
    steps = window._undo_stack.count()
    window._on_color_changed(0xFFFF0000)
    window._paste_palette_color()
    assert window._undo_stack.count() == steps
    assert list(entry.doc.palette.colors) == before
    assert not entry.palette_dirty

    # Opening a graphic takes the dock back to that graphic's own palette.
    window._load_pixel(str(_make_snes_file(tmp_path)))
    assert window._preview_palette is None
    assert window._palette_panel._colors == list(window._doc.palette.colors)


def test_a_palette_file_that_wont_decode_opens_and_import_as_re_reads_it(
    qtbot, tmp_path
) -> None:
    """A ``.pal`` records nothing about its own encoding, so the format is a
    guess - and a wrong guess must not be a dead end. 512 bytes of two-byte
    colors read as three-byte ones cannot decode at all; it opens on the sentinel
    palette, read-only so the colors we invented can't be written over the file,
    and the Format dropdown re-reads it - which with no graphic open means
    re-reading the file, there being no document to re-decode through.
    """
    from celpix.core.palette import MISSING_COLOR
    from celpix.project.workspace import EntryKind, entry_notices

    pal = tmp_path / "glyphs.pal"
    pal.write_bytes(bytes((i * 7) & 0xFF for i in range(512)))
    window = MainWindow()
    qtbot.addWidget(window)

    def pick(combo, preset_id: str) -> None:
        # Not select_combo_data: that is signal-safe by design, and this has to
        # be the user's own pick, signal and all.
        combo.setCurrentIndex(combo.findData(preset_id))

    pick(window._palette_import_preset, "preset.palette.rgb888")
    window._open_palette_data(str(pal))

    entry = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    assert window._preview_palette is entry
    assert "not a multiple of entry size 3" in window._palette_error(entry.doc)
    assert list(entry.doc.palette.colors) == [MISSING_COLOR] * 16
    assert not entry.doc.palette_config.write_enabled
    assert entry_notices(entry)  # the files row says why, not just the status line
    # Format names the palette on screen, so it landed on what the file was read
    # with rather than on the dock's default.
    assert window._palette_preset.currentData() == "preset.palette.rgb888"

    pick(window._palette_preset, "preset.palette.bgr555")

    entry = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    assert window._palette_error(entry.doc) is None
    assert len(entry.doc.palette) == 256
    assert window._palette_panel._colors == list(entry.doc.palette.colors)
    # Writability comes back with the read: the file is a real palette again.
    assert entry.doc.palette_config.write_enabled
    assert entry.palette_preset_id == "preset.palette.bgr555"
    # Import as… governs the next file read, and only that - it stayed put.
    assert window._palette_import_preset.currentData() == "preset.palette.rgb888"


def test_editing_a_file_palette_updates_every_graphic_using_it(qtbot, tmp_path) -> None:
    from celpix.project.workspace import EntryKind

    pal = tmp_path / "shared.pal"
    pal.write_bytes(bytes(32))  # 16 BGR555 entries
    px1 = _make_snes_file(tmp_path)
    px2 = tmp_path / "second.4bpp.sfc"
    px2.write_bytes(bytes((i * 7 + 3) & 0xFF for i in range(32 * 8)))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px1))
    window._add_palette_file(str(pal))
    palette_entry = next(
        e for e in window._workspace.entries if e.kind is EntryKind.PALETTE
    )
    window._use_palette_entry(palette_entry)
    first = window._workspace.current

    # A second graphic on the very same palette file.
    window._load_pixel(str(px2))
    window._use_palette_entry(palette_entry)
    second = window._workspace.current
    assert second is not first

    # Editing while viewing the second updates the palette entry - so both graphics
    # see the new color, including the one scrolled off-screen.
    window._palette_panel._select(0)
    window._on_color_changed(0xFFFFFFFF)
    assert palette_entry.doc.palette.color(0) == 0xFFFFFFFF
    assert second.doc.palette.color(0) == 0xFFFFFFFF
    assert first.doc.palette.color(0) == 0xFFFFFFFF
    assert palette_entry.palette_dirty
    assert not first.pixel_dirty and not second.pixel_dirty


def test_removing_a_used_file_palette_converts_graphics_to_custom(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QMessageBox

    from celpix.project.workspace import EntryKind, PaletteMode, palette_source_for

    pal = tmp_path / "shared.pal"
    pal.write_bytes(bytes(32))
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._add_palette_file(str(pal))
    palette_entry = next(
        e for e in window._workspace.entries if e.kind is EntryKind.PALETTE
    )
    window._use_palette_entry(palette_entry)
    graphic = window._workspace.current
    window._palette_panel._select(0)
    window._on_color_changed(0xFFFFFFFF)  # an unsaved edit - it rides into the copy
    colors_before = list(graphic.doc.palette.colors)

    # Confirm the removal; the graphic keeps the colors as its own custom palette.
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    window._remove_entry(palette_entry)

    assert window._workspace.find_palette(str(pal)) is None
    assert graphic.session.palette_mode is PaletteMode.CUSTOM
    assert list(graphic.doc.palette.colors) == colors_before
    # Re-homing the palette is a project change, not an edit to the graphic's bytes.
    assert not graphic.pixel_dirty
    src = palette_source_for(graphic)
    assert src is not None and src.colors is not None

    # Undo re-registers the palette and relinks the graphic back to it.
    window._undo_stack.undo()
    restored = window._workspace.find_palette(str(pal))
    assert restored is not None and restored.kind is EntryKind.PALETTE
    assert graphic.session.palette_mode is PaletteMode.FILE
    assert list(graphic.doc.palette.colors) == colors_before


def test_project_reload_relinks_a_file_palette_to_its_entry(qtbot, tmp_path) -> None:
    from celpix.project.workspace import EntryKind

    rom = _make_snes_file(tmp_path)
    pal = tmp_path / "s.pal"
    pal.write_bytes(bytes(32))

    # Build the project live: open the graphic, apply the palette (which registers
    # the PALETTE entry and links the graphic to it), then save.
    saver = MainWindow()
    qtbot.addWidget(saver)
    saver._load_pixel(str(rom))
    saver._add_palette_file(str(pal))
    pal_entry = next(e for e in saver._workspace.entries if e.kind is EntryKind.PALETTE)
    saver._use_palette_entry(pal_entry)
    saver._capture_session()
    project = tmp_path / "hack.celpix"
    saver._save_project_to(str(project))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_project(str(project))

    # The palette entry came back, and the graphic re-links to it in File mode.
    restored_pal = window._workspace.find_palette(str(pal))
    assert restored_pal is not None and restored_pal.kind is EntryKind.PALETTE
    graphic = window._workspace.current
    assert graphic.session.palette_mode == "file"
    assert window._palette_mode == "file"

    # And editing the reloaded palette dirties the palette entry, not the graphic.
    window._palette_panel._select(0)
    window._on_color_changed(0xFFFFFFFF)
    assert restored_pal.palette_dirty
    assert not graphic.pixel_dirty and not graphic.palette_dirty


def test_the_eyedropper_samples_without_disturbing_what_is_being_edited(
    qtbot, tmp_path, monkeypatch
) -> None:
    """Picking a color from either surface - the canvas or the palette grid - must
    leave the selection that identifies the *edited* entry where it was.

    Both surfaces are checked together because the risk they share is the whole
    point: a pick is a click, and a click on either one normally moves a
    selection. On the canvas that would reload an Offset-mode palette out from
    under the color being edited; on the grid it would retarget the edit itself.
    """
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    window = _open_for_color_edit(qtbot, tmp_path, monkeypatch)
    window._open_color_editor(3)
    panel = window._palette_panel
    picked: list[int] = []
    window._canvas.color_picked.connect(picked.append)

    window._set_pick_mode(True)
    before_selection = window._selected_tile
    window._canvas.mousePressEvent(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(2, 2),
            QPointF(2, 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )

    assert len(picked) == 1
    assert picked[0] >> 24 == 0xFF  # a real, opaque rendered pixel
    assert window._selected_tile == before_selection  # no tile got selected
    # The pick disarms itself and lands in the editor.
    assert window._color_editor.editor.color() == picked[0]

    # From the grid: entry 3 is still the one being edited - it took entry 9's color.
    source = window._doc.palette.color(9)
    window._set_pick_mode(True)
    panel.color_picked.emit(source)
    assert panel.selected_index() == 3
    assert window._doc.palette.color(3) == source


def test_custom_palette_round_trips_through_a_project(
    qtbot, tmp_path, monkeypatch
) -> None:
    from celpix.project import projectfile

    window = _open_for_color_edit(qtbot, tmp_path, monkeypatch)
    window._on_color_changed(0xFF8899AA)
    assert window._palette_mode == "custom"
    saved = list(window._doc.palette.colors)

    project = tmp_path / "p.celpix"
    window._save_project_to(str(project))  # the real path — it refreshes the session

    # The colors themselves are the stored form — there is no file behind them.
    loaded = projectfile.load_project(str(project))
    assert loaded.version == projectfile.PROJECT_VERSION
    entry = loaded.entries[0]
    assert entry.session.palette_mode == "custom"
    assert entry.pending_palette.colors == saved
    assert entry.pending_palette.path is None

    # Re-opening restores the edited palette rather than regenerating a default.
    window2 = MainWindow()
    qtbot.addWidget(window2)
    window2._load_project(str(project))
    assert window2._palette_mode == "custom"
    assert window2._doc.palette.colors == saved


def test_default_palette_is_full_length_so_the_gray_ramp_is_reachable(
    qtbot, tmp_path
) -> None:
    """The generated default runs the full 256 whatever the format's index space.

    Sized to one palette row instead (16 at 4bpp), the generator's second row —
    the grayscale ramp that makes single-channel data readable — is never
    produced at all, and the palette row spin has nowhere to step to.
    """
    from celpix.core.palette import FULL_PALETTE_COUNT

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))

    assert window._index_space() == 16  # 4bpp: one row would have stopped here
    assert len(window._doc.palette) == FULL_PALETTE_COUNT
    # ...and row 1 is reachable: the spin clamps to the palette's real rows.
    # What the ramp itself looks like is test_palette's business.
    window._palette_row.setValue(1)
    assert window._palette_row.value() == 1


# -- pinned palette regions (docs/design/palette-editing.md) ----------------
def test_a_pinned_region_renders_through_its_own_row(qtbot, tmp_path) -> None:
    """The point of the feature: two rows on screen at once.

    Asserted on the canvas image rather than on the model, because the whole
    difficulty is that one QImage carries one colour table — the row has to reach
    the screen through the *indices*, and only the rendered pixels prove it did.
    """
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))  # 8 SNES 4bpp tiles
    window._columns.setValue(8)
    window._rows.setValue(1)

    unpinned = window._canvas._image.copy()

    # Pin the second half of the sheet to row 2; the first half keeps row 0.
    window._set_linear_selection(4, 7)
    window._palette_row.setValue(2)
    window._pin_selection()
    window._palette_row.setValue(0)
    pinned = window._canvas._image

    # Tiles 0-3 are untouched, tiles 4-7 recolour.
    assert pinned.pixelColor(4, 4) == unpinned.pixelColor(4, 4)
    assert pinned.pixelColor(36, 4) != unpinned.pixelColor(36, 4)
    # And the pinned half shows exactly what row 2 would show everywhere.
    window._palette_row.setValue(2)
    all_row_2 = window._canvas._image
    assert pinned.pixelColor(36, 4) == all_row_2.pixelColor(36, 4)

    # The toggle takes it back out without discarding it.
    window._palette_row.setValue(0)
    window._show_palette_regions_action.setChecked(False)
    assert window._canvas._image.pixelColor(36, 4) == unpinned.pixelColor(36, 4)
    assert not window._palette_regions.is_empty()


def test_a_pinned_region_follows_the_picture_across_a_bit_depth_switch(
    qtbot, tmp_path
) -> None:
    """The pixel anchor, end to end through the window.

    Pin two tiles at 4bpp, reinterpret the same file at 2bpp. Both codecs cut 8x8
    tiles, so the region still covers tiles 2-3 — the colouring stays where the
    user put it on screen, even though those tiles now hold different bytes.
    """
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._set_linear_selection(2, 3)
    window._palette_row.setValue(1)
    window._pin_selection()

    window._apply_pixel_config("preset.pixel.nes-2bpp", 0)
    assert window._doc.bytes_per_tile == 16  # the bytes were re-cut...
    area = window._doc.tile_width * window._doc.tile_height
    rows = [window._palette_regions.row_at(t * area, 0) for t in range(8)]
    assert rows == [0, 0, 1, 1, 0, 0, 0, 0]  # ...the pinned tiles were not


def test_export_honours_pinned_regions_and_widens_its_table(qtbot, tmp_path) -> None:
    """A pinned export needs a table spanning every row it uses, not one row.

    Sized to the highest row actually pinned rather than blindly to 256, so a
    two-row sheet stays a small indexed PNG.
    """
    from celpix.ui import export

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._set_linear_selection(4, 7)
    window._palette_row.setValue(2)
    window._pin_selection()
    window._palette_row.setValue(0)

    image = export.document_image(window._doc, window._registry)
    # Rows 0..2 are reachable, so three 16-entry blocks - not 16 and not 256.
    assert len(image.colorTable()) == 48
    # The pinned half of the sheet carries its row in the index itself.
    assert image.pixelIndex(4, 4) < 16
    assert 32 <= image.pixelIndex(36, 4) < 48


def test_a_tile_bank_opens_at_its_own_depth_with_no_trailing_junk(
    qtbot, tmp_path
) -> None:
    """2bpp, 4bpp and 8bpp all decode into something that looks like graphics, so
    a wrong pick is plausible garbage rather than an obvious error."""
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_cgx_file(tmp_path, 0x4500, name="two.CGX")))
    entry = window._workspace.current
    assert entry.container_id == "container.scgcad-cgx"
    assert entry.session.pixel_preset_id == "preset.pixel.snes-2bpp"
    # The payload alone: the header and row table are not tiles.
    assert len(window._doc.pixel_data) == 0x4000

    window._load_pixel(str(_cgx_file(tmp_path, 0x10100, name="eight.CGX")))
    assert window._workspace.current.session.pixel_preset_id == "preset.pixel.snes-8bpp"
    assert len(window._doc.pixel_data) == 0x10000


def test_a_tile_banks_rows_seed_pinned_palette_regions(qtbot, tmp_path) -> None:
    """The file says which row each tile is meant to be read under, which is what
    pinned regions otherwise have to be told by hand."""
    rows = bytes([0, 0, 3, 3, 3, 5]) + bytes(0x3FA)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_cgx_file(tmp_path, 0x8500, rows)))

    doc = window._doc
    per_tile = doc.tile_width * doc.tile_height
    pinned = [
        (r.start // per_tile, r.length // per_tile, r.row)
        for r in doc.view.palette_regions.regions
    ]
    # Runs collapse, and row 0 is pinned like any other: the file named it, so
    # leaving it out would let those tiles drift with the palette row selector
    # while the tiles beside them stayed put.
    assert pinned[:3] == [(0, 2, 0), (2, 3, 3), (5, 1, 5)]


def test_a_tile_banks_row_base_carries_its_pinned_rows(qtbot, tmp_path) -> None:
    """A bank's row table is *relative*, and its header says what to: the two are
    one statement, so the base has to reach the rows the table seeded.

    Which is also what makes the base worth having on a pixel entry at all —
    moving it re-aims every pin at once, rather than the user re-pinning a
    thousand tiles because the palette they loaded is the other half of CGRAM.
    """
    rows = bytes([0, 0, 3]) + bytes(0x3FD)
    window = MainWindow()
    qtbot.addWidget(window)
    # col_half 1: the OBJ half, so this bank's rows count from 8 at 4bpp.
    window._load_pixel(str(_cgx_file(tmp_path, 0x8500, rows, col=(1, 0))))
    entry, doc = window._workspace.current, window._doc

    assert doc.palette_row_base == 8
    assert window._row_base.value() == 8
    assert not window._row_base.isHidden()  # a pixel entry has one too
    # Stored as the file states them, not folded in — or moving the base below
    # would move the art twice.
    per_tile = doc.tile_width * doc.tile_height
    assert doc.view.palette_regions.row_at(2 * per_tile, 0) == 3
    # ...and drawn eight rows up, tile 0 included: a bank pinned to its own row 0
    # is pinned, and follows the base like every other.
    assert window._tile_biases([0, 2]) == [8 * 16, 11 * 16]

    # The palette that got loaded is the user's answer, and it moves all of them.
    window._row_base.setValue(0)
    assert entry.palette_row_base == 0 and window._doc.palette_row_base == 0
    assert window._tile_biases([0, 2]) == [0, 3 * 16]


def test_pinning_lands_on_the_colours_that_were_selected(qtbot, tmp_path) -> None:
    """Pin takes the row from Palette Row, and Palette Row names a row of the palette on
    screen — so under a base the *stored* row is that one counted back, and what
    the pin draws is the colours the user was looking at when they pinned.

    Asserted on the canvas image, because the whole question is which sixteen
    colours reached the screen.
    """
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))  # 8 SNES 4bpp tiles
    window._columns.setValue(8)
    window._rows.setValue(1)
    area = window._doc.tile_width * window._doc.tile_height

    window._row_base.setValue(8)
    window._palette_row.setValue(11)
    all_row_11 = window._canvas._image.copy()

    window._set_linear_selection(4, 7)
    window._pin_selection()
    assert window._palette_regions.row_at(4 * area, 0) == 3  # 11, counted back
    assert "palette row 11" in window.statusBar().currentMessage()  # said as shown

    # The pinned half keeps row 11's colours when the view moves off it, and the
    # unpinned half does not.
    window._palette_row.setValue(0)
    pinned = window._canvas._image
    assert pinned.pixelColor(36, 4) == all_row_11.pixelColor(36, 4)
    assert pinned.pixelColor(4, 4) != all_row_11.pixelColor(4, 4)

    # A row the base takes below the palette's first is stored as the plain
    # difference, negative and all: what the pin has to mean is "draws through the
    # row that was picked", and that holds under either reading of the ends.
    window._palette_row.setValue(2)
    window._set_linear_selection(0, 3)
    window._pin_selection()
    assert window._palette_regions.row_at(0, 0) == 2 - 8
    assert window._tile_biases([0])[0] == 2 * 16

    # With wrapping on the same pin is stored inside the palette instead, and
    # draws through the same row — the two readings agree about what was picked.
    window._wrap_palette_rows_action.setChecked(True)
    window._pin_selection()
    assert window._palette_regions.row_at(0, 0) == 10  # (2 - 8) of sixteen rows
    assert window._tile_biases([0])[0] == 2 * 16


def test_a_project_with_its_own_regions_is_not_overwritten_by_the_file(
    qtbot, tmp_path
) -> None:
    """The file's rows are a starting point; regions the user pinned are theirs."""
    from celpix.core.paletteregions import PaletteRegion, PaletteRegions

    rows = bytes([4]) * 0x400
    path = _cgx_file(tmp_path, 0x8500, rows)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(path))
    entry = window._workspace.current
    mine = PaletteRegions.from_regions([PaletteRegion(0, 64, 7)])
    entry.doc.view.palette_regions = mine
    entry.pending_view = entry.doc.view
    entry.doc = None

    window._workspace.set_current(None)
    window._activate_entry(entry)
    assert window._doc.view.palette_regions == mine


def test_the_pinned_palette_toggles_are_two_separate_switches(qtbot, tmp_path) -> None:
    """Seeing a tile drawn through row 5 does not tell you it *is* row 5, so the
    number can be shown without the recolour and either without the other."""
    window = MainWindow()
    qtbot.addWidget(window)
    assert window._show_palette_regions_action.text() == "S&how Pinned Palette Colors"
    # The rows toggle is not the pins' alone - it numbers every named row, which
    # on a tilemap is the cells' own - so it is not named for them.
    assert window._show_palette_rows_action.text() == "Show Palette &Rows"
    assert window._show_palette_regions_action.isChecked()
    assert not window._show_palette_rows_action.isChecked()

    window._load_pixel(str(_cgx_file(tmp_path, 0x8500, bytes([3]) * 0x400)))
    assert window._canvas._palette_rows is None  # labels off by default

    window._show_palette_rows_action.setChecked(True)
    assert window._canvas._palette_rows
    assert set(window._canvas._palette_rows) == {3}

    # Turning the colours off leaves the labels: they are separate questions.
    window._show_palette_regions_action.setChecked(False)
    assert window._canvas._palette_rows is None  # nothing pinned is *applied*...
    window._show_palette_regions_action.setChecked(True)
    assert window._canvas._palette_rows


def test_the_pinned_palette_toggles_persist_as_local_preferences(
    qtbot, tmp_path
) -> None:
    """Both are about how *you* read a sheet, not about any one entry or project,
    so a fresh window comes up the way the last one was left — the grid's rule."""
    _fresh_settings(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    # The defaults, off a store nothing has written yet.
    assert window._show_palette_regions_action.isChecked()
    assert not window._show_palette_rows_action.isChecked()

    window._show_palette_regions_action.setChecked(False)
    window._show_palette_rows_action.setChecked(True)

    reopened = MainWindow()
    qtbot.addWidget(reopened)
    assert not reopened._show_palette_regions_action.isChecked()
    assert reopened._show_palette_rows_action.isChecked()
    # And the member the render cycle reads, not just the menu entry.
    assert reopened._show_palette_regions is False
    assert reopened._show_palette_rows is True


def test_wrapping_the_palette_row_base_is_off_until_asked_for(qtbot, tmp_path) -> None:
    """What a base carrying a row off the end of the palette does is a reading, so
    it is a switch — and the one that leaves a wrong base visible is the default.

    Off, the row stops at the palette's first; on, it comes round the far end. The
    same preference rule as the two beside it: app-wide, and read off the document
    rather than the window, so the export cannot disagree with the canvas.
    """
    _fresh_settings(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    assert window._wrap_palette_rows_action.text() == "&Wrap Palette Rows"
    assert not window._wrap_palette_rows_action.isChecked()

    window._load_pixel(str(_make_snes_file(tmp_path)))  # 8 SNES 4bpp tiles
    window._columns.setValue(8)
    window._rows.setValue(1)
    window._set_linear_selection(0, 3)
    window._palette_row.setValue(1)
    window._pin_selection()
    # Base -3 takes the pinned row below the palette. Off, it stops at row 0.
    window._row_base.setValue(-3)
    assert not window._doc.view.wrap_palette_rows
    assert window._tile_biases([0])[0] == 0

    # On, it comes round: sixteen rows at 4bpp, so 1 - 3 is row 14.
    window._wrap_palette_rows_action.setChecked(True)
    assert window._doc.view.wrap_palette_rows
    assert window._tile_biases([0])[0] == 14 * 16

    # And it outlives the window, like the two switches beside it.
    reopened = MainWindow()
    qtbot.addWidget(reopened)
    assert reopened._wrap_palette_rows_action.isChecked()
    assert reopened._wrap_palette_rows is True


def test_the_row_labels_mark_the_pinned_tiles_and_not_the_view(qtbot, tmp_path) -> None:
    """ "Show Pinned Palette Rows" is about what is *pinned*, so an unpinned tile
    carries no number whatever Palette Row happens to be — including when it is not 0,
    where the view's own row is otherwise indistinguishable from a pin to it."""
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))  # 8 SNES 4bpp tiles
    window._columns.setValue(8)
    window._rows.setValue(1)
    window._show_palette_rows_action.setChecked(True)

    # Pin the second half to row 2, then read the sheet through row 3 - a view row
    # that is neither 0 nor the pinned one.
    window._set_linear_selection(4, 7)
    window._palette_row.setValue(2)
    window._pin_selection()
    window._palette_row.setValue(3)

    assert window._canvas._palette_rows[:8] == [None] * 4 + [2] * 4
    # The recolour is unaffected: an unpinned tile still draws through the view's
    # row, which is what the label's absence is saying about it.
    assert window._window_biases(8, 1)[:8] == [3 * 16] * 4 + [2 * 16] * 4

    # A pin *to* the view's own row is still a pin, and still labelled: it reads
    # the same on screen, and the label is the only thing that says it is pinned.
    window._set_linear_selection(0, 1)
    window._pin_selection()
    assert window._canvas._palette_rows[:4] == [3, 3, None, None]


def test_the_palette_grid_marks_the_row_the_selection_is_pinned_to(
    qtbot, tmp_path
) -> None:
    """Where the selected tiles take their colors from, which is not the row the
    grid's own outline is on the moment anything is pinned."""
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))  # 8 SNES 4bpp tiles
    window._columns.setValue(8)
    window._rows.setValue(1)
    panel = window._palette_panel

    window._set_linear_selection(4, 7)
    window._palette_row.setValue(2)
    window._pin_selection()
    window._palette_row.setValue(0)
    assert panel._marked_row == 2  # the pin, not the view's row

    # A selection spanning two rows has no one row to mark, and neither has one
    # over tiles nobody pinned.
    window._set_linear_selection(0, 7)
    assert panel._marked_row is None
    window._set_linear_selection(0, 3)
    assert panel._marked_row is None

    # Hiding the pinned render takes the mark with it: it says where the colors
    # on screen come from, and they no longer come from there.
    window._set_linear_selection(4, 7)
    assert panel._marked_row == 2
    window._show_palette_regions_action.setChecked(False)
    assert panel._marked_row is None


def test_the_palette_grid_marks_the_row_a_selected_cell_draws_in(
    qtbot, tmp_path
) -> None:
    """A tilemap cell states its row in the file, so the same mark reads it there
    instead of asking the pinned regions - and states it *relative* to the map's
    own base, which is what the grid has to show it counted from."""
    from celpix.core.tilemap import Cell

    window, _ = _bound_tilemap(
        qtbot,
        tmp_path,
        [
            Cell(index=1, palette_row=2),
            Cell(index=2, palette_row=2),
            Cell(index=3, palette_row=5),
        ],
        maker=_pnl_file,
    )
    panel = window._palette_panel

    window._select_tiles(0, 0)
    assert panel._marked_row == 2
    window._select_tiles(2, 2)
    assert panel._marked_row == 5
    # Cells that agree still mark; a run across two rows has no one row to mark.
    window._select_tiles(0, 1)
    assert panel._marked_row == 2
    window._select_tiles(0, 2)
    assert panel._marked_row is None

    # The mark is the row the cell is *drawn* through, so the base moves it.
    window._select_tiles(0, 0)
    window._row_base.setValue(8)
    assert panel._marked_row == 10

    window._clear_selection()
    assert panel._marked_row is None


def _stamp_layout_over(qtbot, tmp_path, cells, entries):
    """A stamp layout bound to a panel of ``cells``, itself bound to a bank.

    Two hops, which is what makes the palette row worth a fixture: a layout's own
    word is a coordinate with no colour field, so every row on screen is one the
    panel states.
    """
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_pnl_file(tmp_path, cells)))
    panel = window._workspace.entries[1]
    panel.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(panel)
    window._load_pixel(str(_map_file(tmp_path, entries)))
    layout = window._workspace.current
    layout.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[1]
    )
    window._reload_tilemap(layout)
    return window, layout, panel


def test_the_palette_grid_marks_the_row_a_stamped_cell_is_drawn_in(
    qtbot, tmp_path
) -> None:
    """A chained map's row is the *source* cell's: its own word is a coordinate,
    and what reaches the screen is whatever the stamp it names carries. So the
    mark has to read the drawn cells and not the file's own, which held a 0 that
    is never a row anybody chose."""
    from celpix.core.tilemap import Cell

    cells = [Cell()] * 34
    cells[0], cells[1] = Cell(index=1, palette_row=5), Cell(index=2, palette_row=5)
    cells[32], cells[33] = Cell(index=3, palette_row=5), Cell(index=4, palette_row=5)
    window, _layout, _panel = _stamp_layout_over(
        qtbot, tmp_path, cells, [Cell(index=0)]
    )
    assert window._doc.cells[0].palette_row == 0  # the coordinate word
    assert window._doc.drawn_cells[0].palette_row == 5  # the stamp's own

    window._select_tiles(0, 0)
    assert window._palette_panel._marked_row == 5
    # And the same row is what the label over that cell says, from the one
    # computation - a mark and a number that could disagree would be worse than
    # either alone.
    window._show_palette_rows_action.setChecked(True)
    assert window._canvas._palette_rows[0] == 5


def test_the_pin_gesture_writes_a_tilemap_cells_own_palette_row(
    qtbot, tmp_path
) -> None:
    """One gesture, two stores. A pixel document has nothing in its bytes that
    could say a row, so pinning holds it in the project; a cell has the field, so
    the same gesture writes it there - through the base, clamped to what the
    field can hold, and undoable like any other cell edit."""
    from celpix.core.tilemap import Cell

    window, _bank, _entry = _bound_screen(
        qtbot, tmp_path, [Cell(index=1), Cell(index=2)]
    )
    window._select_tiles(0, 0)
    window._palette_row.setValue(3)
    window._pin_selection()
    assert window._doc.cells[0].palette_row == 3
    assert window._doc.cells[1].palette_row == 0  # only what was selected
    assert window._palette_panel._marked_row == 3

    # Stored as a *named* row, so the base is taken back off on the way in and
    # put on again on the way out - the colours picked are the colours landed on.
    window._row_base.setValue(4)
    window._palette_row.setValue(6)
    window._pin_selection()
    assert window._doc.cells[0].palette_row == 2
    assert window._palette_panel._marked_row == 6
    window._row_base.setValue(0)

    # Clamped to the field: a console BG cell spends three bits on the row, so
    # row 9 would come back as 1 if encode were left to mask it down.
    window._palette_row.setValue(9)
    window._pin_selection()
    assert window._doc.cells[0].palette_row == 7

    window._undo_stack.undo()
    assert window._doc.cells[0].palette_row == 2


def test_a_stamp_layout_has_no_palette_row_of_its_own_to_set(qtbot, tmp_path) -> None:
    """The row a stamped position draws through belongs to the panel it names, and
    that is where it is editable: the layout's own word is a coordinate with no
    colour field, so the gesture is off rather than writing a bit encode drops."""
    from celpix.core.tilemap import Cell

    window, _layout, panel = _stamp_layout_over(
        qtbot, tmp_path, [Cell(index=1, palette_row=5)] * 34, [Cell(index=0)]
    )
    window._select_tiles(0, 0)
    assert window._cell_palette_row_limit() is None
    assert not window._pin_palette_action.isEnabled()

    # The panel itself is where the row lives, so there the gesture is armed.
    window._activate_entry(panel)
    window._select_tiles(0, 0)
    assert window._cell_palette_row_limit() == 7
    assert window._pin_palette_action.isEnabled()


def _col_file(tmp_path, name="score.COL"):
    """A real-shaped S-CG-CAD palette: 0x200 of color, then its metadata block."""
    from celpix.plugins.builtins.scgcad import COL_HEADER_AT, COL_SIZE, SIGNATURE

    out = bytearray(COL_SIZE)
    # Distinguishable colors in the payload; the block after them is text and
    # would decode as 128 more "colors" if the framing were ignored.
    for i in range(256):
        out[i * 2 : i * 2 + 2] = ((i * 129) & 0x7FFF).to_bytes(2, "little")
    out[COL_HEADER_AT : COL_HEADER_AT + len(SIGNATURE)] = SIGNATURE
    path = tmp_path / name
    path.write_bytes(bytes(out))
    return path


def test_a_registered_col_reads_only_its_colors(qtbot, tmp_path) -> None:
    """Detection picks the palette container, so the entry stops at 0x200 instead
    of decoding the tool's metadata block as 128 more colors."""
    from celpix.project.workspace import EntryKind

    window = MainWindow()
    qtbot.addWidget(window)
    window._add_palette_file(str(_col_file(tmp_path)))

    entry = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    assert entry.container_id == "container.scgcad-col"
    assert window._load_palette_entry(entry)
    assert len(entry.doc.palette.colors) == 256


def test_a_palette_entrys_container_can_be_corrected(qtbot, tmp_path, monkeypatch):
    """The override a file has always had, now on a palette — and applying it
    re-reads the file and pushes the new colors onto the graphic showing them,
    which is the half a plain re-read would miss."""
    from celpix.plugins.base import RAW_CONTAINER
    from celpix.project.workspace import EntryKind
    from celpix.ui.container_dialog import ContainerEdit

    px = _make_snes_file(tmp_path)
    col = _col_file(tmp_path)

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._add_palette_file(str(col))
    entry = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    window._use_palette_entry(entry)
    assert len(entry.doc.palette.colors) == 256

    # Repick it as plain bytes, as a user correcting a wrong guess would.
    monkeypatch.setattr(
        "celpix.ui.main_window.entries.ContainerDialog.edit_container",
        lambda *a, **k: ContainerEdit(RAW_CONTAINER, entry.paths, entry.reshape_id),
    )
    window._change_container_for(entry)

    assert entry.container_id == RAW_CONTAINER
    assert len(entry.doc.palette.colors) == 512  # the block, read as color
    # The graphic renders the palette by reference, so it has to have followed.
    assert window._doc.palette.colors == entry.doc.palette.colors


def test_a_palette_is_never_offered_a_graphics_container(qtbot, tmp_path) -> None:
    """The list a palette entry is given and the list a file is given are
    disjoint, so neither can be pointed at the other's formats."""
    from celpix.core.capabilities import ContentKind
    from celpix.ui.container_dialog import ContainerDialog

    window = MainWindow()
    qtbot.addWidget(window)
    col = str(_col_file(tmp_path))

    for_palette = ContainerDialog(
        window._registry, paths=(col,), kind=ContentKind.PALETTE
    )
    qtbot.addWidget(for_palette)
    offered = set(_combo_ids(for_palette._container))
    assert "container.scgcad-col" in offered
    assert "container.ines" not in offered

    for_file = ContainerDialog(window._registry, paths=(col,), kind=ContentKind.PIXELS)
    qtbot.addWidget(for_file)
    graphics = set(_combo_ids(for_file._container))
    assert "container.ines" in graphics
    assert "container.scgcad-col" not in graphics


def test_a_palette_file_that_states_its_format_is_read_in_it(qtbot, tmp_path) -> None:
    """A TPL names its own color format, and that beats the import default.

    Every other palette file leaves the encoding to be guessed, so the dock's
    Import as... dropdown supplies one. A stated format is a fact instead, and
    read through the wrong one the colors are wrong but never obviously so - any
    two bytes decode as some color. The dropdown must land on what the file says.
    """
    from celpix.project.workspace import PaletteMode
    from celpix.ui.widgets import select_combo_data

    # Type 2 = BGR555: red, green, blue, white as 16-bit little-endian entries.
    pal = tmp_path / "stated.tpl"
    pal.write_bytes(
        b"TPL\x02" + bytes([0x1F, 0x00, 0xE0, 0x03, 0x00, 0x7C, 0xFF, 0x7F])
    )
    window = MainWindow()
    qtbot.addWidget(window)

    # Park the import default somewhere the file disagrees with, so adopting the
    # header is visible rather than a coincidence.
    select_combo_data(window._palette_import_preset, "preset.palette.rgb888")
    assert window._palette_import_preset_id() == "preset.palette.rgb888"

    window._open_palette_data(str(pal))
    entry = window._workspace.find_palette(str(pal))
    assert entry is not None
    assert entry.palette_preset_id == "preset.palette.bgr555"
    assert window._palette_mode is PaletteMode.FILE
    # Decoded as BGR555, not as the 3-byte RGB888 the dropdown was sitting on.
    assert window._palette_panel._colors[:4] == [
        0xFFFF0000,
        0xFF00FF00,
        0xFF0000FF,
        0xFFFFFFFF,
    ]


def test_a_stated_palette_format_does_not_overrule_a_chosen_one(
    qtbot, tmp_path
) -> None:
    """Only a format nobody chose gives way to the file's header.

    An entry is registered on whatever Import as... happens to be on, which is a
    setting about importing in general rather than an answer about this file. Once
    someone has said what they want, a stated header must not undo it.
    """
    from celpix.ui.widgets import select_combo_data

    pal = tmp_path / "stated.tpl"
    pal.write_bytes(
        b"TPL\x02" + bytes([0x1F, 0x00, 0xE0, 0x03, 0x00, 0x7C, 0xFF, 0x7F])
    )
    window = MainWindow()
    qtbot.addWidget(window)

    select_combo_data(window._palette_import_preset, "preset.palette.rgb888")
    window._add_palette_file(str(pal))
    entry = window._workspace.find_palette(str(pal))
    assert entry is not None
    # A deliberate pick, made before the file is ever decoded.
    entry.palette_preset_id = "preset.palette.rgb444"
    window._load_palette_entry(entry)
    assert entry.palette_preset_id == "preset.palette.rgb444"
