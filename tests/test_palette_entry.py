"""Entry-mode palettes: colours decoded out of another open entry's bytes.

The model half (the project round trip, and what a stale or ineligible position
degrades to) and the editing half (an edit reaching the ROM through whatever owns
the bytes, a colour with no owner refused, a consumer re-decoding when its source
moves, and what closing the source does). See ``docs/design/palette-editing.md``.
"""

from __future__ import annotations

import json

from celpix.project import projectfile
from celpix.project.workspace import (
    CompositePiece,
    EntrySession,
    PaletteMode,
    PaletteSource,
    Workspace,
    new_composite,
)
from celpix.ui.main_window import MainWindow

#: One BGR555 colour is two bytes, which every count here is in.
COLOR = 2
#: A 4bpp tile, the unit the pixel side of these fixtures is measured in.
TILE = 32

SWATCH = "preset.pixel.view-as-palette"
BGR555 = "preset.palette.bgr555"
RGB888 = "preset.palette.rgb888"


def _session(pixel: str = "preset.pixel.snes-4bpp") -> EntrySession:
    return EntrySession(pixel, BGR555)


def _colors(count: int, first: int = 0x1234) -> bytes:
    """``count`` two-byte colour words, each distinct."""
    return b"".join(((first + n) & 0x7FFF).to_bytes(2, "little") for n in range(count))


def _window(qtbot) -> MainWindow:
    window = MainWindow()
    qtbot.addWidget(window)
    return window


# -- the project round trip -------------------------------------------------
def test_an_entry_sourced_palette_round_trips_through_a_project(tmp_path) -> None:
    """The source is written as a **position** and resolved back to the object,
    the one place the positional form exists — so reordering the rows costs the
    binding nothing, and a consumer sitting *before* its source still finds it.
    """
    ws = Workspace()
    rom = tmp_path / "rom.bin"
    rom.write_bytes(_colors(64))
    graphic = ws.open_file(str(rom))
    graphic.session = _session()
    graphic.session.palette_mode = PaletteMode.ENTRY
    table = ws.add_slice(str(rom), "colours", 0x20, 0x20)
    table.session = _session(SWATCH)
    graphic.palette_entry = table
    graphic.pending_palette = PaletteSource(entry=table, offset=4)
    ws.set_current(graphic)
    path = tmp_path / "p.celpix"
    projectfile.save_project(ws, str(path))

    # A forward reference: the consumer is entry 0 and names entry 1.
    stored = json.loads(path.read_text())["entries"][0]
    assert stored["palette"] == {"entry": 1, "offset": 4}

    loaded = projectfile.load_project(str(path))
    restored, source = loaded.entries[0], loaded.entries[1]
    assert restored.pending_palette.entry is source
    assert restored.pending_palette.offset == 4
    assert restored.palette_entry is source

    # Reordering the rows moves the position with them rather than the binding.
    ws.reorder(table, before=graphic)
    projectfile.save_project(ws, str(path))
    document = json.loads(path.read_text())
    assert document["entries"][1]["palette"] == {"entry": 0, "offset": 4}

    # A position naming nothing usable degrades to the default palette rather
    # than to whatever now sits at a stale index.
    document["entries"][1]["palette"]["entry"] = 99
    path.write_text(json.dumps(document))
    degraded = projectfile.load_project(str(path)).entries[1]
    assert degraded.pending_palette.entry is None
    assert degraded.palette_entry is None


# -- reading -----------------------------------------------------------------
def _assembled_table(qtbot, tmp_path):
    """A window holding a ROM, two swatch slices of it and a composite joining
    them with a pad in front — the colour-RAM image a game builds by hand.

    The ROM's first row of colours sits at 0x40 and its second at 0x100, and the
    composite reproduces "two bytes of slot 0 nobody writes, then those rows".
    """
    rom = tmp_path / "rom.bin"
    data = bytearray(0x400)
    data[0x40 : 0x40 + 0x1E] = _colors(15, 0x0100)
    data[0x100 : 0x100 + 0x1E] = _colors(15, 0x0200)
    rom.write_bytes(bytes(data))
    window = _window(qtbot)
    window._load_pixel(str(rom))
    parent = window._workspace.current
    top = window._workspace.add_slice(parent.path, "row 0", 0x40, 0x1E)
    top.session = _session(SWATCH)
    bottom = window._workspace.add_slice(parent.path, "row 1", 0x100, 0x1E)
    bottom.session = _session(SWATCH)
    table = new_composite(
        "CGRAM",
        (
            CompositePiece(length=COLOR),  # the never-written slot 0
            CompositePiece(top),
            CompositePiece(bottom),
        ),
    )
    # Read as swatches, which is what makes a "tile" one colour word — so the
    # 30-byte rows join at their own length instead of being rounded up to a
    # 4bpp tile (``docs/design/composite-entry.md``).
    table.session = _session(SWATCH)
    window._workspace.entries.append(table)
    return window, parent, top, bottom, table


def test_a_palette_is_decoded_from_a_composite_of_swatch_slices(
    qtbot, tmp_path
) -> None:
    """The problem the mode exists for: a colour table the game assembles out of
    several ROM places, with a hole where the hardware writes nothing. The
    composite already expresses that; this is the graphic reading it.
    """
    window, parent, top, bottom, table = _assembled_table(qtbot, tmp_path)
    window._activate_entry(parent)

    assert window._load_palette_from_entry(table, 0, BGR555)

    assert window._palette_mode is PaletteMode.ENTRY
    assert parent.palette_entry is table
    palette = window._doc.palette
    # Slot 0 is the pad, slots 1..15 the first ROM row, 16.. the second.
    assert palette.color(0) == 0xFF000000
    assert palette.color(1) != palette.color(16)
    assert window._doc.palette_config.write_enabled is False
    # Two bytes of pad and two 15-colour rows: the join, to the byte.
    assert len(palette) == 31


def test_a_ranged_piece_places_the_colours_it_states(qtbot, tmp_path) -> None:
    """A piece's byte range addresses its source's resolved bytes, so a palette
    read through one starts where the range says rather than where the slice
    does."""
    window, parent, top, bottom, table = _assembled_table(qtbot, tmp_path)
    # Take only the last four colours of the first row.
    table.pieces = (
        CompositePiece(length=COLOR),
        CompositePiece(top, offset=0x16, length=COLOR * 4),
    )
    window._activate_entry(parent)

    assert window._load_palette_from_entry(table, 0, BGR555)

    whole = window._doc.palette
    assert len(whole) == 5
    assert whole.color(1) == 0xFF000000 or whole.color(1) != whole.color(2)


# -- editing -----------------------------------------------------------------
def test_a_colour_edit_lands_in_the_owning_slices_parent_and_undoes(
    qtbot, tmp_path
) -> None:
    """One region, one authority: the colour is encoded over the bytes it was
    read from, deposited into the composite piece that owns them, folded into
    that slice's parent — and the dirt lands on both. Undo hands each owner back
    the exact state it was in.
    """
    window, parent, top, bottom, table = _assembled_table(qtbot, tmp_path)
    window._activate_entry(parent)
    assert window._load_palette_from_entry(table, 0, BGR555)
    before_bytes = bytes(parent.doc.pixel_data)
    assert not top.pixel_dirty and not parent.pixel_dirty

    window._palette_panel.select_index(1)  # the first colour of the top row
    window._on_color_changed(0xFF00FF00)

    assert top.pixel_dirty
    assert parent.pixel_dirty
    # ...and only the piece the colour's own read unit falls in. Splicing the
    # whole window would dirty every piece it crosses and make the next Write
    # re-encode bytes nobody touched.
    assert not bottom.pixel_dirty
    assert top.doc.pixel_data[:2] != before_bytes[0x40:0x42]
    # The fold is a debt the slice records and the file pays at the next place
    # its buffer is believed — which is what showing or writing it does.
    window._settle_region(parent)
    assert parent.doc.pixel_data[0x40:0x42] == top.doc.pixel_data[:2]
    # Only that colour's own two bytes moved.
    assert parent.doc.pixel_data[0x42:0x5E] == before_bytes[0x42:0x5E]

    window._undo_stack.undo()
    assert not top.pixel_dirty
    assert not parent.pixel_dirty
    window._settle_region(parent)
    assert parent.doc.pixel_data[0x40:0x5E] == before_bytes[0x40:0x5E]


def test_a_colour_whose_bytes_are_a_pad_is_refused(qtbot, tmp_path) -> None:
    """Nothing owns a pad's bytes, so there is nowhere to deposit the edit and
    keeping it would put a colour on screen that the next reassembly takes away.
    The rule a stroke over a composite's pad already follows."""
    window, parent, top, bottom, table = _assembled_table(qtbot, tmp_path)
    window._activate_entry(parent)
    assert window._load_palette_from_entry(table, 0, BGR555)
    before = window._doc.palette.color(0)

    window._palette_panel.select_index(0)  # the never-written slot
    window._on_color_changed(0xFFFF0000)

    assert window._doc.palette.color(0) == before
    assert not top.pixel_dirty and not parent.pixel_dirty
    assert "no source for" in window.statusBar().currentMessage()


def test_editing_the_source_redecodes_a_loaded_consumer(qtbot, tmp_path) -> None:
    """A palette is a *decode* of bytes that have moved, so a pixel edit on the
    source reaches every loaded consumer — and it cannot be patched, only read
    again.

    Under the **format the consumer is on**, which is the live document's and not
    its session's: the session's copy is only written on an entry switch, so
    re-reading it would put the entry on screen back on the format it was opened
    with and silently undo the Format pick below.
    """
    from celpix.ui.widgets import select_combo_data

    window, parent, top, bottom, table = _assembled_table(qtbot, tmp_path)
    window._activate_entry(parent)
    assert window._load_palette_from_entry(table, 0, BGR555)
    select_combo_data(window._palette_preset, RGB888)
    window._reload_palette()
    assert parent.doc.palette_config.interpret_preset_id == RGB888
    colours = len(parent.doc.palette)
    before = window._doc.palette.color(1)

    # Paint the slice the first colour came from, directly. The whole swatch,
    # because the swatch codec encodes each tile's *most common* colour.
    window._activate_entry(top)
    tiles = window._decode_run(0, 1)
    copy = type(tiles[0])(tiles[0].width, tiles[0].height, bytes(tiles[0].data))
    for y in range(copy.height):
        for x in range(copy.width):
            copy.set(x, y, 0xFF0000FF)
    window._apply_tile_edit(0, [copy], "paint")

    assert parent.doc.palette.color(1) != before
    # The re-decode kept the format the user picked, and so the window it sizes.
    assert parent.doc.palette_config.interpret_preset_id == RGB888
    assert len(parent.doc.palette) == colours


def test_closing_the_source_degrades_and_undo_restores(qtbot, tmp_path) -> None:
    """A binding onto a closed entry answers "not open" rather than "somebody
    else": the consumer keeps its mode, shows the generated palette, is marked,
    and gets its colours back when the close is undone."""
    from celpix.project.workspace import palette_missing

    window, parent, top, bottom, table = _assembled_table(qtbot, tmp_path)
    window._activate_entry(parent)
    assert window._load_palette_from_entry(table, 0, BGR555)
    colours = parent.doc.palette.colors

    window._push_removal(table, None)  # the undoable close, minus its prompt

    assert window._palette_mode is PaletteMode.ENTRY  # the mode is kept
    assert palette_missing(parent)
    assert parent.doc.palette.colors != colours

    window._undo_stack.undo()

    assert not palette_missing(parent)
    assert parent.doc.palette.colors == colours


# -- where a swatch composite is filed, and what a click on it means ---------
def _section_of(panel, entry) -> str:
    """The heading ``entry``'s row is sitting under, on screen."""
    return panel._items[entry].parent().text(0)


def test_a_swatch_composite_is_filed_with_the_palettes_and_moves_with_its_format(
    qtbot, tmp_path
) -> None:
    """A join of a ROM's colour tables *is* a colour table, so its row belongs
    under Palettes — and the answer is the format's, so the row follows a switch
    of it, undo included.

    Its ``content_kind`` is untouched throughout: what files a row is a separate
    question from what its bytes are, asked in one place.
    """
    from celpix.core.capabilities import ContentKind

    window, parent, top, bottom, table = _assembled_table(qtbot, tmp_path)
    panel = window._files_panel
    panel.add_entry(table, None, None)

    assert _section_of(panel, table) == "Palettes"
    assert table.content_kind is ContentKind.PIXELS  # unchanged, and it matters

    window._activate_entry(table)
    at = window._pixel_preset.findData("preset.pixel.snes-4bpp")
    window._pixel_preset.setCurrentIndex(at)  # the real signal, not a snap

    assert _section_of(panel, table) == "Pixels"

    window._undo_stack.undo()

    assert _section_of(panel, table) == "Palettes"


def test_a_swatch_composite_applies_on_a_double_click_and_opens_on_a_single(
    qtbot, tmp_path
) -> None:
    """Filed with the palettes, it behaves like one: a click picks the row
    without moving the view — the first click of a double-click cannot open it
    and leave the gesture meaning something else — and the double-click applies
    it to the graphic on screen.

    With nothing it could be applied to, the same double-click opens it instead,
    which is the only other thing it could mean.
    """
    window, parent, top, bottom, table = _assembled_table(qtbot, tmp_path)
    panel = window._files_panel
    panel.add_entry(table, None, None)
    window._activate_entry(parent)

    item = panel._items[table]
    panel._tree.setCurrentItem(item)  # a click, as far as the tree is concerned

    assert panel.selected_entries() == [table]  # the row really is picked
    assert window._workspace.current is parent  # ...and the view stayed put

    panel._on_double_clicked(item, 0)

    assert window._palette_mode is PaletteMode.ENTRY
    assert parent.palette_entry is table
    assert window._workspace.current is parent  # still not opened

    # With the composite itself on screen there is nothing to apply it to, so
    # the same gesture is the one that opens it.
    window._activate_entry(table)
    assert window._workspace.current is table
