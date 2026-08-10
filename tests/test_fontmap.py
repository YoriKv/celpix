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

    window._on_font_alphabet_edited(0, UPPER, (), True, "edit font alphabet")

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

    window._on_font_alphabet_edited(0x80, UPPER, (), True, "set base code to $80")

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
        0, bank.font_chars, bank.font_codes, True, "set base code to $0"
    )
    assert entry.doc.text.body == "[$80][wait]"
    assert [g.text for g in entry.doc.font_alphabet.commands] == ["wait"]


def test_an_unticked_font_is_not_read(qtbot, tmp_path) -> None:
    """Use as Font is the declaration, and unticking keeps the table.

    The table is the user's own work and there is no reading of unticking a box
    that means "and throw that away" - so it stays, and what the tick decides is
    whether it is read.
    """
    window, bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1])
    assert entry.doc.text.body == "CAB"

    window._on_font_alphabet_edited(0, UPPER, (), True, "edit font alphabet")
    bank.use_as_font = False
    window._apply_font_alphabet(bank, (False, 0, UPPER, ()))

    assert entry.doc.text.body == "[$02][$00][$01]"
    assert bank.font_chars == UPPER  # kept, not cleared


def test_an_alphabet_edit_is_one_undo_step(qtbot, tmp_path) -> None:
    """Everything the editor settles travels as one state, so one gesture is one
    step - and an undo puts the whole table back, not part of it."""
    window, bank, entry = _fontmap(qtbot, tmp_path, [0x82, 0x80, 0x81], chars="")

    window._on_font_alphabet_edited(0x80, UPPER, (), True, "paste 43 characters")

    assert (bank.font_base, bank.font_chars) == (0x80, UPPER)
    assert entry.doc.text.body == "CAB"
    window._undo_stack.undo()
    assert (bank.font_base, bank.font_chars) == (0, "")
    assert entry.doc.text.body == "[$82][$80][$81]"


def test_the_streams_control_codes_come_from_its_own_cell_format(
    qtbot, tmp_path
) -> None:
    """The design's load-bearing split, end to end: letters from the font,
    punctuation from the stream (``docs/design/fontmap-entry.md`` §3).

    Two runs in one game routinely share a font and punctuate differently, so the
    controls cannot live on the font - and the terminator here has to reach the
    text through a *preset* param, not through the alphabet the bank names.
    """
    from celpix.core.errors import Stage
    from celpix.plugins.base import Preset

    window, _bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1, 0xFF, 0])
    window._registry.register_preset(
        Preset(
            id="preset.tilemap.text-terminated",
            name="Text run, FF-terminated",
            stage=Stage.INTERPRET_TILEMAP,
            engine_id="codec.tilemap.packed",
            params={
                "layout": "text",
                "bytes": 1,
                "index": {"shift": 0, "bits": 8},
                # No `role` on the second: a cell format's controls are commands
                # unless they say otherwise, which is what keeps every line from
                # carrying the only thing it could be.
                "controls": [
                    {"code": 0xFF, "name": "line break", "role": "break"},
                    {
                        "code": 0x2A,
                        "name": "wait for input",
                        "description": "Holds until the player presses a button.",
                    },
                ],
            },
        )
    )
    entry.tilemap_preset_id = "preset.tilemap.text-terminated"
    entry.doc = None
    window._activate_entry(window._workspace.entries[0])
    window._activate_entry(entry)

    doc = window._doc
    # 0xFF is the stream's line break, not the font's letter for it...
    assert doc.text.body == "CAB\nA"
    # ...and it types back to exactly the bytes it came from.
    assert list(doc.font_alphabet.encode(doc.text.body).codes) == [2, 0, 1, 0xFF, 0]
    # The unroled entry became a command, and reads as its own name — spelled to
    # one word, since that name is what a reader retypes inside the brackets.
    assert doc.font_alphabet.decode([0x2A]).body == "[wait-for-input]"
    assert [g.text for g in doc.font_alphabet.commands] == [
        "line-break",
        "wait-for-input",
    ]
    # And the format author's sentence about the code reaches the insert row,
    # which is the only place a reader would look for it.
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


def test_the_use_as_font_tick_is_one_undoable_declaration(qtbot, tmp_path) -> None:
    """It gates the table without taking it away.

    The table is the user's own work and there is no reading of unticking a box
    that means "and throw that away", so what the tick decides is only whether it
    is read.
    """
    window, bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1])
    window._activate_entry(bank)

    window._on_use_as_font_change(False)

    assert not bank.use_as_font and bank.font_chars == UPPER
    window._activate_entry(entry)
    assert window._doc.text.body == "[$02][$00][$01]"

    window._undo_stack.undo()
    assert bank.use_as_font
    window._activate_entry(entry)
    assert window._doc.text.body == "CAB"


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
    # And the tick is on show here, where it is not on the map. isHidden rather
    # than isVisible: the main window itself is never shown under offscreen Qt,
    # so every child reads as invisible and only the explicit flag means anything.
    assert not window._use_as_font.isHidden() and window._use_as_font.isChecked()


def test_the_tick_is_hidden_on_anything_that_is_not_a_sheet(qtbot, tmp_path) -> None:
    """A map's cells are not letters, so there is no question to answer there."""
    window, _bank, entry = _fontmap(qtbot, tmp_path, [2, 0, 1])
    window._activate_entry(entry)
    window._refresh_view()

    assert window._use_as_font.isHidden()


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


def test_a_run_of_rows_typed_in_the_editor_is_one_undo_step(qtbot, tmp_path) -> None:
    """Each row settles as it is left, so the sheet and the string follow the
    caret — but a word typed into six rows is one thing the user did."""
    window, bank, _entry = _fontmap(qtbot, tmp_path, [2, 0, 1], chars="")
    window._refresh_view()
    steps = window._undo_stack.count()

    # fresh only on the first, which is what a run of typing reports.
    window._on_font_alphabet_edited(0, "A", (), True, "edit font alphabet")
    window._on_font_alphabet_edited(0, "AB", (), False, "edit font alphabet")
    window._on_font_alphabet_edited(0, "ABC", (), False, "edit font alphabet")

    assert bank.font_chars == "ABC"
    assert window._undo_stack.count() == steps + 1  # one step, not three
    window._undo_stack.undo()
    assert bank.font_chars == ""


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


def test_a_role_pick_is_its_own_step_and_needs_a_name_to_land(qtbot, tmp_path) -> None:
    """A role is a pick from a list, not a keystroke: it ends the run either side.

    And it needs something to be the role of - what a non-text code reads as is
    its *name*, so a role on a row that spells nothing is put back rather than
    silently dropped on the next redraw.
    """
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
    table.item(6, COL_ROLE).setText("line break")  # a row past the run
    assert window._undo_stack.count() == steps
    assert table.item(6, COL_ROLE).text() == "text"
    assert "no text" in editor._badge.text()


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
