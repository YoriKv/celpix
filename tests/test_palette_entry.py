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


def test_a_never_opened_composite_is_assembled_at_its_own_seed(qtbot, tmp_path) -> None:
    """A composite with no session yet is joined at the format its own sources
    state rather than at the consumer's: read as 4bpp tiles, its 30-byte swatch
    rows round up to 32 and every colour after the first row shifts — and the
    colours would change again the moment the composite was opened."""
    window, parent, top, bottom, table = _assembled_table(qtbot, tmp_path)
    table.session = None  # never activated, which is how a fresh paste arrives
    window._activate_entry(parent)

    assert window._load_palette_from_entry(table, 0, BGR555)

    # Two bytes of pad and two 15-colour rows, joined at their own length.
    assert len(window._doc.palette) == 31


def test_a_palette_read_from_the_consumers_own_parent_keeps_it_on_screen(
    qtbot, tmp_path
) -> None:
    """The source may be the very file the consumer is cut from, and a landing in
    a file drops every slice document below it. The consumer's cannot go: it is
    what the canvas is drawing, and the palette this edit was made on is in it."""
    rom = tmp_path / "rom.bin"
    data = bytearray(0x400)
    data[0x40 : 0x40 + 0x20] = _colors(16, 0x0100)
    rom.write_bytes(bytes(data))
    window = _window(qtbot)
    window._load_pixel(str(rom))
    parent = window._workspace.current
    cut = window._workspace.add_slice(parent.path, "gfx", 0x200, TILE * 2)
    cut.session = _session()
    window._activate_entry(cut)
    assert window._load_palette_from_entry(parent, 0x40, BGR555)
    shown = cut.doc

    window._palette_panel.select_index(1)
    window._on_color_changed(0xFF00FF00)

    assert cut.doc is shown
    assert window._doc is shown
    assert parent.pixel_dirty
    assert parent.doc.pixel_data[0x42:0x44] != bytes(data[0x42:0x44])


def test_a_tied_entry_is_written_through_its_owner_in_entry_mode(
    qtbot, tmp_path
) -> None:
    """The NES draws colour 0 of every background row out of its one backdrop
    slot, so an edit to any of them is an edit to that byte. That is a question
    about the **format**, not about this pathway being writable — an Entry palette
    is never writable and still lands its colour in the source's bytes."""
    rom = tmp_path / "rom.bin"
    rom.write_bytes(bytes(range(0x10)) * 0x40)
    window = _window(qtbot)
    window._load_pixel(str(rom))
    parent = window._workspace.current
    table = window._workspace.add_slice(parent.path, "screen palette", 0x100, 0x10)
    table.session = _session(SWATCH)
    window._activate_entry(parent)
    assert window._load_palette_from_entry(table, 0, "preset.palette.nes-screen")
    before = rom.read_bytes()[0x100:0x110]

    window._palette_panel.select_index(4)  # row 1's backdrop
    window._on_color_changed(0xFF00FF00)

    palette = window._doc.palette
    assert palette.color(0) == palette.color(4) == palette.color(8)
    # The backdrop byte carries it; the tied byte the codec never displays is
    # left exactly as it was read.
    assert table.doc.pixel_data[0] != before[0]
    assert table.doc.pixel_data[4] == before[4]


def test_an_entry_mode_offset_reads_as_a_plain_byte_offset(qtbot, tmp_path) -> None:
    """Offset mode's number is a position in this entry's own file, so it wears
    whatever bank format the navbar is set to. Entry mode's indexes another
    entry's resolved buffer from 0, and a composite's join has no CPU address for
    a bank layout to render it at — nor to read a typed one back as."""
    from celpix.core.address import BankLayout

    window, parent, top, bottom, table = _assembled_table(qtbot, tmp_path)
    window._activate_entry(parent)
    window._bank_layout = lambda: BankLayout(
        bank_size=0x8000, addr_base=0x8000, bank_base=0x80
    )

    assert window._load_palette_from_entry(table, COLOR * 2, BGR555)
    assert window._palette_offset_text() == "000004"
    assert window._parse_palette_offset("20") == 0x20

    assert window._load_palette_at_offset(0x40)
    assert window._palette_offset_text() == "80:8040"
    assert window._parse_palette_offset("80:8040") == 0x40


# -- a palette file as an entry of its own ----------------------------------
def _palette_file(tmp_path, name: str = "colors.pal", count: int = 32):
    pal = tmp_path / name
    pal.write_bytes(_colors(count))
    return pal


def test_a_palette_file_opens_as_swatches_and_a_painted_swatch_is_a_colour(
    qtbot, tmp_path
) -> None:
    """A registered palette file is a document of its own: opened, its colour
    words are swatches, and its bytes are the authority — painting one re-decodes
    the palette half, and every graphic mirroring the file takes the new colour,
    since a File palette is shown by reference.
    """
    from celpix.project.workspace import EntryKind

    pal = _palette_file(tmp_path)
    rom = tmp_path / "rom.bin"
    rom.write_bytes(bytes(range(256)) * 4)
    window = _window(qtbot)
    window._load_pixel(str(rom))
    graphic = window._workspace.current
    window._add_palette_file(str(pal), preset_id=BGR555)
    entry = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    window._use_palette_entry(entry)
    assert window._palette_mode is PaletteMode.FILE
    mirrored = list(graphic.doc.palette.colors)

    window._activate_entry(entry)
    assert window._workspace.current is entry
    assert entry.session.pixel_preset_id == SWATCH
    assert entry.doc.tile_count == 32
    # The dock shows the file's own colours, in File mode on the entry itself.
    assert window._palette_mode is PaletteMode.FILE
    assert window._palette_panel._colors == mirrored

    # Paint swatch 3 solid: a pixel edit on the palette's bytes. The whole
    # swatch, because the swatch codec encodes each tile's most common colour.
    tiles = window._decode_run(3, 1)
    copy = type(tiles[0])(tiles[0].width, tiles[0].height, bytes(tiles[0].data))
    for y in range(copy.height):
        for x in range(copy.width):
            copy.set(x, y, 0xFF0000FF)
    window._apply_tile_edit(3, [copy], "paint")

    assert entry.pixel_dirty
    assert entry.doc.palette.color(3) != mirrored[3]
    assert entry.doc.palette.color(3) == 0xFF0000FF  # decoded back from BGR555
    assert graphic.doc.palette.color(3) == entry.doc.palette.color(3)
    assert graphic.doc.palette.color(4) == mirrored[4]
    # One undo takes the bytes and the colour back, on both documents.
    window._undo_stack.undo()
    assert not entry.pixel_dirty
    assert graphic.doc.palette.color(3) == mirrored[3]


def test_a_slice_of_a_palette_file_is_a_palette_and_a_composite_piece(
    qtbot, tmp_path
) -> None:
    """The point of slicing a palette: a run of its colours is a palette source
    for a graphic (Entry mode) and a piece for a composite to assemble, and a
    colour edited through the graphic lands in the palette file's bytes by the
    fold every slice owes its parent.
    """
    from celpix.project.workspace import (
        EntryKind,
        can_supply_palette,
        is_composable,
        section_kind,
        slice_of,
    )
    from celpix.ui.undo_commands import AddEntryCommand

    pal = _palette_file(tmp_path)
    rom = tmp_path / "rom.bin"
    rom.write_bytes(bytes(range(256)) * 4)
    window = _window(qtbot)
    window._load_pixel(str(rom))
    graphic = window._workspace.current
    window._add_palette_file(str(pal), preset_id=BGR555)
    palette = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)

    # Rows 16..31 of the file, cut the way New Slice… would cut them.
    run = slice_of(palette, "upper row", 16 * COLOR, 16 * COLOR)
    window._seed_slice_from_parent(run)
    window._push_command(AddEntryCommand(window, run, "new slice"))
    assert window._workspace.parent_of(run) is palette
    assert window._workspace.children_of(palette) == [run]
    assert run.parent_kind is EntryKind.PALETTE
    assert section_kind(run, window._registry).value == "palette"  # filed under
    assert (
        window._files_panel._items[run].parent() is window._files_panel._items[palette]
    )
    assert is_composable(run) and is_composable(palette)
    assert can_supply_palette(graphic, run)
    assert not can_supply_palette(palette, graphic)  # its colours are its own

    # The graphic reads the run as its palette — the swatch codec seeds the
    # format — and an edit lands in the palette file through the slice.
    window._activate_entry(graphic)
    window._use_entry_as_palette(run)
    assert window._palette_mode is PaletteMode.ENTRY
    assert graphic.palette_entry is run
    assert len(window._doc.palette) == 16
    before = bytes(_colors(32))
    window._palette_panel.select_index(2)
    window._on_color_changed(0xFF00FF00)
    assert run.pixel_dirty and palette.pixel_dirty
    # The fold is paid where the palette's buffer is next believed — showing
    # it, here, since a registered palette nobody opened has no buffer yet.
    edited = window._doc.palette.color(2)
    window._activate_entry(palette)
    assert palette.doc.pixel_data[18 * COLOR : 19 * COLOR] != before[36:38]
    assert palette.doc.pixel_data[: 16 * COLOR] == before[:32]
    # ...and the palette file's own colours followed its bytes.
    assert palette.doc.palette.color(18) == edited

    # A composite assembled from the run reads those same bytes.
    table = new_composite("CGRAM", (CompositePiece(run),))
    window._workspace.insert(table, len(window._workspace.entries))
    window._activate_entry(table)
    assert window._workspace.current is table
    assert table.session.pixel_preset_id == SWATCH  # seeded from the run
    assert table.doc.pixel_data == palette.doc.pixel_data[16 * COLOR :]


def _file_palette_window(qtbot, tmp_path, count: int = 32):
    """A graphic reading a ``count``-colour BGR555 palette file in File mode."""
    from celpix.project.workspace import EntryKind

    pal = _palette_file(tmp_path, count=count)
    rom = tmp_path / "rom.bin"
    rom.write_bytes(bytes(range(256)) * 4)
    window = _window(qtbot)
    window._load_pixel(str(rom))
    graphic = window._workspace.current
    window._add_palette_file(str(pal), preset_id=BGR555)
    entry = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    window._use_palette_entry(entry)
    return window, pal, graphic, entry


def _paint_swatch(window, index: int, argb: int) -> None:
    """Paint swatch ``index`` of the entry on screen solid ``argb``."""
    tiles = window._decode_run(index, 1)
    copy = type(tiles[0])(tiles[0].width, tiles[0].height, bytes(tiles[0].data))
    for y in range(copy.height):
        for x in range(copy.width):
            copy.set(x, y, argb)
    window._apply_tile_edit(index, [copy], "paint")


def test_a_painted_swatch_outranks_an_earlier_colour_edit(qtbot, tmp_path) -> None:
    """The palette file's bytes are the authority, so a stroke on a swatch whose
    colour was edited — the edit undone or still standing, the stroke made on
    the file or on a slice of it — is what the dock shows and what Write puts
    on disk. Carried over the new decode, the edited
    colour would come back and a save would splice it over the paint.
    """
    from celpix.pipeline import pipeline
    from celpix.project.workspace import slice_of
    from celpix.ui.undo_commands import AddEntryCommand

    window, _pal, graphic, entry = _file_palette_window(qtbot, tmp_path)
    window._activate_entry(entry)
    red, green, blue = 0xFFFF0000, 0xFF00FF00, 0xFF0000FF

    def holds(argb: int) -> None:
        doc = entry.doc
        assert doc.palette.color(3) == argb
        assert window._palette_panel._colors[3] == argb
        assert graphic.doc.palette.color(3) == argb
        # What Write puts on disk: the pixel half and the palette splice agree.
        written = pipeline.spliced_palette_bytes(doc, window._registry)
        assert written[3 * COLOR : 4 * COLOR] == doc.pixel_data[3 * COLOR : 4 * COLOR]

    window._palette_panel.select_index(3)
    window._on_color_changed(green)
    window._undo_stack.undo()
    _paint_swatch(window, 3, blue)
    holds(blue)

    window._on_color_changed(green)  # an edit left standing
    _paint_swatch(window, 3, red)
    holds(red)

    # The same through a slice: the stroke reaches the file by the fold, paid
    # when the file is next shown.
    window._on_color_changed(green)
    run = slice_of(entry, "lower row", 0, 16 * COLOR)
    window._seed_slice_from_parent(run)
    window._push_command(AddEntryCommand(window, run, "new slice"))
    window._activate_entry(run)
    _paint_swatch(window, 3, blue)
    window._activate_entry(entry)
    holds(blue)


def test_undoing_a_colour_edit_across_a_disk_reload_reaches_the_new_document(
    qtbot, tmp_path
) -> None:
    """A reload replaces the palette file's document; the colour edit made before
    it carries across, and its undo lands on the document that replaced it — the
    colour, the bytes and every mirror together.
    """
    window, pal, graphic, entry = _file_palette_window(qtbot, tmp_path)
    original = graphic.doc.palette.color(3)
    original_bytes = pal.read_bytes()
    window._palette_panel.select_index(3)
    window._on_color_changed(0xFF00FF00)
    assert entry.doc.palette.color(3) == 0xFF00FF00

    changed = bytearray(original_bytes)
    changed[10 * COLOR : 11 * COLOR] = (0x001F).to_bytes(2, "little")  # pure red
    pal.write_bytes(bytes(changed))
    stale = entry.doc
    window._reload_from_disk([str(pal)])
    assert entry.doc is not stale
    assert entry.doc.palette.color(3) == 0xFF00FF00  # the unsaved edit carried
    assert entry.doc.palette.color(10) == 0xFFFF0000  # the file's change arrived

    window._undo_stack.undo()
    assert entry.doc.palette.color(3) == original
    assert graphic.doc.palette.color(3) == original
    assert entry.doc.pixel_data[3 * COLOR : 4 * COLOR] == original_bytes[6:8]


def test_the_swatch_format_pick_on_a_palette_file_is_its_colour_format(
    qtbot, tmp_path
) -> None:
    """On an opened palette file the toolbar's swatch format *is* the palette's
    format: the pick re-decodes the colours, stamps the entry and reaches every
    mirroring graphic, and one undo puts all of it back."""
    # 96 bytes: whole colours in both formats, so each one decodes.
    window, _pal, graphic, entry = _file_palette_window(qtbot, tmp_path, count=48)
    window._activate_entry(entry)
    colours = list(entry.doc.palette.colors)

    fmt = window._palette_view_preset
    fmt.setCurrentIndex(fmt.findData(RGB888))
    assert entry.palette_preset_id == RGB888
    assert entry.doc.palette_config.interpret_preset_id == RGB888
    assert len(entry.doc.palette) == 32  # three bytes a colour now
    assert graphic.doc.palette.colors == entry.doc.palette.colors

    window._undo_stack.undo()
    assert entry.palette_preset_id == BGR555
    assert entry.doc.palette.colors == colours
    assert graphic.doc.palette.colors == colours
