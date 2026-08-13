"""Fontmaps: reading cells as words, and the text window that
writes them."""

from __future__ import annotations

from celpix.core.font import HOLE, TEMPLATES, Glyph, GlyphRole
from celpix.ui.font_alphabet_window import COL_CODE, COL_ROLE, COL_TEXT
from celpix.ui.main_window import MainWindow
from uihelpers import _make_snes_file

# The uppercase run the editor offers as a template, which is what most of these
# fonts are written in. Named here so a test says "the shipped run" rather than
# re-typing twenty-six letters.
UPPER = TEMPLATES[0][2]


# -- fontmaps: a tilemap whose cells are characters -------------------------
def _fontmap(
    qtbot,
    tmp_path,
    codes,
    *,
    chars=UPPER,
    base=0,
    named=(),
    preset="preset.tilemap.text-8bit",
):
    """A window with a font bank at entry 0 and a text run bound to it.

    The run is carved by hand rather than detected, which is the ordinary way a
    string region is reached: nothing in a ROM says "a text run starts here".
    """
    from celpix.core.capabilities import ContentKind
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    bank = window._workspace.entries[0]
    # Ticked whether or not there is a table yet: these sheets *are* fonts, and
    # what varies between the tests is what they have been told to spell.
    bank.use_as_font = True
    bank.font_chars = chars
    bank.font_base = base
    bank.font_codes = tuple(named)

    path = tmp_path / "text.bin"
    path.write_bytes(bytes(codes))
    window._load_pixel(str(path), content_kind=ContentKind.TILEMAP)
    entry = window._workspace.current
    entry.tilemap_preset_id = preset
    window._rebind_tiles(entry, TileSource(mode=TileMode.ENTRY, entry=bank))
    return window, bank, entry


def test_a_fontmap_reads_its_cells_as_words(qtbot, tmp_path) -> None:
    """The whole feature in one line: cells that are character codes read as text.

    Nothing about the *picture* changes - a text run draws as the grid of glyph
    tiles its cells already are - so what has to be true is that the second
    reading exists and agrees with the cells (``docs/design/fontmap-entry.md``).
    """
    # "CAB" under the shipped uppercase run, then a code the font has no glyph for.
    window, _bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1, 0xF0])

    doc = window._doc
    assert doc is not None and doc.is_fontmap and doc.is_tilemap
    assert doc.text.body == "CAB[$F0]"
    # The alphabet is the *font's*, reached through the binding rather than
    # stored on the map.
    assert doc.font_alphabet is not None
    assert window._text_available()


def test_a_dictionary_code_off_the_sheet_draws_as_the_characters_it_spells(
    qtbot, tmp_path
) -> None:
    """A compression-table index is not a tile number, so it is not drawn as one.

    ALTTP spends ``$88``-``$E8`` on ``the``, ``you`` and a hundred more above a
    128-tile font: there is nothing at ``$E3`` for the sheet to draw, and the
    cell renders as whatever lies past the end of the font. Drawing it as the
    glyphs it stands for is the only reading of that cell that is a picture of
    anything (``docs/design/fontmap-entry.md`` §5).

    What it costs is that a drawn position is no longer a cell, which is the half
    everything touching the canvas has to agree on.
    """
    # An eight-tile sheet, so $0A is off it. "C", then the code for "BAD", then "B".
    window, _bank, _entry = _fontmap(
        qtbot, tmp_path, [2, 0x0A, 1], chars="ABCDE", named=(Glyph(0x0A, "BAD"),)
    )
    doc = window._doc

    assert [cell.index for cell in doc.laid_out_cells] == [2, 1, 0, 3, 1]
    assert doc.drawn_positions == 5
    # Inward: every position the word covers is the one cell the file has there,
    # so an edit through any of them changes $0A.
    assert [doc.cell_at(at) for at in range(5)] == [0, 1, 1, 1, 2]
    # And outward, which is the direction the text window's caret travels.
    assert doc.drawn_span(1, 2) == (1, 4)

    # Nothing about the file moved: the cells, and the string, are what they were.
    assert [cell.index for cell in doc.cells] == [2, 0x0A, 1]
    assert doc.text.body == "CBADB"


def test_a_dictionary_code_the_sheet_can_draw_is_left_alone(qtbot, tmp_path) -> None:
    """The picture the file describes wins over the reading of it.

    A font that really has a ``th`` ligature tile has one, and spelling the code
    out would replace a correct glyph with a wrong pair of them. So it is the
    tile range that decides, not the role.
    """
    window, _bank, _entry = _fontmap(
        qtbot, tmp_path, [2, 3, 1], chars="ABCDE", named=(Glyph(3, "BAD"),)
    )
    doc = window._doc

    assert doc.text.body == "CBADB"  # it still *says* the three characters
    assert [cell.index for cell in doc.laid_out_cells] == [2, 3, 1]
    assert doc.drawn_positions == 3


def _stacked_font(qtbot, tmp_path, codes, **kwargs):
    """A fontmap whose font's Pattern reads its 8 tiles as four 8x16 glyphs.

    Two columns of 1x2 interleaved blocks, which is the layout an 8x16 font sheet
    is stored in — tops row, then the matching bottoms row. So glyph 0 is tiles
    0 and 2, glyph 1 is 1 and 3, glyph 2 is 4 and 6, glyph 3 is 5 and 7
    (``docs/design/fontmap-entry.md`` §4).
    """
    from celpix.ui.widgets import select_combo_data

    window, bank, entry = _fontmap(qtbot, tmp_path, codes, **kwargs)
    window._activate_entry(bank)
    window._columns.setValue(2)
    window._block_cols.setValue(1)
    window._block_rows.setValue(2)
    select_combo_data(window._block_order, "row-interleave")
    # Through the view change, not by writing the document: the widgets are the
    # authority and a refresh writes them over anything set behind its back.
    window._on_view_change()
    return window, bank, entry


def test_a_font_whose_pattern_stacks_tiles_draws_each_code_as_its_block(
    qtbot, tmp_path
) -> None:
    """An 8x16 glyph is two tiles, and the sheet's Pattern is what says which two.

    A font sheet is only legible once its arrangement is set, and an arrangement
    that makes it legible is one whose blocks *are* the characters — so that is
    where the fact is taken from rather than from a field of its own
    (``docs/design/fontmap-entry.md`` §4). The code is then a **block** number,
    which is what lets a mapping no base index can express — ALTTP's
    ``((c & $F0) << 1) | (c & $0F)`` — come out of ordinary placement.
    """
    from celpix.core.tilemap import Cell

    window, _bank, entry = _stacked_font(qtbot, tmp_path, [2, 0, 1], chars="ABCD")
    doc = entry.doc

    assert doc.glyph_layout is not None
    assert doc.cell_tiles == (1, 2)
    # Eight tiles, four glyphs: the bound is in the unit the codes count in.
    assert (doc.tile_count, doc.glyph_count) == (8, 4)
    assert [doc.cell_tile_indices(Cell(index=c)) for c in range(4)] == [
        [0, 2],
        [1, 3],
        [4, 6],
        [5, 7],
    ]
    # And the reading is untouched: what a code *says* is the table's business
    # and has nothing to do with how many tiles draw it.
    assert doc.text.body == "CAB"


def test_the_alphabet_editor_lists_one_row_per_glyph_not_per_tile(
    qtbot, tmp_path
) -> None:
    """The sheet and the table are the same run, so both count in glyphs.

    A row per *tile* over a stacked font would offer eight codes where the font
    has four letters, and put every character after the first on the wrong one —
    the sheet is what the codes index into, and under a grouping a code indexes a
    block.
    """
    window, _bank, _entry = _stacked_font(qtbot, tmp_path, [2, 0, 1], chars="ABCD")
    window._refresh_view()

    image, ids, cell_px, _columns = window._font_sheet()
    assert list(ids) == [0, 1, 2, 3]
    assert cell_px == (8, 16)  # one glyph, not one tile
    assert image.height() >= 16
    assert window._font_alphabet._table.rowCount() == 4


def test_changing_a_fonts_pattern_re_letters_the_strings_drawn_through_it(
    qtbot, tmp_path
) -> None:
    """The Pattern stops being display-only on a font sheet, and has to say so.

    The audience for it is every open string bound to the sheet, exactly as an
    alphabet edit's is: the fact is the font's, so a second fontmap still reading
    the old grouping is as wrong as the first
    (:meth:`~celpix.ui.main_window.session.SessionMixin._resync_glyph_layouts`).
    """
    from celpix.core.tilemap import Cell

    window, bank, entry = _stacked_font(qtbot, tmp_path, [2, 0, 1], chars="ABCD")
    assert entry.doc.cell_tile_indices(Cell(index=1)) == [1, 3]

    # Back to one tile per glyph, on the sheet — and the string follows without
    # being reloaded or even looked at.
    window._activate_entry(bank)
    window._block_rows.setValue(1)
    window._on_view_change()

    assert entry.doc.glyph_layout is None
    assert entry.doc.cell_tiles == (1, 1)
    assert entry.doc.cell_tile_indices(Cell(index=1)) == [1]
    assert entry.doc.glyph_count == 8


def test_use_as_font_stays_hidden_on_a_map_through_a_toolbar_relayout(
    qtbot, tmp_path
) -> None:
    """The tick is the sheet's question, and a map must not be asked it.

    Hiding the *checkbox* was not enough to keep it away: a toolbar wraps a
    widget it is handed in a QWidgetAction and shows that widget again on its
    next layout pass, so the tick came back on the first window resize — offering
    a declaration a map cannot make, on the entry least able to make it.
    """
    window, bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="ABCDE")
    window.show()

    window._activate_entry(bank)  # the sheet: the tick is its question to answer
    qtbot.waitUntil(lambda: not window._use_as_font.isHidden())

    window._activate_entry(entry)  # the fontmap drawn through it
    window.resize(700, 600)
    qtbot.waitUntil(lambda: window._use_as_font.isHidden())
    window.resize(1100, 760)
    qtbot.waitUntil(lambda: window._use_as_font.isHidden())


def test_a_text_cell_format_takes_use_as_font_off_the_map(qtbot, tmp_path) -> None:
    """A map reads its cells *through* a font and cannot also be one.

    The tick is offered on a pixels entry alone, so what this clears is stale —
    an older project, a hand-edited file — but the moment the format says the
    cells are text is the moment it can be said to be wrong. One step with the
    switch, so an undo puts both back.
    """
    window, _bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="ABCDE")
    entry.tilemap_preset_id = "preset.tilemap.gb-bg"  # a grid map, for now
    entry.use_as_font = True
    window._activate_entry(entry)
    assert not entry.is_font_sheet  # inert on a map, whatever the flag says

    combo = window._tilemap_preset
    at = combo.findData("preset.tilemap.text-8bit")
    combo.setCurrentIndex(at)
    window._on_tilemap_preset_change(at)

    assert entry.use_as_font is False
    window._undo_stack.undo()
    assert (entry.use_as_font, entry.tilemap_preset_id) == (
        True,
        "preset.tilemap.gb-bg",
    )


def test_a_fontmap_with_no_alphabet_still_opens_and_says_so(qtbot, tmp_path) -> None:
    """Hex is the honest reading of codes nothing has explained, and the window
    is where the user is told which control fixes it - so it must not be hidden
    in the one state that most needs explaining."""
    window, _bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="")

    doc = window._doc
    assert doc.is_fontmap and doc.font_alphabet is None
    assert doc.text.body == "[$02][$00][$01]"
    assert window._text_available()  # gated on the declaration, not the alphabet
    _status, badge = window._text_status(None)
    assert badge is not None and badge.text == "no alphabet"


def test_typing_in_the_text_window_lands_on_the_cells(qtbot, tmp_path) -> None:
    """A committed string becomes cell indices, as one undoable step.

    The attributes are the load-bearing half: a cell carries a palette row and
    flips the text form cannot show, so an edit replaces the *index* and leaves
    the rest of the cell alone. Rebuilding cells from the string would zero them.
    """
    from dataclasses import replace

    window, _bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1])
    # Give the middle cell an attribute the text has no way to express.
    entry.doc.cells[1] = replace(entry.doc.cells[1], palette_row=3, flip_h=True)

    window._on_text_committed("BAT")

    cells = window._doc.cells
    assert [cell.index for cell in cells] == [1, 0, 19]  # B, A, T
    assert cells[1].palette_row == 3 and cells[1].flip_h  # carried, not rebuilt
    # And it is one step on the shared stack, so Ctrl+Z brings the string back.
    window._undo_stack.undo()
    assert [cell.index for cell in window._doc.cells] == [2, 0, 1]


def test_text_past_the_end_of_its_region_is_cut_off_and_said_out_loud(
    qtbot, tmp_path
) -> None:
    """A text region is a fixed run of cells and the codes have nowhere else to go,
    so what runs past the end comes off the end.

    Refusing the whole string instead would leave the canvas drawing the old one
    while the window showed the new, which reads as the editor having stopped
    working. What is owed is the *warning*: the words that came off are text the
    user will look for later and not find, and they need to know before the run of
    typing goes any further.
    """
    window, _bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1])

    window._on_text_committed("MUCH LONGER")

    assert [cell.index for cell in window._doc.cells] == [12, 20, 2]  # "MUC"
    assert "'H LONGER' pushed off the end" in window.statusBar().currentMessage()


def test_a_character_the_font_lacks_is_written_as_a_blank(qtbot, tmp_path) -> None:
    """The edit still lands, with the gap on the picture where the glyph is missing.

    Refusing the write instead is the failure this guards: one character the font
    cannot spell would leave the canvas drawing the string as it was before, and
    keep it there for every keystroke after, which reads as the editor having
    stopped working. The warning is owed either way and is on the status bar and
    the badge - what the user must not lose is the rest of what they typed.
    """
    window, _bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1])

    window._on_text_committed("A%B")  # '%' is not in the uppercase run

    # 36 is the space of the shipped uppercase run, and the cells either side of
    # it are where the user typed them - not shuffled up by the missing glyph.
    assert [cell.index for cell in window._doc.cells] == [0, 36, 1]
    assert "has no code in this font" in window.statusBar().currentMessage()
    _status, badge = window._text_status("A%B")
    assert badge is not None and badge.text == "1 not in font"


def test_the_alphabet_editor_writes_to_the_bound_font(qtbot, tmp_path) -> None:
    """The window edits an entry other than the one on screen, and means to.

    Two fontmaps over one font must not be able to disagree about what its tiles
    spell, so the answer is stored once, on the font - and changing it re-reads
    every open map drawn through it, not only the one on screen.
    """
    window, bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="")
    window._refresh_view()
    # Reached through the binding: the map is current, the font is what is edited.
    assert window._font_entry() is bank
    assert window._font_alphabet_available()
    assert entry.doc.text.body == "[$02][$00][$01]"

    window._on_font_alphabet_edited(0, 0, 0, UPPER, (), "edit font alphabet")

    assert bank.font_chars == UPPER
    assert entry.doc.font_alphabet is not None
    assert entry.doc.text.body == "CAB"


def test_base_code_slides_the_run_without_moving_the_picture(qtbot, tmp_path) -> None:
    """The origin control, and the one thing no font sheet can tell you.

    The run states which characters the glyphs are and in what order - readable
    off the sheet - but where it *starts* lives in the game's code
    (``docs/graphics-formats-reference/text-formats.md`` §3.2). So an unknown ROM
    is dialled, not guessed, and what moves is the *reading* alone: the cells and
    the tiles they draw are untouched.
    """
    # The same "CAB", but written by a game whose uppercase run begins at $80.
    window, bank, entry = _fontmap(qtbot, tmp_path, [0x82, 0x80, 0x81])
    assert entry.doc.text.body == "[$82][$80][$81]"
    drawn = [cell.index for cell in entry.doc.cells]

    window._on_font_alphabet_edited(0x80, 0, 0, UPPER, (), "set base code to $80")

    assert entry.doc.text.body == "CAB"
    # The reading moved; the cells did not, so neither did the picture.
    assert [cell.index for cell in entry.doc.cells] == drawn
    # And it types back to the codes the file actually holds.
    assert list(entry.doc.font_alphabet.encode("CAB").codes) == [0x82, 0x80, 0x81]


def test_a_named_code_beats_the_run_and_does_not_move_with_it(qtbot, tmp_path) -> None:
    """The two halves of a font's own table, and why they are two.

    The run is positional - it is the sheet read straight off - so the origin
    moves it. A named code was read out of the *stream* at the value it actually
    has, so moving it would shift a terminator the user took out of the file.
    """
    window, bank, entry = _fontmap(
        qtbot,
        tmp_path,
        [0x80, 0x02],
        base=0x80,
        named=(Glyph(0x02, "wait", GlyphRole.CONTROL),),
    )

    # $80 is the run's first slot; $02 would have been "C" had the run reached it,
    # and is a named control instead — read as its name, which is the whole point
    # of naming one.
    assert entry.doc.text.body == "A[wait]"
    assert [g.text for g in entry.doc.font_alphabet.commands] == ["wait"]

    # Dialling the origin moves the run off $80 and leaves the named code where
    # the file put it.
    window._on_font_alphabet_edited(
        0, 0, 0, bank.font_chars, bank.font_codes, "set base code to $0"
    )
    assert entry.doc.text.body == "[$80][wait]"
    assert [g.text for g in entry.doc.font_alphabet.commands] == ["wait"]


def test_an_unticked_font_is_not_read(qtbot, tmp_path) -> None:
    """Use as Font is the declaration, and the apply path honours it.

    Asked of the apply rather than of the tick's own gesture, because this is the
    state an **undo** puts back: the gesture deletes the table (see
    ``test_unticking_use_as_font_deletes_the_table_and_undo_brings_it_back``),
    while the command lands whatever state it is handed, unticked and spelled
    included, and the codes must read as hex either way.
    """
    window, bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1])
    assert entry.doc.text.body == "CAB"

    window._on_font_alphabet_edited(0, 0, 0, UPPER, (), "edit font alphabet")
    bank.use_as_font = False
    window._apply_font_alphabet(bank, (False, 0, 0, 0, UPPER, ()))

    assert entry.doc.text.body == "[$02][$00][$01]"


def test_an_undeclared_sheet_has_no_alphabet_to_edit(qtbot, tmp_path) -> None:
    """The editor is gated on the tick, both ways in.

    An editor over an unticked sheet would be a table nothing reads: what the
    user needs told is *why* the codes are hex, and that is the text window's
    badge. So the window is not offered on either route - over the fontmap or
    over the sheet itself.
    """
    window, bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1])
    bank.use_as_font = False

    window._activate_entry(entry)
    window._refresh_view()
    assert window._font_entry() is None
    assert not window._font_alphabet_available()
    assert not window._font_alphabet_action.isEnabled()
    assert not window._font_alphabet.isVisible()

    window._activate_entry(bank)
    window._refresh_view()
    assert window._font_entry() is None


def _binding_gesture(qtbot, tmp_path, codes):
    """A fontmap and an undeclared sheet, with the Tiles combo about to be set.

    The bind goes through the combo rather than through ``_rebind_tiles``,
    because what is under test is the *gesture* - the prompt hangs off the one
    moment the user has said which sheet they mean.
    """
    from celpix.core.capabilities import ContentKind

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    bank = window._workspace.entries[0]
    bank.font_chars = UPPER  # typed up, but never declared

    path = tmp_path / "text.bin"
    path.write_bytes(bytes(codes))
    window._load_pixel(str(path), content_kind=ContentKind.TILEMAP)
    entry = window._workspace.current
    entry.tilemap_preset_id = "preset.tilemap.text-8bit"
    window._refresh_tilemap_bar()
    combo = window._tile_binding
    combo.setCurrentIndex(combo.findText(bank.name))
    return window, bank, entry, combo


def test_binding_a_fontmap_offers_to_declare_the_sheet_a_font(
    qtbot, tmp_path, confirmations
) -> None:
    """The bind is the moment the user has named the sheet, so it is when to ask.

    The tick lives on the *sheet*, and a fontmap bound to an undeclared one shows
    hex with nothing on its own bar to explain it - so the question is put here
    rather than leaving the user to find the other entry and answer it there.
    Two undo steps, the shape a bind from file already has.
    """
    window, bank, entry, combo = _binding_gesture(qtbot, tmp_path, [2, 0, 1])
    confirmations.yes = True

    window._on_tile_binding_change(combo.currentIndex())

    assert confirmations.asked
    assert bank.use_as_font
    assert entry.tile_source is not None and entry.tile_source.entry is bank
    assert window._doc.text.body == "CAB"

    window._undo_stack.undo()  # the bind
    assert bank.use_as_font
    window._undo_stack.undo()  # the declaration
    assert not bank.use_as_font


def test_declining_the_declaration_calls_the_binding_off(qtbot, tmp_path) -> None:
    """Declining is a real answer: the bank may be art a text-format map reads.

    Landing the bind anyway would leave a string drawn through a sheet nothing
    can spell, so the gesture is dropped whole and the combo goes back to what it
    was bound to before.
    """
    window, bank, entry, combo = _binding_gesture(qtbot, tmp_path, [2, 0, 1])
    depth = window._undo_stack.count()

    window._on_tile_binding_change(combo.currentIndex())  # the fixture cancels

    assert not bank.use_as_font
    assert entry.tile_source is None
    assert window._undo_stack.count() == depth
    assert window._tile_binding.currentIndex() == 0  # (none), put back


def test_an_alphabet_edit_is_one_undo_step(qtbot, tmp_path) -> None:
    """Everything the editor settles travels as one state, so one gesture is one
    step - and an undo puts the whole table back, not part of it."""
    window, bank, entry = _fontmap(qtbot, tmp_path, [0x82, 0x80, 0x81], chars="")

    window._on_font_alphabet_edited(0x80, 0, 0, UPPER, (), "paste 43 characters")

    assert (bank.font_base, bank.font_chars) == (0x80, UPPER)
    assert entry.doc.text.body == "CAB"
    window._undo_stack.undo()
    assert (bank.font_base, bank.font_chars) == (0, "")
    assert entry.doc.text.body == "[$82][$80][$81]"


def test_the_punctuation_comes_from_the_font_and_the_cell_format_states_none(
    qtbot, tmp_path
) -> None:
    """The whole alphabet is the font's — letters and punctuation alike — and a
    cell format states no codes at all (``docs/design/fontmap-entry.md`` §3).

    Both halves pinned together, because the second is what makes the first
    load-bearing: the terminator has to reach the text through the bank's own
    named codes, and a preset that names codes anyway must change nothing. Here
    the preset's claims are deliberately *wrong* — a break on the letter ``A``
    and a name for ``$FF`` — so honouring any of them would show.
    """
    from celpix.core.errors import Stage
    from celpix.core.font import Glyph, GlyphRole
    from celpix.plugins.base import Preset

    window, _bank, entry = _fontmap(
        qtbot,
        tmp_path,
        [2, 0, 1, 0xFF, 0],
        named=(
            Glyph(0xFF, "line-break", GlyphRole.BREAK),
            Glyph(
                0x2A,
                "wait-for-input",
                GlyphRole.CONTROL,
                "Holds until the player presses a button.",
            ),
        ),
    )
    window._registry.register_preset(
        Preset(
            id="preset.tilemap.text-with-controls",
            name="Text run, controls the reader ignores",
            stage=Stage.INTERPRET_TILEMAP,
            engine_id="codec.tilemap.packed",
            params={
                "layout": "text",
                "bytes": 1,
                "fields": "iiii iiii",
                "controls": [
                    {"code": 0x00, "name": "not-a-letter", "role": "break"},
                    {"code": 0xFF, "name": "from-the-preset"},
                ],
            },
        )
    )
    entry.tilemap_preset_id = "preset.tilemap.text-with-controls"
    entry.doc = None
    window._activate_entry(window._workspace.entries[0])
    window._activate_entry(entry)

    doc = window._doc
    # $FF is the font's line break and $00 is still an "A", which is both claims
    # at once: the preset named neither and could not have.
    assert doc.text.body == "CAB\nA"
    # ...and it types back to exactly the bytes it came from.
    assert list(doc.font_alphabet.encode(doc.text.body).codes) == [2, 0, 1, 0xFF, 0]
    assert doc.font_alphabet.decode([0x2A]).body == "[wait-for-input]"
    assert [g.text for g in doc.font_alphabet.commands] == [
        "line-break",
        "wait-for-input",
    ]
    # And the sentence behind the name reaches the insert row, which is the only
    # place a reader would look for it.
    assert [g.description for g in doc.font_alphabet.commands] == [
        "",
        "Holds until the player presses a button.",
    ]


def _typing(qtbot, tmp_path, codes, at, **kwargs):
    """A fontmap with its text window open and the caret at ``at``."""
    window, bank, entry = _fontmap(qtbot, tmp_path, codes, **kwargs)
    window._show_text()
    window._text.select_range(at, at)
    return window, entry


def test_a_keystroke_replaces_one_cell_and_the_budget_never_moves(
    qtbot, tmp_path
) -> None:
    """The whole editing model in one gesture: the field is typed *over*, not
    typed into.

    A text region is a fixed run of cells and the codes have nowhere else to go,
    so a key that inserted would push the string past the end of its region on
    the second word (``docs/design/fontmap-entry.md`` §5).
    """
    window, _entry = _typing(qtbot, tmp_path, [2, 0, 1], 1)  # "CAB", caret after C

    qtbot.keyClicks(window._text._edit, "X")

    assert window._text.body == "CXB"
    assert [cell.index for cell in window._doc.cells] == [2, 23, 1]  # C X B


def test_typing_on_a_code_replaces_the_whole_pair(qtbot, tmp_path) -> None:
    """A ``[$F0]`` is one cell however wide it reads, so it costs one keystroke to
    type over - and leaving ``$F0]`` behind would put three letters nobody typed
    into the string."""
    window, _entry = _typing(qtbot, tmp_path, [2, 0xF0, 1], 1)  # "C[$F0]B"

    qtbot.keyClicks(window._text._edit, "A")

    assert window._text.body == "CAB"
    assert [cell.index for cell in window._doc.cells] == [2, 0, 1]


def test_a_code_is_written_only_once_it_is_finished(qtbot, tmp_path) -> None:
    """Inside a ``[...]`` the overtyping stops: the user is spelling a number and
    half of one is not a code, so nothing reaches the cells until the caret leaves.

    The budget line keeps up all the same - a code being typed still costs the one
    cell it will become, and a readout frozen until the ``]`` would read as the
    keys having been ignored.
    """
    window, _entry = _typing(qtbot, tmp_path, [2, 0, 1], 1)  # "CAB", caret after C
    field = window._text._edit

    qtbot.keyClicks(field, "[$F")

    assert window._text.body == "C[$FB"  # the `A` went in the first keystroke
    assert [cell.index for cell in window._doc.cells] == [2, 0, 1]  # nothing written
    qtbot.keyClicks(field, "0]")
    assert [cell.index for cell in window._doc.cells] == [2, 0xF0, 1]

    # An undo is the one refresh that takes the field back off the user: leaving
    # a draft standing over a step they just reverted would offer to write it
    # again the next time focus moved.
    window._undo_stack.undo()
    assert window._text.body == "CAB"


def test_a_run_of_typing_is_one_undo_step(qtbot, tmp_path) -> None:
    """Every keystroke reaches the cells at once, so the canvas follows the caret -
    but Ctrl+Z has to take back the word, not the letter. What ends a run is the
    user doing something else, which is why the window is what says so."""
    window, _entry = _typing(qtbot, tmp_path, [2, 0, 1], 0)
    depth = window._undo_stack.index()

    qtbot.keyClicks(window._text._edit, "EDA")

    assert [cell.index for cell in window._doc.cells] == [4, 3, 0]
    assert window._undo_stack.index() == depth + 1
    window._undo_stack.undo()
    assert [cell.index for cell in window._doc.cells] == [2, 0, 1]

    # A click, an arrow key or a command button breaks the run: what came after
    # the break is its own step.
    window._undo_stack.redo()
    window._text.break_run()
    window._text.select_range(0, 0)
    qtbot.keyClicks(window._text._edit, "B")
    assert window._undo_stack.index() == depth + 2
    window._undo_stack.undo()
    assert [cell.index for cell in window._doc.cells] == [4, 3, 0]


def test_an_undo_hands_back_the_caret_along_with_the_string(qtbot, tmp_path) -> None:
    """A text edit is made *somewhere*, so the place comes back with it.

    Without it Ctrl+Z restores the word and leaves the caret at the far end of a
    string that no longer has what was typed there - so carrying on typing means
    first finding the spot again, in a field where a keystroke replaces whatever
    it lands on.
    """
    window, _entry = _typing(qtbot, tmp_path, [2, 0, 1], 0)  # "CAB", caret at the head

    qtbot.keyClicks(window._text._edit, "ED")
    assert window._text._edit.textCursor().position() == 2

    window._undo_stack.undo()

    assert window._text.body == "CAB"
    # The head of the run, not the end of it: one step took back both letters.
    assert window._text._edit.textCursor().position() == 0
    window._undo_stack.redo()
    assert window._text._edit.textCursor().position() == 2


def test_a_letter_typed_over_itself_is_still_a_step(qtbot, tmp_path) -> None:
    """The cells stand still and the caret does not, and the caret is part of the
    step.

    A key that lands the character already there is a thing the user did, and one
    Ctrl+Z is what they will press to take it back - the alternative is a
    keystroke that silently does nothing and an undo that reaches past it into
    the word before. Nothing was written, though, so the entry must not start
    reading dirty over it.
    """
    window, entry = _typing(qtbot, tmp_path, [2, 0, 1], 1)  # "CAB", caret after C
    depth = window._undo_stack.index()
    entry.pixel_saved_revision = entry.pixel_revision  # as if it were just saved

    qtbot.keyClicks(window._text._edit, "A")  # the letter already in that cell

    assert [cell.index for cell in window._doc.cells] == [2, 0, 1]  # nothing moved
    assert window._undo_stack.index() == depth + 1
    assert not entry.pixel_dirty  # a caret is not an unsaved change

    window._undo_stack.undo()
    assert window._text._edit.textCursor().position() == 1


def test_a_fontmap_selects_in_runs_where_every_other_map_selects_rectangles(
    qtbot, tmp_path
) -> None:
    """A sentence is a run, and the width it wraps at is a view setting.

    Every other tilemap is edited as the picture it draws, so a drag there corners
    a rectangle of cells. A fontmap's cells are text: a phrase runs off the end of
    a canvas row and carries on at the start of the next, and a rectangle over
    that names the middle of three lines and no whole word at all.
    """
    from celpix.ui.main_window.selection import SelectionShape

    window, _bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1, 4, 3, 0])
    window._columns.setValue(3)

    assert window._selection_shape.currentData() is SelectionShape.LINEAR
    assert not window._selection_shape.isEnabled()  # so S cannot swap it either

    # The second cell, dragged onto the row below: the whole run between them,
    # where a rectangle would have taken the two-wide block and skipped cell 2.
    window._on_slots_selected(1, 4)
    assert window._selected_cells() == [1, 2, 3, 4]

    # The same cells read as an ordinary grid map are back to a rectangle: what
    # forces the shape is the reading, not the entry.
    combo = window._tilemap_preset
    at = combo.findData("preset.tilemap.gb-bg")
    combo.setCurrentIndex(at)
    window._on_tilemap_preset_change(at)
    assert window._selection_shape.currentData() is SelectionShape.RECT


def test_the_canvas_and_the_text_mirror_one_anothers_selection(qtbot, tmp_path) -> None:
    """Both ways, because both are questions a user asks of a string.

    Finding a word in the text is how they find it on the canvas; picking tiles
    out of the picture is how they ask what those tiles say. A cell that reads as
    a whole ``[$F0]`` carries all five of its characters either way, so the two
    selections cover the same thing rather than the same length.
    """
    from PySide6.QtGui import QTextCursor

    window, _entry = _typing(qtbot, tmp_path, [2, 0, 1, 0xF0, 4], 0)  # "CAB[$F0]E"
    field = window._text._edit
    assert window._text.body == "CAB[$F0]E"

    # Canvas → text: three cells picked out of the picture, and the code among
    # them reads out whole.
    window._on_slots_selected(1, 3)
    cursor = field.textCursor()
    assert (cursor.selectionStart(), cursor.selectionEnd()) == (1, 8)

    # Text → canvas, and the way back does not bounce: the selection the canvas
    # was just given must not be pushed back over the one the user made here.
    cursor.setPosition(3)
    cursor.setPosition(9, QTextCursor.MoveMode.KeepAnchor)
    field.setTextCursor(cursor)
    assert window._selection_tiles() == [3, 4]
    assert (field.textCursor().selectionStart(), field.textCursor().selectionEnd()) == (
        3,
        9,
    )

    # A bare caret still names its own cell, and stays where it was put - a cell
    # is coarser than a caret, so a round trip would drag it to the head of the
    # code it is standing in the middle of.
    cursor.setPosition(5)  # inside the [$F0]
    field.setTextCursor(cursor)
    assert window._selection_tiles() == [3]
    assert field.textCursor().position() == 5


def test_the_mirror_holds_where_a_cell_is_not_one_canvas_slot(qtbot, tmp_path) -> None:
    """The caret's answer travels through three numberings, and two of them moved.

    A character comes from a cell, a cell is drawn at one or more **positions**,
    and the canvas selects **tiles**. Both of the last two steps used to be
    identity on every fontmap there was, so both were invisible: a dictionary
    code that spells out is several positions to one cell, and an 8x16 glyph is
    two tiles to one position. Skip either and the highlight lands short of the
    word by exactly that ratio.

    Driven through the real cursor, so what is exercised is the signal path the
    user's arrow key takes rather than the handler on its own.
    """
    from PySide6.QtGui import QTextCursor

    def pick(window, first, last):
        field = window._text._edit
        cursor = field.textCursor()
        cursor.setPosition(first)
        cursor.setPosition(last, QTextCursor.MoveMode.KeepAnchor)
        field.setTextCursor(cursor)

    # Two tiles per glyph: "CAB" over an 8x16 font, so cell 1 is slots 2 and 3.
    window, _bank, entry = _stacked_font(qtbot, tmp_path, [2, 0, 1], chars="ABCD")
    window._activate_entry(entry)
    window._show_text()
    assert window._doc.tiles_per_cell == 2

    pick(window, 1, 2)  # the "A"
    assert window._selection_tiles() == [2, 3]
    assert window._selected_cells() == [1]

    # And several positions per cell: "BAD" is the one cell $0A, drawn as three.
    window, _entry = _typing(
        qtbot, tmp_path, [2, 0x0A, 1], 0, chars="ABCDE", named=(Glyph(0x0A, "BAD"),)
    )
    assert window._text.body == "CBADB"

    pick(window, 1, 4)  # the whole word
    assert window._selection_tiles() == [1, 2, 3]
    assert window._selected_cells() == [1]

    # A caret inside it names the same cell and the same three slots: the word is
    # one thing to point at, however many glyphs draw it.
    pick(window, 2, 2)
    assert window._selection_tiles() == [1, 2, 3]


def test_backspace_blanks_a_whole_code_and_leaves_the_length_alone(
    qtbot, tmp_path
) -> None:
    """The delete keys walk the same pieces the typing does, and for the same two
    reasons.

    A whole code goes in one press because half of one is not something the string
    can hold. And it is **blanked, not removed**: closing the gap would pull the
    rest of the string a cell to the left and leave the tail holding whatever the
    file had there, which is a second edit the user did not ask for. Blanking
    changes the one cell they were looking at.
    """
    from PySide6.QtCore import Qt

    window, _entry = _typing(qtbot, tmp_path, [2, 0xF0, 1], 6)  # "C[$F0]B", after `]`

    qtbot.keyClick(window._text._edit, Qt.Key.Key_Backspace)

    assert window._text.body == "C B"
    assert [cell.index for cell in window._doc.cells] == [2, 36, 1]  # 36 is the space
    # The caret is left on the space it made, so backspace held down walks left
    # through the string instead of standing still.
    assert window._text._edit.textCursor().position() == 1


def test_the_insert_switch_gives_back_an_ordinary_text_field(qtbot, tmp_path) -> None:
    """The mode that has to be asked for: every key in it spends a cell the region
    has not got spare, and what falls off the far end to pay for it is a word the
    user wrote.

    On, the string grows and shrinks like any text field's - and the region takes
    up the difference at its end, cutting text off or filling with the blank, so
    that what the canvas draws is always the string the window shows.
    """
    from PySide6.QtCore import Qt

    window, _entry = _typing(qtbot, tmp_path, [2, 0, 1], 1)  # "CAB", caret after C
    field = window._text._edit
    # Off until asked for, every session: nothing restores it (see the switch).
    assert not window._text.inserting
    window._text._insert_mode.setChecked(True)

    qtbot.keyClicks(field, "X")

    # Nothing was replaced - the string grew, and the `B` it pushed past the end
    # of the region came off there and was reported.
    assert window._text.body == "CXA"
    assert [cell.index for cell in window._doc.cells] == [2, 23, 0]
    assert "'B' pushed off the end" in window.statusBar().currentMessage()

    # And Backspace removes rather than blanking in place: `A` is pulled along and
    # the cell it gave up at the end is filled with the blank.
    qtbot.keyClick(field, Qt.Key.Key_Backspace)
    assert window._text.body == "CA "
    assert [cell.index for cell in window._doc.cells] == [2, 0, 36]

    # The room that made has to still be there on the next keystroke, which is
    # what a preserved tail would have taken away.
    qtbot.keyClicks(field, "Z")
    assert [cell.index for cell in window._doc.cells] == [2, 25, 0]  # C Z A


def test_the_field_keeps_its_own_editing_out_of_the_way(qtbot, tmp_path) -> None:
    """celPix types over the string and ``QPlainTextEdit`` types into it, so the
    widget's own editing - undo included - is switched off rather than left live
    as a second editor with different rules writing to the same buffer."""
    from PySide6.QtCore import Qt

    window, _entry = _typing(qtbot, tmp_path, [2, 0, 1], 0)

    assert not window._text._edit.isUndoRedoEnabled()
    # Ctrl+Z in a `Qt.Tool` window never reaches the Edit menu's shortcut, so the
    # window forwards it to the one session history.
    with qtbot.waitSignal(window._text.undo_requested):
        qtbot.keyClick(
            window._text._edit, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier
        )


def test_the_wrap_switch_folds_lines_to_the_window(qtbot, tmp_path) -> None:
    """A local preference, not project state: whether a long string is folded to
    the window or laid out as the file's own lines says how you are reading it."""
    from PySide6.QtWidgets import QPlainTextEdit

    from celpix.ui.text_window import WORD_WRAP_KEY
    from celpix.ui.widgets import load_bool_setting

    window, _entry = _typing(qtbot, tmp_path, [2, 0, 1], 0)
    field = window._text._edit
    assert field.lineWrapMode() == QPlainTextEdit.LineWrapMode.NoWrap

    window._text._wrap.setChecked(True)

    assert field.lineWrapMode() == QPlainTextEdit.LineWrapMode.WidgetWidth
    assert load_bool_setting(WORD_WRAP_KEY, False)


def test_a_paste_costs_the_cells_it_is_worth_and_no_more(qtbot, tmp_path) -> None:
    """A drop, a middle click and the menu's Paste all arrive through one Qt hook,
    and all three would otherwise write straight into the buffer - leaving the
    window's idea of the string an edit behind and every offset after it wrong.

    What lands is the same overtype a keystroke is, counted in pieces: two
    characters replace two cells, so the region cannot grow by pasting into it.
    """
    from PySide6.QtCore import QMimeData

    window, _entry = _typing(qtbot, tmp_path, [2, 0, 1, 0, 2], 0)  # "CABAC"
    source = QMimeData()
    source.setText("ED")

    window._text._edit.insertFromMimeData(source)

    assert window._text.body == "EDBAC"
    assert [cell.index for cell in window._doc.cells] == [4, 3, 1, 0, 2]


def test_a_fontmap_opens_its_text_window_and_a_close_stops_it(qtbot, tmp_path) -> None:
    """The canvas can only ever draw a string as the grid of glyph tiles it is, so
    the text window is not a second look at something already legible - it is the
    reading, and landing on a string with the words a menu item away is the state
    the entry exists to avoid.

    Closing it is the one gesture that says otherwise, and it has to stick: a
    window that came back on the next refresh would be unclosable.
    """
    window, bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1])
    window._refresh_view()
    assert window._text.isVisible()

    window._text.close()
    window._refresh_view()
    assert not window._text.isVisible()

    # Leaving the fontmap and coming back does not undo the close - being put
    # away because the entry is not a fontmap says nothing about wanting it.
    window._activate_entry(bank)
    window._activate_entry(entry)
    assert not window._text.isVisible()

    # Asking for it is what re-arms the automatic opening, since the only way to
    # have turned it off was to have closed the window.
    window._show_text()
    window._activate_entry(bank)
    window._activate_entry(entry)
    assert window._text.isVisible()


def test_typing_over_a_selection_blanks_all_of_it_first(qtbot, tmp_path) -> None:
    """Selecting a phrase and typing is how a string gets rewritten, and what it
    costs must not depend on how much of it you have typed so far.

    The whole selection is blanked to spaces and the typing restarts at its head -
    the same gesture Backspace makes, for the same reason. Replacing it with only
    the first letter would pull the tail four cells left and leave the file's own
    bytes showing at the end of the region.
    """
    window, _entry = _typing(qtbot, tmp_path, [2, 0, 1, 0, 2], 0)  # "CABAC"
    window._text.select_range(1, 4)  # "ABA", three pieces

    qtbot.keyClicks(window._text._edit, "X")

    assert window._text.body == "CX  C"
    assert [cell.index for cell in window._doc.cells] == [2, 23, 36, 36, 2]
    # And the caret is on the first cell still to be replaced, so the rest of the
    # word carries on typing over the blanks it just made.
    assert window._text._edit.textCursor().position() == 2
    qtbot.keyClicks(window._text._edit, "YZ")
    assert [cell.index for cell in window._doc.cells] == [2, 23, 24, 25, 2]


def test_an_undo_settles_the_budget_line_on_what_it_restored(qtbot, tmp_path) -> None:
    """The readout has to describe the string that is on screen, and which string
    that is - the file's or the draft the window kept - is the window's decision.

    Computed before it had made that decision, an undo left the budget of the
    draft it had just taken away standing over the text it put back.
    """
    window, _entry = _typing(qtbot, tmp_path, [2, 0, 1], 0)  # "CAB"
    field = window._text._edit
    qtbot.keyClicks(field, "X")  # typed over: one step on the stack
    qtbot.keyClicks(field, "[")  # and a code opened, which the file has not got

    assert window._text.body == "X[B"
    # Two, not one: the code being spelled costs its cell like any other piece,
    # and a budget that only counted it once it was finished would be stale for
    # exactly as long as the user was typing it.
    assert "2 / 3 cells" in window._text._status.currentMessage()

    window._undo_stack.undo()

    assert window._text.body == "CAB"
    assert "3 / 3 cells" in window._text._status.currentMessage()


def test_a_font_with_no_space_fills_freed_cells_with_zero(qtbot, tmp_path) -> None:
    """A region is always exactly full, so a string typed shorter has to leave
    *something* in the cells it gave up - and a font drawn without a space has no
    text for them.

    Zero rather than the nearest letter that happens to exist: a cell nobody chose
    the contents of should read as a cell nobody chose the contents of, and the
    hex it comes back as says so.
    """
    from PySide6.QtCore import Qt

    # No space anywhere in the run.
    window, bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="ABCDEFGHIJ")
    entry.doc = None
    window._activate_entry(bank)
    window._activate_entry(entry)
    assert window._doc.text.body == "CAB"

    window._text.select_range(3, 3)
    window._text._insert_mode.setChecked(True)
    qtbot.keyClick(window._text._edit, Qt.Key.Key_Backspace)

    assert [cell.index for cell in window._doc.cells] == [2, 0, 0]


def test_the_canvas_marks_where_a_fontmaps_lines_end(qtbot, tmp_path) -> None:
    """The picture's only account of where the strings stop.

    A text run draws as a grid of glyph tiles whatever its punctuation, so the
    canvas shows nothing of the line structure - and on a flag-terminated format
    there is not even an odd-looking cell to notice, since the terminator is a
    letter like any other (``docs/design/fontmap-entry.md`` §5).
    """
    # "CAB" with the bit set on the B, then another A: one line end, mid-run.
    window, _bank, entry = _fontmap(
        qtbot,
        tmp_path,
        [2, 0, 1 | 0x80, 0],
        preset="preset.tilemap.text-8bit-flag",
    )
    doc = window._doc
    assert doc.text.body == "CAB\nA"
    assert window._line_end_slots() == {2}

    # It follows the cells, not the load: typing the break away takes the mark
    # with it, on the same refresh that redraws the glyphs.
    window._on_text_committed("CABA")
    assert entry.doc.text.body == "CABA"
    assert window._line_end_slots() == frozenset()


def test_nothing_is_marked_on_a_map_that_is_not_a_fontmap(qtbot, tmp_path) -> None:
    """The mark is the fontmap's alone. Every other cell format's bit 7 means
    something else entirely - a palette row, a flip, a priority - and ruling a
    cell edge on a screen would be claiming a break in a picture that has none."""
    window, _bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1 | 0x80, 0])
    assert not window._doc.laid_out_cells[2].ends_line  # no field to hold it
    assert window._line_end_slots() == frozenset()


def test_a_command_button_puts_its_code_in_the_string(qtbot, tmp_path) -> None:
    """The insert row's whole purpose, and the reason the switch beside it is not
    called ``_insert``: an attribute of that name would shadow the method these
    buttons bind to, and every click would raise instead of typing anything."""
    from celpix.ui.text_window import TextWindow

    window = TextWindow()
    qtbot.addWidget(window)
    window.show_text(
        "t", "AB", (0, 1), [("line-break", "[line-break]", "Ends the line.")], "", None
    )

    button = window._guide_row.itemAt(0).widget()
    # The caption names it, the token is what lands, and the description is the
    # format author's own sentence about the code.
    assert button.text() == "line-break"
    assert button.toolTip() == "Insert [line-break]\nEnds the line."

    with qtbot.waitSignal(window.committed):
        button.click()
    # Typed over the piece the caret was on, one cell for one cell, like a key.
    assert window.body == "[line-break]B"


def test_the_command_buttons_fold_to_the_width_instead_of_scrolling(qtbot) -> None:
    """Every command stays on screen: a narrow window costs another row of
    buttons, never a name the user has to scroll sideways to find."""
    from celpix.ui.text_window import TextWindow

    window = TextWindow()
    qtbot.addWidget(window)
    window.show_text(
        "t", "AB", (0, 1), [(f"cmd{n}", f"[cmd{n}]", "") for n in range(8)], "", None
    )
    grid, guide = window._guide_row, window._guide

    def rows() -> set[int]:
        return {grid.getItemPosition(at)[0] for at in range(grid.count())}

    guide.resize(2000, guide.height())
    guide._reflow()  # the width is handed down by the layout on a real show
    assert grid.count() == 8  # every button placed, and each of them once
    assert rows() == {0}
    one_row = guide.sizeHint().height()

    guide.resize(160, guide.height())
    guide._reflow()
    assert grid.count() == 8
    assert len(rows()) > 1
    # And the fold is paid for in height, which is what the window is asked for.
    assert guide.sizeHint().height() > one_row


def test_retyping_a_lines_last_letter_does_not_unend_the_line(qtbot, tmp_path) -> None:
    """The terminator bit belongs to the **cell**, not to the letter carrying it.

    That cell decodes to two things - the letter and the break - and typing over
    the letter took both, which on the canvas ran the line into the next one and
    moved every glyph after it. Backspace did the same. Only Enter, and Backspace
    on the break itself, may move a break.
    """
    from PySide6.QtCore import Qt

    # "CAB\nCAB": the B at cell 2 carries the bit.
    window, _entry = _typing(
        qtbot,
        tmp_path,
        [2, 0, 1 | 0x80, 2, 0, 1],
        2,  # the caret on the B
        preset="preset.tilemap.text-8bit-flag",
    )
    field = window._text._edit
    assert window._doc.text.body == "CAB\nCAB"

    qtbot.keyClicks(field, "D")
    assert window._doc.text.body == "CAD\nCAB"
    assert window._line_end_slots() == {2}

    # Blanking it is the same rule: the letter goes, the line still ends there.
    window._text.select_range(3, 3)
    qtbot.keyClick(field, Qt.Key.Key_Backspace)
    assert window._doc.text.body == "CA \nCAB"
    assert window._line_end_slots() == {2}

    # And the break itself comes off on its own, taking no letter and freeing no
    # cell - the only way to unend the line.
    window._text.select_range(4, 4)
    qtbot.keyClick(field, Qt.Key.Key_Backspace)
    assert window._doc.text.body == "CA CAB"
    assert [cell.index for cell in window._doc.cells] == [2, 0, 0x24, 2, 0, 1]
    assert window._line_end_slots() == frozenset()


def test_enter_costs_no_cell_where_a_break_is_a_bit(qtbot, tmp_path) -> None:
    """So it inserts rather than overtypes, even though overtyping is the mode.

    The rule overtyping keeps is that the region's length never moves, and a
    break that is a bit on the character before it does not move it. Overtyping
    one spent a cell on something free: the letter under the caret was eaten and
    the whole rest of the string pulled a cell left, which is the loudest way a
    text region can appear to break.
    """
    from PySide6.QtCore import Qt

    window, _entry = _typing(
        qtbot,
        tmp_path,
        [2, 0, 1, 2, 0, 1],
        3,
        preset="preset.tilemap.text-8bit-flag",
    )
    before = [cell.index for cell in window._doc.cells]

    qtbot.keyClick(window._text._edit, Qt.Key.Key_Return)

    assert window._doc.text.body == "CAB\nCAB"
    # Not one index moved - the break is the bit, and the bit was free.
    assert [cell.index for cell in window._doc.cells] == before
    assert window._line_end_slots() == {2}


def _pair_typing(qtbot, tmp_path, codes, at):
    """A fontmap whose font spells ``TH`` with the single code $50.

    The dictionary a fixed-size text region actually uses: one byte standing for
    a run of characters (``docs/design/fontmap-entry.md`` §4), which is what
    ALTTP's 97 dictionary codes are.
    """
    return _typing(
        qtbot, tmp_path, codes, at, named=(Glyph(0x50, "TH", GlyphRole.TEXT),)
    )


def test_a_pair_typed_a_letter_at_a_time_lands_on_the_one_cell_it_costs(
    qtbot, tmp_path
) -> None:
    """T then H is **one** cell, not two, because the font has a code for the pair.

    The encoder matches the longest spelling first, so the second keystroke does
    not write a second cell - it rewrites the first and frees one. The region is
    a fixed run and stays exactly full, so what the freed cell costs is the rest
    of the string sliding up behind the pair and a blank at the end.
    """
    #  "  FOX", caret at the head. 36 is the run's space.
    window, _entry = _pair_typing(qtbot, tmp_path, [36, 36, 5, 14, 23], 0)
    field = window._text._edit

    qtbot.keyClicks(field, "T")
    assert [cell.index for cell in window._doc.cells] == [19, 36, 5, 14, 23]

    qtbot.keyClicks(field, "H")
    assert [cell.index for cell in window._doc.cells] == [0x50, 5, 14, 23, 36]
    # And the field is left showing what the file now says, rather than the
    # shorter string that was typed to get there.
    assert window._doc.text.body == "THFOX "
    assert window._text.body == "THFOX "


def test_backspace_between_a_pairs_letters_takes_the_whole_pair(
    qtbot, tmp_path
) -> None:
    """A pair is one cell from either side of it and from inside it.

    Blanking only the half in front of the caret left the other half standing as
    a letter of its own, which costs a second cell the region has not got: the
    string grew by one and the last character of the region was pushed off the
    end and lost.
    """
    from PySide6.QtCore import Qt

    window, _entry = _pair_typing(qtbot, tmp_path, [0x50, 2, 0, 1], 1)  # "THCAB"
    field = window._text._edit
    assert window._doc.text.body == "THCAB"

    qtbot.keyClick(field, Qt.Key.Key_Backspace)
    assert window._text.body == " CAB"
    assert [cell.index for cell in window._doc.cells] == [36, 2, 0, 1]

    # And nothing behind the caret is nothing to take back.
    before = [cell.index for cell in window._doc.cells]
    window._text.select_range(0, 0)
    qtbot.keyClick(field, Qt.Key.Key_Backspace)
    assert [cell.index for cell in window._doc.cells] == before


def test_a_break_carried_by_a_pair_ends_the_line_after_the_whole_pair(
    qtbot, tmp_path
) -> None:
    """The other half of a caret standing inside a pair, and the same rule.

    Where a line ends on a bit the cell carries, the bit is a whole cell's -
    there is no half of one for a line to end on. Landing it where the caret
    literally was split the pair into the two letters it stands for, spent a
    second cell on them and pushed the tail of the region off the end.
    """
    from PySide6.QtCore import Qt

    window, _entry = _typing(
        qtbot,
        tmp_path,
        [0x50, 2, 0, 1],  # "THCAB"
        1,
        named=(Glyph(0x50, "TH", GlyphRole.TEXT),),
        preset="preset.tilemap.text-8bit-flag",
    )

    qtbot.keyClick(window._text._edit, Qt.Key.Key_Return)

    assert window._doc.text.body == "TH\nCAB"
    assert [cell.index for cell in window._doc.cells] == [0x50, 2, 0, 1]
    assert [cell.ends_line for cell in window._doc.cells] == [True, False, False, False]


# -- the Font Alphabet window ------------------------------------------------
def test_the_font_alphabet_window_opens_on_a_fontmap_and_a_close_stops_it(
    qtbot, tmp_path
) -> None:
    """The alphabet decides what the string says, so it opens beside the string.

    Closing it says the user does not want it, which is the only reading of that
    gesture - and View ▸ Font Alphabet is how it comes back.
    """
    window, _bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1])
    window._refresh_view()

    assert window._font_alphabet.isVisible()
    assert window._font_alphabet_action.isEnabled()

    window._on_font_alphabet_dismissed()
    window._font_alphabet.hide()
    window._refresh_view()
    assert not window._font_alphabet.isVisible()

    window._show_font_alphabet()
    assert window._font_alphabet.isVisible()


def test_a_cell_picked_on_the_canvas_selects_its_code_in_the_editor(
    qtbot, tmp_path
) -> None:
    """Clicking a glyph in the string asks what that code says, and the answer
    is a row of the table - so the pick lands there as a *selection*, the one
    Enter opens and the clipboard buttons act on."""
    window, _bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1])
    window._refresh_view()
    editor = window._font_alphabet

    window._select_tiles(1, 1)  # the second cell, which names tile 0
    assert [at.row() for at in editor._table.selectionModel().selectedRows()] == [0]
    # Both readings moved: the sheet above the table follows a row pick, so a
    # pick made for the user has to leave it in the same state a click would.
    assert editor._sheet.selected_id() == 0

    window._select_tiles(0, 0)  # the first cell names tile 2
    assert [at.row() for at in editor._table.selectionModel().selectedRows()] == [2]
    assert editor._sheet.selected_id() == 2


def test_picking_a_stretch_of_rows_picks_the_same_stretch_of_tiles(
    qtbot, tmp_path
) -> None:
    """One list shown twice, so a selection of several rows is a selection of
    several tiles - the ones the clipboard buttons are about to act on."""
    from PySide6.QtCore import QItemSelection, QItemSelectionModel

    window, _bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1])
    window._refresh_view()
    editor = window._font_alphabet
    table = editor._table

    picked = QItemSelection(table.model().index(2, 0), table.model().index(5, 2))
    table.selectionModel().select(picked, QItemSelectionModel.SelectionFlag.Select)
    assert editor._sheet.selected_ids() == {2, 3, 4, 5}
    # The first of them is still *the* tile, the one the arrows step from and
    # the one the dock's ring is told about.
    assert editor._sheet.selected_id() == 2

    # And a single pick puts it back to one, rather than adding to the stretch.
    editor.select_tile(0)
    assert editor._sheet.selected_ids() == {0}


def test_stepping_the_pick_with_shift_held_still_moves_one_row(qtbot, tmp_path) -> None:
    """The arrows step the sheet's pick, and the table has to follow it exactly.

    Shift is the state the key arrives in half the time — a user reaching for a
    stretch — and Qt reads the live modifiers when a row is selected with no
    event of its own: the table extended from its anchor and showed rows the
    sheet was ringing one tile of, which is the two readings disagreeing.
    """
    from PySide6.QtCore import Qt

    window, _bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1])
    window._refresh_view()
    editor = window._font_alphabet
    editor._sheet.select_id(1)

    qtbot.keyClick(editor._sheet, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)

    assert editor._sheet.selected_ids() == {2}
    rows = [index.row() for index in editor._table.selectionModel().selectedRows()]
    assert rows == [2]


def test_pasting_a_string_fills_consecutive_codes_from_the_selected_row(
    qtbot, tmp_path
) -> None:
    """The gesture the whole window is arranged around.

    A font sheet is a run of letters in order, and that run is the thing a user
    already has in a clipboard - so one paste states the font. Newlines are
    skipped rather than written: they describe the shape of whatever the text was
    copied out of, not a glyph.
    """
    from PySide6.QtGui import QGuiApplication

    window, bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="")
    window._refresh_view()
    editor = window._font_alphabet

    QGuiApplication.clipboard().setText("AB\nC")
    editor._table.selectRow(0)
    editor._fill_down()

    # Three characters over three consecutive codes, the newline stepped over.
    assert bank.font_chars == "ABC"
    assert entry.doc.text.body == "CAB"
    # One gesture, one step: the whole paste comes back together.
    window._undo_stack.undo()
    assert bank.font_chars == ""


def test_pasting_starts_at_the_selected_row_and_stops_at_the_last_tile(
    qtbot, tmp_path
) -> None:
    """A code past the sheet is a code no tile draws, so it is dropped and said.

    Growing the table instead would put glyphs behind codes nobody can see, and
    silently keeping them is how a font comes to spell something the picture
    never showed.
    """
    from PySide6.QtGui import QGuiApplication

    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="")
    window._refresh_view()
    editor = window._font_alphabet
    slots = len(editor._ids)

    QGuiApplication.clipboard().setText("X" * (slots + 5))
    editor._table.selectRow(2)
    editor._fill_down()

    # Started at the third code, ran out at the last tile, and the two holes it
    # stepped over are still holes.
    assert bank.font_chars == f"{HOLE}{HOLE}" + "X" * (slots - 2)
    assert "dropped" in editor._badge.text()


def test_enter_opens_the_text_cell_of_the_selected_row(qtbot, tmp_path) -> None:
    """Click a tile, press Enter, type the letter.

    Qt's own edit key is F2, which nobody reaches for on a row they just picked
    off the sheet - and the row's Text is the cell it exists to answer whatever
    column the cursor happens to be in.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QAbstractItemView

    window, _bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="")
    window._refresh_view()
    editor = window._font_alphabet
    editor._table.selectRow(2)

    qtbot.keyClick(editor._table, Qt.Key.Key_Return)

    assert editor._table.state() is QAbstractItemView.State.EditingState
    assert (editor._table.currentRow(), editor._table.currentColumn()) == (2, COL_TEXT)
    # Escape reaches the open editor and not the table, which is the other half
    # of the rule: the key only opens an editor while there is none.
    qtbot.keyClick(editor._table.focusWidget(), Qt.Key.Key_Escape)
    assert editor._table.state() is QAbstractItemView.State.NoState


def test_unticking_use_as_font_deletes_the_table_and_undo_brings_it_back(
    qtbot, tmp_path, confirmations
) -> None:
    """The untick is the whole declaration, so it takes the table with it.

    A sheet that is not a font has no use for an origin, a run and a list of
    named codes, and the editor is gated on the same tick — so keeping them would
    leave the project carrying a table with nowhere to see it. One undo step
    carries all four fields, which is what makes the deletion safe to offer.
    """
    window, bank, entry = _fontmap(qtbot, tmp_path, [0x12, 0x10, 0x11], base=0x10)
    window._activate_entry(bank)
    confirmations.yes = True

    window._on_use_as_font_change(False)

    assert confirmations.asked  # never silently
    # The origin goes with the run: it is a spin dialled against this sheet.
    assert not bank.use_as_font and bank.font_chars == "" and bank.font_base == 0
    window._activate_entry(entry)
    assert window._doc.text.body == "[$12][$10][$11]"

    window._undo_stack.undo()
    assert bank.use_as_font and bank.font_chars == UPPER and bank.font_base == 0x10
    window._activate_entry(entry)
    assert window._doc.text.body == "CAB"


def test_cancelling_the_untick_keeps_the_font_whole(qtbot, tmp_path) -> None:
    """Nothing is declared and nothing is pushed - the tick goes back on.

    The dialog is the only thing standing between a stray click and a table
    typed by hand, so cancelling has to leave the entry exactly as it was rather
    than untick and keep.
    """
    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1])
    window._activate_entry(bank)
    depth = window._undo_stack.count()

    window._on_use_as_font_change(False)  # the fixture answers Cancel

    assert bank.use_as_font and bank.font_chars == UPPER
    assert window._undo_stack.count() == depth
    assert window._use_as_font.isChecked()


def test_a_sheet_with_nothing_typed_on_it_unticks_without_asking(
    qtbot, tmp_path, confirmations
) -> None:
    """There is nothing to lose, and asking anyway trains the answer out of them."""
    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="")
    window._activate_entry(bank)

    window._on_use_as_font_change(False)

    assert not confirmations.asked and not bank.use_as_font


def test_the_editor_opens_on_the_font_sheet_itself(qtbot, tmp_path) -> None:
    """A sheet is typed up before any string exists to test it against.

    The two entry points are two different documents — a fontmap reaches its
    tiles through the binding, a pixels entry *is* its tiles — so the sheet is
    built two ways and this is the half that has no cells to build it from.
    """
    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1])
    window._activate_entry(bank)
    window._refresh_view()

    assert window._font_entry() is bank
    assert window._font_alphabet_available()
    assert window._font_alphabet.isVisible()
    # Slot n is code base + n, so the sheet has to have slots at all.
    assert window._font_sheet() is not None
    # And the tick is on show here, where it is not on the map. Asked of the
    # toolbar *action*, which is what the sync sets and what survives a re-layout
    # (the widget's own flag only catches up once the toolbar lays out, which a
    # window nobody has shown never does).
    assert window._use_as_font_action.isVisible() and window._use_as_font.isChecked()


def test_the_tick_is_hidden_on_anything_that_is_not_a_sheet(qtbot, tmp_path) -> None:
    """A map's cells are not letters, so there is no question to answer there."""
    window, _bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1])
    window._activate_entry(entry)
    window._refresh_view()

    assert not window._use_as_font_action.isVisible()


def test_the_sheet_reads_in_the_selected_palette_row(qtbot, tmp_path) -> None:
    """The row is folded into the indices upstream, so the colour table must not
    offset again — applied twice, a bank picked in row 5 draws in row 10."""
    window, _bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1])
    window._activate_entry(entry)
    window._subpalette.setValue(2)
    window._refresh_view()

    sheet = window._font_sheet()
    assert sheet is not None
    # The same picture the tile source dock draws, which is the claim: one bank,
    # one row, one rendering.
    assert window._tile_source_row() == 2


# -- brackets, and the draft an unfinished code leaves -----------------------
def test_a_bracket_that_could_not_close_is_dropped(qtbot, tmp_path) -> None:
    """Both are keys a user hits on the way to something else.

    A second ``[`` inside a code cannot open another one, and a ``]`` with
    nothing open has nothing to close — the text form has no literal ``]``,
    since it only ever means "the code ends here". Nothing happens, which is the
    honest response to a slip.
    """
    window, _entry = _typing(qtbot, tmp_path, [2, 0, 1, 0xF0], 0)  # "CAB[$F0]"
    field = window._text._edit
    assert window._text.body == "CAB[$F0]"

    # A ] where none is open.
    qtbot.keyClicks(field, "]")
    assert window._text.body == "CAB[$F0]"

    # A [ inside one. The caret goes between the $ and the F.
    window._text.select_range(5, 5)
    qtbot.keyClicks(field, "[")
    assert window._text.body == "CAB[$F0]"

    # And the ] that finishes a code the field already holds is *kept*, which is
    # why this is a walk and not a filter: it is only unmatched ones that go.
    qtbot.keyClicks(field, "9")
    assert window._text.body == "CAB[$9F0]"


def test_a_command_button_is_refused_inside_a_code(qtbot, tmp_path) -> None:
    """A whole ``[wait]`` dropped into the middle of one makes a bracketed thing
    no reader can parse, where the button's promise is that what it writes is
    exactly what the string holds."""
    from celpix.ui.text_window import TextWindow

    window = TextWindow()
    qtbot.addWidget(window)
    window.show_text("t", "A[$FE]B", (0, 1, 1, 1, 1, 1, 2), [("br", "[br]", "")], "")

    window.select_range(3, 3)  # inside the [$FE]
    window._guide_row.itemAt(0).widget().click()

    assert window.body == "A[$FE]B"


def test_ctrl_z_takes_back_the_draft_before_it_reaches_the_stack(
    qtbot, tmp_path
) -> None:
    """A half-spelled code is not on the session's stack at all.

    Nothing has been written for it, so an undo that reached past it took back
    something the user was not looking at and left the broken code standing —
    which is how a backspace inside a control code and one Ctrl+Z came to undo
    the tile binding instead of the edit.
    """
    from PySide6.QtCore import Qt

    window, _entry = _typing(qtbot, tmp_path, [2, 0, 1, 0xF0], 0)
    field = window._text._edit
    window._text.select_range(7, 7)  # inside "[$F0]", before the ]

    qtbot.keyClick(field, Qt.Key.Key_Backspace)
    assert window._text.body == "CAB[$F]"  # a draft: nothing written yet
    assert [cell.index for cell in window._doc.cells] == [2, 0, 1, 0xF0]

    qtbot.keyClick(field, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)

    # The draft goes, the file's own string comes back, and the step underneath
    # is still one more Ctrl+Z away.
    assert window._text.body == "CAB[$F0]"
    assert [cell.index for cell in window._doc.cells] == [2, 0, 1, 0xF0]


def test_each_row_settled_in_the_editor_is_its_own_undo_step(qtbot, tmp_path) -> None:
    """A row is a code's whole answer, so three rows are three things to take
    back — where a gesture that fills many at once stays one step."""
    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="")
    window._refresh_view()
    table = window._font_alphabet._table
    steps = window._undo_stack.count()

    for row, char in enumerate("ABC"):
        table.item(row, COL_TEXT).setText(char)

    assert bank.font_chars == "ABC"
    assert window._undo_stack.count() == steps + 3  # three steps, not one
    window._undo_stack.undo()
    assert bank.font_chars == "AB"  # only the last row comes back


def test_ctrl_z_in_the_alphabet_window_reaches_the_session_stack(
    qtbot, tmp_path
) -> None:
    """The floating window answers the key itself, and has to.

    Qt keeps the main window's Undo action alive while a `Qt.Tool` window is
    active, so a second binding here is a tie - and a tie fires neither, which is
    an editor whose Ctrl+Z does nothing until the user clicks off it.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="ABCDE")
    window.show()
    window._refresh_view()
    editor = window._font_alphabet
    # Shown and active, the way an action's WindowShortcut is judged (the
    # offscreen platform ignores activateWindow()).
    editor.show()
    QApplication.setActiveWindow(editor)
    editor._table.setFocus()

    editor._table.item(1, COL_TEXT).setText("Z")
    assert bank.font_chars == "AZCDE"

    qtbot.keyClick(editor._table, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert bank.font_chars == "ABCDE"
    qtbot.keyClick(editor._table, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
    assert bank.font_chars == "AZCDE"


def test_space_arms_the_sheets_pan_unless_a_cell_is_being_typed_into(
    qtbot, tmp_path
) -> None:
    """The window claims space wherever focus sits, because the press goes to the
    focused widget alone and the table is where reading the sheet leaves it.

    The one exception is a cell editor: a font's own space glyph is a character
    somebody has to be able to type into the Text column.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLineEdit

    window, _bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="ABCDE")
    window._refresh_view()
    editor = window._font_alphabet
    editor.show()
    QApplication.setActiveWindow(editor)
    editor._table.setFocus()

    qtbot.keyPress(editor._table, Qt.Key.Key_Space)
    assert editor._sheet._pan_active
    qtbot.keyRelease(editor._table, Qt.Key.Key_Space)
    assert not editor._sheet._pan_active

    # Typing into a row: the editor is a line edit of this window, and the key is
    # its own.
    editor._table.editItem(editor._table.item(1, COL_TEXT))
    cell = editor._table.findChildren(QLineEdit)[-1]
    cell.setFocus()
    qtbot.keyPress(cell, Qt.Key.Key_Space)
    assert not editor._sheet._pan_active


def test_a_role_pick_is_its_own_step_and_needs_a_name_to_land(qtbot, tmp_path) -> None:
    """A role is a pick from a list rather than a keystroke, and it needs
    something to be the role of - what a non-text code reads as is its *name*, so
    a role on a row that spells nothing is put back rather than silently dropped
    on the next redraw."""
    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="ABCDE")
    window._refresh_view()
    editor = window._font_alphabet
    table = editor._table

    table.item(1, COL_TEXT).setText("Z")
    # What the role delegate does when its combo closes.
    table.item(1, COL_ROLE).setText("control")
    assert bank.font_codes == (Glyph(1, "Z", GlyphRole.CONTROL),)

    # Its own step: the letter typed a moment earlier stays put.
    window._undo_stack.undo()
    assert (bank.font_chars, bank.font_codes) == ("AZCDE", ())

    steps = window._undo_stack.count()
    table.item(6, COL_ROLE).setText("control")  # a row past the run
    assert window._undo_stack.count() == steps
    assert table.item(6, COL_ROLE).text() == "text"
    assert "no text" in editor._badge.text()


def test_a_line_break_names_itself_and_numbers_the_next_one(qtbot, tmp_path) -> None:
    """The one role that does not have to be said out loud first: a break reads
    as `br`, and a second one as `br-1`, since two codes answering to one name
    would leave whichever came second unreachable."""
    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="ABCDE")
    window._refresh_view()
    table = window._font_alphabet._table

    table.item(6, COL_ROLE).setText("line break")
    assert bank.font_codes == (Glyph(6, "br", GlyphRole.BREAK),)
    assert table.item(6, COL_TEXT).text() == "br"  # written into the column it names

    table.item(7, COL_ROLE).setText("line break")
    assert [glyph.text for glyph in bank.font_codes] == ["br", "br-1"]

    # A name the user wrote is left alone - only an empty row is filled in.
    table.item(5, COL_TEXT).setText("scroll")
    table.item(5, COL_ROLE).setText("line break")
    assert Glyph(5, "scroll", GlyphRole.BREAK) in bank.font_codes


def test_a_code_can_be_made_to_spell_a_pair_rather_than_name_a_command(
    qtbot, tmp_path
) -> None:
    """Several characters typed in are *guessed* to be a name, and the Role
    column is where that is corrected.

    A code standing for a pair is what a game's dictionary compression is, and
    the guess is right for `wait` and wrong for `th` - so it is a guess and not a
    rule, it holds only for the keystroke that made it, and a row that already
    reads *dict* keeps its role while its spelling is corrected.
    """
    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="ABCDE")
    window._refresh_view()
    table = window._font_alphabet._table

    table.item(1, COL_TEXT).setText("th")
    assert bank.font_codes == (Glyph(1, "th", GlyphRole.CONTROL),)

    table.item(1, COL_ROLE).setText("dict")
    assert bank.font_codes == (Glyph(1, "th", GlyphRole.DICT),)
    assert table.item(1, COL_ROLE).text() == "dict"

    table.item(1, COL_TEXT).setText("the")  # not guessed at a second time
    assert bank.font_codes == (Glyph(1, "the", GlyphRole.DICT),)

    # Picking **text** on it is the same pick: the spelling is what says which of
    # the two it is, so the column redraws to what was actually stored.
    table.item(1, COL_ROLE).setText("text")
    assert bank.font_codes == (Glyph(1, "the", GlyphRole.DICT),)
    assert table.item(1, COL_ROLE).text() == "dict"

    # Out of the run either way - a tile draws one character - and the string
    # reads the code as everything it spells.
    assert bank.font_chars == "A" + HOLE + "CDE"
    assert window._doc.text.body == "CAthe"


def test_a_command_can_be_declared_to_swallow_the_cell_after_it(
    qtbot, tmp_path
) -> None:
    """`speed, 1` in the Text column - the count beside the name.

    One cell rather than a fourth column, because that is how the count is
    written everywhere else a font table is (`7A=[speed, 1]`), and the string
    then shows the command and its operand as the one thing they are.
    """
    # "CA", then a command with the byte it swallows.
    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 6, 1], chars="ABCDE")
    window._refresh_view()
    table = window._font_alphabet._table

    table.item(6, COL_TEXT).setText("speed, 1")
    assert bank.font_codes == (Glyph(6, "speed", GlyphRole.CONTROL, params=1),)
    # Shown back the way it was typed, so the count can be corrected.
    assert table.item(6, COL_TEXT).text() == "speed, 1"

    assert window._doc.text.body == "CA[speed, $01]"
    # And the cell it swallowed is no longer a letter of its own.
    assert "B" not in window._doc.text.body


def test_append_gives_a_code_past_the_sheet_a_row_to_be_named_on(
    qtbot, tmp_path
) -> None:
    """The table is one row per tile, and a font routinely has to answer for
    codes no tile draws — a terminator above the sheet, a letter the sheet
    uploaded beside this one draws (``docs/design/fontmap-entry.md`` §4).

    So the rows have to be reachable, and what is typed into one has to land on
    the code the row's header shows. Nothing in an appended row is on a tile, so
    it is stored as a **named code** whatever its role.
    """
    # $0A is four codes past the eight-tile sheet, so it reads as hex until the
    # table can reach it at all.
    window, bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1, 0x0A], chars="ABC")
    window._refresh_view()
    editor = window._font_alphabet
    assert editor._table.rowCount() == 8  # the sheet, and nothing else
    assert entry.doc.text.body == "CAB[$0A]"

    editor._append_spin.setValue(4)

    codes = [
        editor._table.item(row, COL_CODE).text()
        for row in range(editor._table.rowCount())
    ]
    assert codes[-4:] == ["$08", "$09", "$0A", "$0B"]
    assert bank.font_append == 4  # the font's own answer, saved with the project

    editor._table.item(10, COL_TEXT).setText("end")
    editor._table.item(10, COL_ROLE).setText("control")

    assert bank.font_codes == (Glyph(0x0A, "end", GlyphRole.CONTROL),)
    assert entry.doc.text.body == "CAB[end]"
    # Dialling the rows away again does not take the answer with it: a code that
    # says something keeps its row, appended after the sheet.
    editor._append_spin.setValue(0)
    assert bank.font_codes == (Glyph(0x0A, "end", GlyphRole.CONTROL),)
    assert editor._table.item(8, COL_CODE).text() == "$0A"


def test_prepend_lists_below_the_origin_and_moves_the_tiles_down_the_table(
    qtbot, tmp_path
) -> None:
    """The other direction, and the arithmetic that has to follow it.

    Every row ⇄ tile conversion shifts by however many rows sit above the sheet,
    so a pick made on the canvas has to land on the row the *tile* is on rather
    than on the row its index would have been. The span is floored at code zero,
    which is why that offset is not simply the spin.
    """
    window, bank, _entry = _fontmap(qtbot, tmp_path, [0x82], chars="ABC", base=0x80)
    window._refresh_view()
    editor = window._font_alphabet

    editor._prepend_spin.setValue(2)

    assert editor._table.item(0, COL_CODE).text() == "$7E"
    assert bank.font_prepend == 2
    # The sheet's first tile is still its first tile; it is now the third row.
    first = editor._ids[0]
    editor.select_tile(first)
    assert [at.row() for at in editor._table.selectionModel().selectedRows()] == [2]
    assert editor._sheet.selected_id() == first

    # Asked for more rows than there are codes below the origin: it lists them
    # anyway, and the tiles stay where the count puts them.
    editor._prepend_spin.setValue(0x100)
    assert editor._table.item(0, COL_CODE).text() == "-$80"
    editor.select_tile(first)
    assert [at.row() for at in editor._table.selectionModel().selectedRows()] == [0x100]


def test_a_run_longer_than_the_sheet_is_not_split_in_half(qtbot, tmp_path) -> None:
    """Which half of the storage a code lands in is the **run's** extent, never
    the sheet's — and that is a data-loss guard, not a tidiness one.

    A paste or a template routinely leaves a run longer than the tiles that draw
    it (*ASCII, from $20* is 95 characters). Bounded by the sheet, a code the run
    already answers for reads out of the run but writes back as a *named* code:
    two halves holding one code, the named one shadowing on read. Named codes do
    not move with **Base code** and the run does, so dialling the origin then
    slides one out from under the other and a character the user typed is gone.
    """
    # The eight-tile sheet is drawn by a 43-character run, so codes $08 upward
    # are inside the run and past the last tile.
    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars=UPPER)
    window._refresh_view()
    editor = window._font_alphabet
    assert len(editor._ids) == 8 < len(bank.font_chars)
    # Those codes get rows from the run, not only from the sheet.
    assert editor._table.rowCount() == len(UPPER)

    editor._table.item(20, COL_TEXT).setText("x")

    # Into the run, at the slot the row names — not a named code beside it.
    assert bank.font_codes == ()
    assert bank.font_chars == UPPER[:20] + "x" + UPPER[21:]

    # And so it travels with the origin, which is the whole point: a named code
    # would have stayed at $14 while the letters around it moved.
    editor._base_spin.setValue(0x10)
    assert editor._table.item(20, COL_CODE).text() == "$24"
    assert editor._table.item(20, COL_TEXT).text() == "x"


def test_a_row_below_code_zero_is_listed_but_cannot_be_written_to(
    qtbot, tmp_path
) -> None:
    """Prepend lists the rows it was asked for even under an origin of 0, where
    they read as `-$04` — it is *how much headroom to look at*, and swallowing
    the count would make the spin look broken on the commonest font of all.

    Nothing is stored there, and that guard is load-bearing rather than tidy: a
    named code is the one half of the storage nothing range-checks downstream, so
    a negative one would survive into the alphabet and `encode` would write it
    into a cell as the index.
    """
    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="ABC")
    window._refresh_view()
    editor = window._font_alphabet
    table = editor._table

    editor._prepend_spin.setValue(3)

    codes = [table.item(row, COL_CODE).text() for row in range(4)]
    assert codes == ["-$03", "-$02", "-$01", "$00"]

    table.item(0, COL_TEXT).setText("Z")

    # Refused, said out loud, and the row put back to what it was.
    assert bank.font_codes == ()
    assert bank.font_chars == "ABC"
    assert "no such code" in editor._badge.text()
    assert table.item(0, COL_TEXT).text() == ""

    # And raising the origin is what makes those rows real, which is what the
    # badge sends the user to do.
    editor._base_spin.setValue(3)
    assert table.item(0, COL_CODE).text() == "$00"
    table.item(0, COL_TEXT).setText("Z")
    assert bank.font_codes == (Glyph(0, "Z", GlyphRole.TEXT),)


def test_shift_moves_the_characters_and_not_the_codes(qtbot, tmp_path) -> None:
    """The correction a run pasted one tile out needs, and not the Base code spin
    one reading over: that moves which codes the run occupies and leaves every
    character on the tile it was typed against."""
    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="ABC")
    window._refresh_view()
    editor = window._font_alphabet

    editor._shift(1)
    # A hole opens at the first tile and the origin has not moved.
    assert (bank.font_chars, bank.font_base) == (f"{HOLE}ABC", 0)

    editor._shift(-1)
    assert (bank.font_chars, bank.font_base) == ("ABC", 0)

    # Shifting up past the start drops the character that falls off the top —
    # a run and a ring are not the same thing.
    editor._shift(-1)
    assert bank.font_chars == "BC"


def test_copy_and_paste_alphabet_carry_the_whole_table(qtbot, tmp_path) -> None:
    """The `20=A` form a font table is kept in everywhere outside celPix, so what
    comes out pastes into a disassembly and back.

    **Replaces** where the table's own Ctrl+V fills down: a table arriving from
    somewhere else is the whole answer, and leaving the old one underneath would
    merge two fonts into a third that is neither.
    """
    from PySide6.QtGui import QGuiApplication

    window, bank, _entry = _fontmap(
        qtbot,
        tmp_path,
        [2, 0, 1],
        chars="ABC",
        named=(Glyph(0xFE, "line-break", GlyphRole.BREAK),),
    )
    window._refresh_view()
    editor = window._font_alphabet

    editor._copy_alphabet()
    copied = QGuiApplication.clipboard().text()
    # Characters as themselves, a command in the bracketed form the same parser
    # reads back.
    assert "00=A" in copied and "FE=[line-break]" in copied

    QGuiApplication.clipboard().setText("00=X\n01=Y\n")
    editor._paste_alphabet()

    # The whole table, not a merge: the old run and its named code are both gone.
    assert bank.font_chars == "XY"
    assert bank.font_codes == ()
    window._undo_stack.undo()
    assert bank.font_chars == "ABC"


def test_pasting_a_plain_string_spells_the_codes_from_the_selected_row(
    qtbot, tmp_path
) -> None:
    """The other form a font is quoted in: a row of letters, one per code.

    It states no codes of its own, so the selection says where it starts - and
    the paste still replaces to the end of the span, which is what tells it apart
    from the table's own fill-down Ctrl+V.
    """
    from PySide6.QtGui import QGuiApplication

    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="ABCDE")
    window._refresh_view()
    editor = window._font_alphabet

    QGuiApplication.clipboard().setText("xy")
    editor._table.selectRow(2)
    editor._paste_alphabet()

    # Landed on the third and fourth codes; the fifth was inside the span and so
    # came out blank, and the two before the selection never moved.
    assert bank.font_chars == "ABxy"


def test_the_clipboard_buttons_act_on_the_picked_rows(qtbot, tmp_path) -> None:
    """Several rows picked is those rows and nothing else, both ways.

    Non-contiguous on purpose: the span is the rows the user pointed at, not the
    stretch between the first and the last.
    """
    from PySide6.QtCore import QItemSelection, QItemSelectionModel
    from PySide6.QtGui import QGuiApplication

    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="ABCDE")
    window._refresh_view()
    editor = window._font_alphabet
    picked = editor._table.selectionModel()
    model = editor._table.model()
    picked.clearSelection()
    for row in (1, 3):
        # The whole row spelled out rather than the Rows flag, which is the same
        # selection: a ctrl+click is what this stands in for.
        picked.select(
            QItemSelection(model.index(row, COL_CODE), model.index(row, COL_ROLE)),
            QItemSelectionModel.SelectionFlag.Select,
        )

    # The pick survives being pushed at the sheet: the tile it marks must not
    # come back as a one-row selection.
    assert [index.row() for index in picked.selectedRows()] == [1, 3]
    editor._copy_alphabet()
    assert QGuiApplication.clipboard().text() == "01=B\n03=D\n"

    QGuiApplication.clipboard().setText("xy")
    editor._paste_alphabet()
    assert bank.font_chars == "AxCyE"

    # A table whose codes all fall outside the picked rows is aimed at the wrong
    # place, so it writes nothing rather than clearing them.
    QGuiApplication.clipboard().setText("00=Z\n")
    editor._paste_alphabet()
    assert bank.font_chars == "AxCyE"
    assert "outside" in editor._badge.text()
