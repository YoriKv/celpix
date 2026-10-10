"""Copy / Paste View Settings (Ctrl+Shift+C / Ctrl+Shift+V)."""

from __future__ import annotations

from celpix.core.tilemap import Cell
from celpix.project.view_settings import view_settings_from_payload
from celpix.project.workspace import PaletteMode
from celpix.ui.main_window import MainWindow
from uihelpers import (
    _bound_tilemap,
    _cgx_file,
    _make_snes_file,
    _pattern_name,
    _tilemap_file,
)


def _pick(combo, data) -> None:
    """Choose ``data`` the way a click does, so the picker's own handler runs."""
    combo.setCurrentIndex(combo.findData(data))


def _rom_with_two_slices(qtbot, tmp_path):
    px = tmp_path / "rom.sfc"
    px.write_bytes(bytes((i * 13 + 1) & 0xFF for i in range(32 * 64)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    first = window._workspace.add_slice(str(px), "first", 0, 1024)
    second = window._workspace.add_slice(str(px), "second", 1024, 1024)
    return window, first, second


def test_paste_lands_formats_arrangement_compression_and_palette(
    qtbot, tmp_path
) -> None:
    window, first, second = _rom_with_two_slices(qtbot, tmp_path)
    window._activate_entry(first)
    _pick(window._pixel_preset, "preset.pixel.snes-2bpp")
    window._pattern.setCurrentIndex(
        window._pattern.findText(_pattern_name("genesis-sprite"))
    )
    _pick(window._compression, "compression.lz1")
    assert window._load_palette_at_offset(64)
    window._palette_row.setValue(1)
    window._copy_view_settings_action.trigger()

    window._activate_entry(second)
    window._nav_tiles(3)  # off the origin, so the landing is checked
    position = window._byte_position()
    before = window._pixel_preset_id()
    steps = window._undo_stack.count()
    assert window._paste_view_settings_action.isEnabled()
    window._paste_view_settings_action.trigger()

    # One step, and the view stays on the byte it was standing on.
    assert window._undo_stack.count() == steps + 1
    assert window._workspace.current is second
    assert window._byte_position() == position
    assert window._pixel_preset_id() == "preset.pixel.snes-2bpp"
    view = window._doc.view
    assert (view.block_columns, view.block_rows, view.block_order) == (2, 2, "column")
    assert window._compression_id() == "compression.lz1"
    assert window._palette_mode is PaletteMode.OFFSET
    assert window._doc.palette_config.source.offset == 64
    assert window._palette_row.value() == 1

    window._undo_stack.undo()
    assert window._pixel_preset_id() == before
    assert window._palette_mode is PaletteMode.DEFAULT
    assert window._doc.view.block_columns == 1
    # Pasting what is already there costs no step.
    window._undo_stack.redo()
    steps = window._undo_stack.count()
    window._paste_view_settings_action.trigger()
    assert window._undo_stack.count() == steps


def test_pixel_settings_leave_a_tilemaps_cell_format(qtbot, tmp_path) -> None:
    window, entry = _bound_tilemap(qtbot, tmp_path, [Cell(index=1), Cell(index=2)])
    bank = window._workspace.entries[0]
    cells = window._tilemap_preset_id(entry)
    window._activate_entry(bank)
    _pick(window._compression, "compression.lz1")
    window._copy_view_settings_action.trigger()

    window._activate_entry(entry)
    window._paste_view_settings_action.trigger()
    assert window._compression_id() == "compression.lz1"
    assert window._tilemap_preset_id(entry) == cells
    assert window._doc.is_tilemap
    assert "only fit pixels" in window.statusBar().currentMessage()


def test_a_tilemap_takes_the_copied_cell_format(qtbot, tmp_path) -> None:
    window, entry = _bound_tilemap(qtbot, tmp_path, [Cell(index=1), Cell(index=2)])
    assert window._tilemap_preset_id(entry) == "preset.tilemap.snes-bg"
    window._copy_view_settings_action.trigger()
    combo = window._tilemap_preset
    combo.setCurrentIndex(combo.findData("preset.tilemap.snes-bg-16x16"))
    window._on_tilemap_preset_change(combo.currentIndex())
    assert window._tilemap_preset_id(entry) == "preset.tilemap.snes-bg-16x16"

    window._paste_view_settings_action.trigger()
    assert window._tilemap_preset_id(entry) == "preset.tilemap.snes-bg"
    assert window._doc.is_tilemap
    window._undo_stack.undo()
    assert window._tilemap_preset_id(entry) == "preset.tilemap.snes-bg-16x16"


def test_an_opened_file_reads_like_the_entry_on_screen(qtbot, tmp_path) -> None:
    from celpix.plugins.builtins.scgcad import CGX_BANKS

    window, first, _second = _rom_with_two_slices(qtbot, tmp_path)
    window._activate_entry(first)
    _pick(window._pixel_preset, "preset.pixel.snes-2bpp")
    window._pattern.setCurrentIndex(
        window._pattern.findText(_pattern_name("genesis-sprite"))
    )
    assert window._load_palette_at_offset(64)
    window._palette_row.setValue(1)

    window._load_pixel(str(_make_snes_file(tmp_path)), inherit=True)
    assert window._workspace.current.name == "s.4bpp.sfc"
    assert window._pixel_preset_id() == "preset.pixel.snes-2bpp"
    view = window._doc.view
    assert (view.block_columns, view.block_rows, view.block_order) == (2, 2, "column")
    assert window._palette_mode is PaletteMode.OFFSET
    assert window._doc.palette_config.source.offset == 64
    assert window._palette_row.value() == 1

    # A file that states its own depth keeps it; the rest is still handed on.
    window._load_pixel(str(_cgx_file(tmp_path)), inherit=True)
    assert window._pixel_preset_id() == CGX_BANKS[0x8500][1]
    assert window._palette_mode is PaletteMode.OFFSET


def test_an_opened_map_takes_the_cell_format_on_screen(qtbot, tmp_path) -> None:
    from celpix.core.capabilities import ContentKind

    cells = [Cell(index=1), Cell(index=2)]
    window, entry = _bound_tilemap(qtbot, tmp_path, cells)
    combo = window._tilemap_preset
    combo.setCurrentIndex(combo.findData("preset.tilemap.snes-bg-16x16"))
    window._on_tilemap_preset_change(combo.currentIndex())

    raw = _tilemap_file(tmp_path, cells)
    window._load_pixel(str(raw), content_kind=ContentKind.TILEMAP, inherit=True)
    opened = window._workspace.current
    assert opened is not entry and opened.name == "screen.bin"
    assert window._tilemap_preset_id(opened) == "preset.tilemap.snes-bg-16x16"


def test_a_malformed_payload_pastes_nothing() -> None:
    assert view_settings_from_payload({"version": 99}) is None
    assert view_settings_from_payload({"version": 1, "kind": "pixels"}) is None
    settings = view_settings_from_payload(
        {
            "version": 1,
            "kind": "pixels",
            "compression_id": "compression.none",
            "pixel_preset_id": "preset.pixel.snes-4bpp",
            "arrangement": {"block_columns": -3, "block_order": "sideways"},
            "palette": {"preset_id": "preset.palette.bgr555", "mode": "?", "row": 2},
        }
    )
    assert settings is not None
    assert settings.arrangement.block_columns == 1
    assert settings.arrangement.block_order == "row"
    assert settings.palette.mode is PaletteMode.DEFAULT
