"""Entry activation: what opening an entry brings with it
(``docs/design/palette-editing.md`` §2)."""

from __future__ import annotations

from celpix.project.workspace import (
    CompositePiece,
    EntryKind,
    new_composite,
    slice_of,
)
from celpix.ui.main_window import MainWindow
from celpix.ui.undo_commands import AddEntryCommand

RGB888 = "preset.palette.rgb888"


def test_a_composite_reopens_a_palette_on_its_slices_format(qtbot, tmp_path) -> None:
    """A composite whose piece is a slice of a closed palette file registers the
    palette again in the format the slice reads it in, not the dock's guess —
    and opening a palette file reports colors, not swatch tiles."""
    pal = tmp_path / "colors.pal"
    pal.write_bytes(bytes(range(96)))  # 32 three-byte colours
    window = MainWindow()
    qtbot.addWidget(window)
    window._add_palette_file(str(pal), preset_id=RGB888)
    palette = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    window._activate_entry(palette)
    assert window.statusBar().currentMessage() == "Loaded 32 colors from colors.pal"
    run = slice_of(palette, "upper row", 48, 48)
    window._seed_slice_from_parent(run)
    window._push_command(AddEntryCommand(window, run, "new slice"))
    window._workspace.close(palette, with_children=False)
    table = new_composite("CGRAM", (CompositePiece(run),))
    window._workspace.insert(table, len(window._workspace.entries))
    window._activate_entry(table)
    reopened = window._workspace.find_palette(str(pal))
    assert reopened is not None
    assert reopened.palette_preset_id == RGB888


def test_removing_a_used_palette_with_an_unavailable_entry_current(
    qtbot, tmp_path, monkeypatch
) -> None:
    """The re-home reshows whatever is current, and an entry whose file is gone
    has no document to render: the removal completes and its consumer goes
    Custom, rather than the command stopping half-applied."""
    from PySide6.QtWidgets import QMessageBox

    from celpix.project.workspace import PaletteMode

    pal = tmp_path / "shared.pal"
    pal.write_bytes(bytes(32))
    graphic_file = tmp_path / "g.4bpp.sfc"
    graphic_file.write_bytes(bytes(256))
    gone = tmp_path / "m.4bpp.sfc"
    gone.write_bytes(bytes(256))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(graphic_file))
    window._add_palette_file(str(pal))
    palette = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    window._use_palette_entry(palette)
    graphic = window._workspace.current
    window._load_pixel(str(gone))
    missing = window._workspace.current
    window._activate_entry(graphic)
    window._workspace.drop_document(missing)
    gone.unlink()
    window._activate_entry(missing)
    assert window._doc is None

    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    window._remove_entry(palette)
    assert window._workspace.find_palette(str(pal)) is None
    assert graphic.session.palette_mode is PaletteMode.CUSTOM
