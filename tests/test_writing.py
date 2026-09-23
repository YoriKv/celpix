"""Writing an entry: what a save leaves behind for the other entries on the
same file (``docs/design/palette-editing.md`` §2)."""

from __future__ import annotations

from celpix.project.workspace import EntryKind, PaletteMode, slice_of
from celpix.ui.main_window import MainWindow
from celpix.ui.undo_commands import AddEntryCommand

BGR555 = "preset.palette.bgr555"
#: One BGR555 colour is two bytes.
COLOR = 2


def test_writing_a_slice_of_a_palette_keeps_the_mirrored_palette_live(
    qtbot, tmp_path
) -> None:
    """A write into a palette file drops its document as stale, but a graphic
    renders that document's colours by reference and the dock edits them there:
    it is decoded again at once, so the palette stays writable."""
    pal = tmp_path / "colors.pal"
    pal.write_bytes(b"".join((0x1234 + n).to_bytes(2, "little") for n in range(32)))
    rom = tmp_path / "rom.bin"
    rom.write_bytes(bytes(range(256)) * 4)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(rom))
    graphic = window._workspace.current
    window._add_palette_file(str(pal), preset_id=BGR555)
    palette = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    window._use_palette_entry(palette)
    assert window._palette_mode is PaletteMode.FILE
    colors = list(graphic.doc.palette.colors)

    run = slice_of(palette, "upper row", 16 * COLOR, 16 * COLOR)
    window._seed_slice_from_parent(run)
    window._push_command(AddEntryCommand(window, run, "new slice"))
    window._activate_entry(run)
    assert window._write_entry(run)

    assert palette.doc is not None
    assert graphic.doc.palette is palette.doc.palette  # mirrored afresh
    assert list(graphic.doc.palette.colors) == colors
    # The dock's edits reach the palette entry, not the graphic's mirror.
    window._activate_entry(graphic)
    assert window._palette_doc() is palette.doc
