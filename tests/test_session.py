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
