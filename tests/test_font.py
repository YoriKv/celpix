"""The alphabet: codes to text and back, and the table forms it is stated in.

The regression risk here is the **round trip**. A fontmap's text window is the
only editing surface in celPix that shows a lossy projection of its document, so
every one of these tests is really the same question — does what came out go back
in as the same bytes — asked of the case that would break it.
"""

from __future__ import annotations

import pytest

from celpix.core.font import (
    FontAlphabet,
    Glyph,
    GlyphRole,
    carried_break,
    glyphs_from_spec,
    parse_table,
    sequential,
)


def _alphabet(**kwargs) -> FontAlphabet:
    """A font with one of everything a general reader can express: letters, a
    code standing for a pair, two line breaks and a named command."""
    return FontAlphabet(
        [
            *sequential(0, "ABCDE"),
            Glyph(0x20, "th"),
            Glyph(0xFE, "line break", GlyphRole.BREAK),
            Glyph(0xFD, "scroll break", GlyphRole.BREAK),
            Glyph(0xFF, "end of string", GlyphRole.CONTROL),
        ],
        **kwargs,
    )


@pytest.mark.parametrize(
    ("codes", "body"),
    [
        # The canonical break is a newline and nothing else, so the window shows
        # the string's own line structure.
        ([0, 1, 0xFE, 2, 3], "AB\nCD"),
        # Every other command reads as its **name** - including a *second*
        # break, which stays unambiguous precisely by not being a newline too.
        ([0, 0xFD, 1], "A[scroll break]B"),
        ([0, 0xFF, 1, 0xFF], "A[end of string]B[end of string]"),
        # A code standing for a pair is spelled out as the pair.
        ([0x20, 0], "thA"),
        # A code **nobody has named** falls back to its own hex, which is what
        # keeps the form general: nothing has to be described to survive an edit.
        ([0x99, 0], "[$99]A"),
        ([0x99] * 4, "[$99][$99][$99][$99]"),
    ],
)
def test_text_round_trips_through_every_kind_of_glyph(codes, body) -> None:
    """Decode reads as words, and encode gives back the **same bytes**.

    The two halves are one test because either alone is worthless: a decoder that
    reads beautifully and does not type back writes a different string to the ROM
    than the one on screen, which is the failure this whole surface exists to
    avoid.
    """
    alphabet = _alphabet()
    text = alphabet.decode(codes)
    assert text.body == body
    encoded = alphabet.encode(text.body)
    assert list(encoded.codes) == codes
    assert encoded.ok


def test_a_literal_bracket_is_doubled_so_it_types_back() -> None:
    """``[`` opens a code, so a font that has one as a letter must escape it.

    Emitting a bare ``[`` would make the decoded string unparseable at exactly
    the point it mattered - the next code would be read as part of a letter.
    """
    alphabet = FontAlphabet([Glyph(5, "["), *sequential(0, "AB")])
    text = alphabet.decode([0, 5, 1])
    assert text.body == "A[[B"
    assert list(alphabet.encode(text.body).codes) == [0, 5, 1]


def test_a_pair_code_is_preferred_over_the_letters_it_stands_for() -> None:
    """Longest text wins on the way back, which is the whole point of a pair code.

    Spending two codes where the font has one for the pair is the difference
    between a string fitting its slot and not - and a fontmap's slot is fixed.
    """
    alphabet = _alphabet()
    # "th" is one code; T and H are not in this font at all, so a shortest-match
    # encoder would report both as unknown instead of finding the pair.
    assert list(alphabet.encode("th").codes) == [0x20]


def test_a_pair_at_the_very_end_of_the_string_is_still_matched_whole() -> None:
    """The longest match is probed by width, and the last one has no room to spare.

    :meth:`FontAlphabet._longest_text` asks for the widest spelling first and
    lets the slice truncate at the end of the string, so a pair sitting on the
    final characters is answered by a probe wider than the text left. It has to
    come back as the pair all the same - a font that spent two codes on the last
    two letters of a fixed-size region is a string that no longer fits it.
    """
    alphabet = FontAlphabet(
        [*sequential(0, "AB"), Glyph(0x20, "th"), Glyph(0x21, "the")]
    )
    assert list(alphabet.encode("Ath").codes) == [0, 0x20]
    assert list(alphabet.encode("Athe").codes) == [0, 0x21]
    # And one character short of the widest spelling, where the probe truncates
    # to a *shorter* glyph that does match.
    assert list(alphabet.encode("th").codes) == [0x20]


def test_encoding_a_second_string_does_not_answer_with_the_first() -> None:
    """The alphabet keeps its last answer, and it is keyed on what was asked.

    One edit asks :meth:`FontAlphabet.encode` the same question several times
    over - the budget readout, the write, the readout again - so the answer is
    kept. Two different strings must still get two answers, in either order and
    however many times each is asked.
    """
    alphabet = _alphabet()
    first = list(alphabet.encode("AB").codes)
    second = list(alphabet.encode("CD").codes)
    assert first == [0, 1]
    assert second == [2, 3]
    assert list(alphabet.encode("AB").codes) == first
    assert list(alphabet.encode("").codes) == []
    assert list(alphabet.encode("CD").codes) == second


def test_what_the_font_cannot_say_is_reported_and_costs_a_blank_cell() -> None:
    """Unknown characters come back as a list, in order and without repeats - and
    each still spends the cell it was typed into.

    Never raised: the window has to keep showing what the user typed while telling
    them part of it will not fit. Never *dropped* either, which is the half that
    matters to the picture - leaving the character out would slide every cell
    after it one to the left, so what reached the file would be a string nobody
    typed and no caller could do anything with it but refuse the whole edit.
    """
    alphabet = FontAlphabet([Glyph(0x20, " "), *sequential(0, "AB")])

    encoded = alphabet.encode("AzBz!")

    assert encoded.unknown == ("z", "!")
    assert not encoded.ok
    assert list(encoded.codes) == [0, 0x20, 1, 0x20, 0x20]


def test_a_font_with_no_space_blanks_with_code_zero() -> None:
    """There is no honest text for the cell, so the fallback is the one code that
    is not a letter picked on the user's behalf."""
    assert FontAlphabet(sequential(1, "AB")).encode("z").codes == (0,)


def test_a_newline_with_no_break_code_is_reported_and_costs_nothing() -> None:
    """A bare index-only run has no punctuation, so Enter has nothing to encode to.

    Dropping it silently would let a user lay out a string that the file cannot
    hold and be told nothing about it. It takes no blank either, unlike a letter
    the font lacks: punctuation this format cannot express never had a cell to
    stand in for, and inventing one would push a space into the string.
    """
    plain = FontAlphabet([Glyph(0x20, " "), *sequential(0, "AB")])
    assert plain.line_break is None

    encoded = plain.encode("A\nB")

    assert encoded.unknown == ("\\n",)
    assert encoded.codes == (0, 1)


def test_a_spelling_of_several_characters_is_a_dictionary_glyph() -> None:
    """``text`` and ``dict`` are one fact said twice, so the model settles it.

    Nothing downstream could ask ``role is DICT`` if a caller were free to
    declare a three-character glyph ``text`` — the picture branches on that role
    (``docs/design/fontmap-entry.md`` §5), and a role free to contradict its own
    spelling is a branch taken on a coin toss. Both directions, because a role
    picked in the editor is as much a claim as one read out of a file.
    """
    assert Glyph(0xE3, "you").role is GlyphRole.DICT
    assert Glyph(0xE3, "you", GlyphRole.TEXT).role is GlyphRole.DICT
    assert Glyph(0x00, "A", GlyphRole.DICT).role is GlyphRole.TEXT
    # And the punctuating roles are left alone: a name is a name however long.
    assert Glyph(0xFE, "line-break", GlyphRole.BREAK).role is GlyphRole.BREAK


def test_a_dictionary_code_spells_out_into_its_characters_own_codes() -> None:
    """What the picture needs from a code the sheet has no tile for.

    Through the alphabet rather than at tiles directly, so the answer follows the
    run's origin; and **nothing at all** where one character of it cannot be
    spelled, since a word drawn with a letter missing from the middle is worse
    than the cell nobody has explained yet.
    """
    alphabet = FontAlphabet([*sequential(0, "you"), Glyph(0xE3, "you")])
    assert alphabet.has_dictionary
    assert alphabet.spelling(0xE3) == (0, 1, 2)
    assert alphabet.spelling(0) == ()  # a letter spells only itself
    assert alphabet.spelling(0x77) == ()  # and a code nothing has named, nothing

    missing = FontAlphabet([*sequential(0, "yo"), Glyph(0xE3, "you")])
    assert missing.spelling(0xE3) == ()
    assert not FontAlphabet(sequential(0, "you")).has_dictionary


def test_the_stream_controls_win_over_the_fonts_letters() -> None:
    """A code the font spells and the stream reserves belongs to the **stream**.

    The font's table was authored against tiles and has no way of knowing which
    codes a given text format has taken for itself, so the merge is one-way
    (``docs/design/fontmap-entry.md`` §3).
    """
    font = FontAlphabet(sequential(0, "ABC"))
    controls = FontAlphabet([Glyph(2, "end of string", GlyphRole.CONTROL)])
    merged = font.merged(controls)
    assert merged.decode([0, 1, 2]).body == "AB[end of string]"
    # The letters it did not claim are untouched.
    assert merged.decode([1]).body == "B"


def test_positions_map_every_character_back_to_its_cell() -> None:
    """The caret-to-canvas link: a hex code's every character names the one cell.

    A caret sits between characters, so this has to be per character rather than
    per glyph - which is also why a five-character ``[$99]`` is five entries.
    """
    text = _alphabet().decode([0, 0x99, 1])
    assert text.body == "A[$99]B"
    assert text.positions == (0, 1, 1, 1, 1, 1, 2)
    assert text.span_of(2, 2) == (1, 2)  # inside the code -> its own cell
    # One past the end is where typing appends, and it names the last cell.
    assert text.cell_at(99) == 2


def test_code_width_follows_the_stream_and_not_the_font() -> None:
    """An unmapped code prints at the width its *cells* are read at.

    The same font sheet can be indexed one byte at a time or two, and a code
    shown at the wrong width does not type back to the same bytes.
    """
    wide = _alphabet(code_digits=4)
    assert wide.decode([0xFFFE]).body == "[$FFFE]"
    assert list(wide.encode("[$FFFE]").codes) == [0xFFFE]


def test_shifting_moves_every_glyph_and_still_types_back() -> None:
    """The origin control: the same table, read at a different starting code.

    Both directions have to move together or the shift is worse than useless -
    text that reads correctly and writes the wrong bytes is exactly the failure
    the round trip exists to catch.
    """
    moved = _alphabet().shifted(0x80)

    assert moved.decode([0x80, 0x81, 0xA0]).body == "ABth"
    assert list(moved.encode("ABth").codes) == [0x80, 0x81, 0xA0]
    # What was at the old codes is now unclaimed, and reads as it should.
    assert moved.decode([0, 1]).body == "[$00][$01]"
    # The unshifted case is every alphabet that needed no dialling, so it is the
    # same object rather than a rebuilt copy.
    unmoved = _alphabet()
    assert unmoved.shifted(0) is unmoved


def test_glyphs_shifted_out_of_the_code_space_are_dropped() -> None:
    """Not clamped, because a kept one is a letter that types into a bad index.

    A code outside what a cell of this width holds is one no stream can contain,
    so keeping the glyph would only give ``encode`` something to write that the
    map cannot store - and clamping would pile several onto the end code and let
    whichever came first silently win.
    """
    off_the_end = _alphabet().shifted(0x80)  # 0xFE, 0xFD, 0xFF go past $FF
    assert off_the_end.line_break is None
    assert not [glyph for glyph in off_the_end.glyphs if glyph.code > 0xFF]
    # A newline now has no code to become, and is reported rather than dropped.
    assert off_the_end.encode("\n").unknown == ("\\n",)

    off_the_start = _alphabet().shifted(-0x10)
    assert [glyph.code for glyph in off_the_start.glyphs if glyph.text == "A"] == []
    assert off_the_start.encode("A").unknown == ("A",)


# -- the table forms -------------------------------------------------------


def test_a_table_file_reads_code_first_by_default() -> None:
    """celPix's own order, and the one a bare dropped file is read in.

    ``3D==`` is the case that decides where the line splits: the *first* ``=`` is
    the separator here, so the value is the ``=`` character itself.
    """
    glyphs = parse_table("00=A\n3D==\nFE=[line break]\n# a comment\n\n")
    assert [(g.code, g.text, g.role) for g in glyphs] == [
        (0x00, "A", GlyphRole.TEXT),
        (0x3D, "=", GlyphRole.TEXT),
        # A bracketed value names a *command*: not a letter, and worth calling
        # something. The name is spelled to one word, because it is what the
        # string holds — `[line-break]`.
        (0xFE, "line-break", GlyphRole.CONTROL),
    ]


def test_a_table_file_reads_text_first_when_told_to() -> None:
    """The assembler's spelling of the same thing, and the mirror-image split.

    Here the *last* ``=`` separates, so ``==3D`` is the ``=`` character again.
    Never detected: both sides of ``20=A`` parse as hex, so a guess reads an
    all-hex table backwards and says nothing.
    """
    glyphs = parse_table('cleartable\ntable "x"\nA=00\n==3D\n', order="text-first")
    assert [(g.code, g.text) for g in glyphs] == [(0x00, "A"), (0x3D, "=")]


def test_a_spec_line_is_a_letter_unless_it_says_what_else_it_is() -> None:
    """The common line is a glyph, so it is the one that carries no ``role``.

    A code that punctuates has to say so, since what it *does* is the whole of
    what a reader has to be told about it.
    """
    spec = [
        {"code": 0xFE, "text": "line break", "role": "break"},
        {"code": 0xFD, "name": "end-of-string", "role": "control"},
        {"code": 0xFF, "text": "A"},
    ]
    assert [g.role for g in glyphs_from_spec(spec)] == [
        GlyphRole.BREAK,
        GlyphRole.CONTROL,
        GlyphRole.TEXT,
    ]


@pytest.mark.parametrize(
    ("typed", "codes"),
    [
        ("A[br]B", [1, 0, 2]),  # a named token left over from another tool
        ("A[$ZZ]B", [1, 0, 2]),  # brackets, a $, and not hex
        ("A[]B", [1, 0, 2]),  # empty
        ("A[unclosed", [1, 0]),  # half-typed, and the rest of the line with it
    ],
)
def test_brackets_around_anything_but_a_code_are_one_blank_cell(typed, codes) -> None:
    """The text form's only syntax is ``[$FF]``, so everything else in brackets
    is a mistake to report rather than letters to write.

    Encoding it character by character is the failure this guards: a leftover
    ``[br]`` pasted from another tool would silently become the four codes for
    ``b``, ``r`` and two brackets, and be written to the ROM as text. What it
    costs instead is **one** blank - the piece it is, not the characters it is
    made of, which is the same unit the caret types over.
    """
    encoded = FontAlphabet([Glyph(0, " "), *sequential(1, "ABC")]).encode(typed)
    assert not encoded.ok
    assert list(encoded.codes) == codes


# -- typing over the string -------------------------------------------------


@pytest.mark.parametrize(
    ("body", "inside"),
    [
        # Beside a code is not inside it: on the `[` the caret is standing on a
        # whole piece, which is what lets typing there replace the pair.
        ("AB[$FE]C", [False, False, False, True, True, True, True, False, False]),
        # An unclosed `[` is a code being spelled, and runs to the end of the
        # string - there is nothing after it yet to be outside of.
        ("AB[$F", [False, False, False, True, True, True]),
        # `[[` is a literal `[`, so it is never anything's interior.
        ("A[[B", [False, False, False, False, False]),
    ],
)
def test_the_caret_is_only_inside_a_code_between_its_brackets(body, inside) -> None:
    """The one switch that turns overtyping off, and the only thing that decides
    it is where the caret is (``docs/design/fontmap-entry.md`` §5).

    Inside, the user is spelling a number digit by digit and the string is theirs
    until it is finished; outside, every keystroke lands on a cell.
    """
    from celpix.core.font import inside_code

    assert [inside_code(body, at) for at in range(len(body) + 1)] == inside


def test_a_code_is_one_piece_however_wide_it_reads() -> None:
    """What a keystroke replaces is a piece, not a character - so a five-character
    ``[$FE]`` costs one key to type over, exactly like the letter beside it."""
    from celpix.core.font import unit_spans

    assert unit_spans("A[$FE][[B") == [(0, 1), (1, 6), (6, 8), (8, 9)]
    # Mid-composition the open bracket takes the rest with it: it is one piece
    # being typed, and there is no closing bracket yet to end it.
    assert unit_spans("A[$F") == [(0, 1), (1, 4)]


def test_typing_over_a_code_replaces_the_whole_pair() -> None:
    """Half a code is not a number, so an overtype that landed on the ``[`` and
    left ``$FE]`` behind would put three letters nobody typed into the string."""
    from celpix.core.font import splice, unit_bounds

    # "AB[$FE]C" as the decoder hands it over: one cell per piece.
    body = "AB[$FE]C"
    units = (0, 1, 2, 2, 2, 2, 2, 3)

    start, stop = unit_bounds(units, 2)  # the caret on the `[`
    assert (start, stop) == (2, 7)
    typed, retyped = splice(body, units, start, stop, "D")
    assert typed == "ABDC"
    assert len(retyped) == len(typed)
    # The replacement is its own piece, distinct from the letters either side, so
    # the next keystroke over it replaces it and not its neighbours.
    assert retyped[0:2] == (0, 1) and retyped[3] == 3 and retyped[2] not in (0, 1, 3)


def test_a_caret_inside_a_pair_glyph_still_replaces_the_glyph() -> None:
    """A code standing for ``th`` is one cell spelled with two characters, and the
    caret can sit between them - replacing only the ``t`` would turn one cell into
    two and silently cost the string a character of its budget."""
    from celpix.core.font import splice, unit_bounds

    body, units = "thA", (0, 0, 1)
    assert unit_bounds(units, 1) == (0, 2)
    assert splice(body, units, *unit_bounds(units, 1), "X")[0] == "XA"


def test_digits_typed_into_a_code_join_it_rather_than_becoming_cells() -> None:
    """Composing is the one case where characters go in as themselves, and they
    have to belong to the code being spelled: given pieces of their own, the next
    keystroke over the finished code would replace one digit of it."""
    from celpix.core.font import splice, unit_bounds

    body, units = "A[$F]", (0, 1, 1, 1, 1)
    typed, retyped = splice(body, units, 4, 4, "E", unit=units[3])
    assert typed == "A[$FE]"
    assert retyped == (0, 1, 1, 1, 1, 1)
    assert unit_bounds(retyped, 1) == (1, 6)  # still one piece


# -- a line break the cell carries as a bit ---------------------------------
def test_a_terminator_bit_reads_as_a_newline_after_its_character() -> None:
    """The character and the line end are one cell, so they are one piece.

    The format sets a bit on the line's last character rather than spending a
    code on a terminator (``text-formats.md`` §4.4), so the newline belongs to
    the same cell as the letter before it — which is what a caret standing on it
    has to select, and what one keystroke has to replace.
    """
    alphabet = FontAlphabet(sequential(0, "ABC"), flag_break=True)
    text = alphabet.decode([0, 1, 2], [False, True, False])
    assert text.body == "AB\nC"
    assert text.positions == (0, 1, 1, 2)


def test_a_typed_newline_sets_the_bit_and_costs_no_cell() -> None:
    """The whole reason ``flag_break`` is on the alphabet rather than the writer.

    A newline in such a stream is not a code, so the budget readout must not
    count one — telling the user a string is a cell too long for its region when
    it fits exactly is a refusal they cannot act on.
    """
    alphabet = FontAlphabet(sequential(0, "ABC"), flag_break=True)
    encoded = alphabet.encode("AB\nC")
    assert encoded.ok
    assert encoded.codes == (0, 1, 2)
    assert encoded.ends_line == (False, True, False)
    # And back out again unchanged, which is the invariant the whole module is.
    assert alphabet.decode(encoded.codes, encoded.ends_line).body == "AB\nC"


@pytest.mark.parametrize("typed", ["\nA", "A\n\n"])
def test_a_newline_with_no_bit_left_to_set_is_reported(typed: str) -> None:
    """One bit per cell, so two line breaks in a row need two cells and there is
    only one — and a break before the first character has no cell at all. Both
    would otherwise vanish silently, which is the one thing the encoder never
    does."""
    assert FontAlphabet(sequential(0, "ABC"), flag_break=True).encode(
        typed
    ).unknown == ("\\n",)


def test_a_break_code_beside_the_bit_is_what_makes_a_blank_line_expressible() -> None:
    """A format with both punctuations uses the bit first — it is free — and
    falls back to the code for the newline the bit cannot hold."""
    alphabet = FontAlphabet(
        [*sequential(0, "ABC"), Glyph(0xFE, "line break", GlyphRole.BREAK)],
        flag_break=True,
    )
    encoded = alphabet.encode("A\n\nB")
    assert encoded.ok
    assert encoded.codes == (0, 0xFE, 1)
    assert encoded.ends_line == (True, False, False)
    assert alphabet.decode(encoded.codes, encoded.ends_line).body == "A\n\nB"


def test_which_cells_end_a_line_is_one_rule_for_the_text_and_the_picture() -> None:
    """``ends_line`` and ``decode`` must agree, cell for cell.

    The text window's newline comes out of ``decode`` and the canvas rules the
    same cells' edges, so two rules would eventually mark a cell the text does
    not break at. The case that separates them is a **second** break code: only
    the first declared is the newline, and the rest read as their own name - so
    the mark has to stay off them too.
    """
    alphabet = _alphabet()  # 0xFE is the canonical break, 0xFD the second
    codes = [0, 0xFE, 1, 0xFD, 2, 0xFF]
    flags = [True, False, False, False, False, False]
    body = alphabet.decode(codes, flags).body
    assert body == "A\n\nB[scroll break]C[end of string]"

    marked = [alphabet.ends_line(code, flag) for code, flag in zip(codes, flags)]
    # The same fact read the other way: the piece a cell decodes to ends in a
    # newline exactly when that cell ends a line. Compared, not asserted twice.
    breaks = [
        alphabet.decode([c], [f]).body.endswith("\n") for c, f in zip(codes, flags)
    ]
    assert marked == breaks == [True, True, False, False, False, False]


def test_a_break_a_cell_carries_is_told_apart_from_a_break_code() -> None:
    """Which one it is decides whether a keystroke may replace it.

    A break *code* is a cell of its own, so typing over it swaps a code for a
    letter like any other keystroke. A break a cell **carries** shares that cell
    with the character it ends, and the two are edited separately or the letter
    cannot be retyped without unending the line.
    """
    alphabet = _alphabet()  # 0xFE is the canonical break
    carried = alphabet.decode([0, 1, 0xFE], [False, True, False])
    assert carried.body == "AB\n\n"
    # The first newline is the B's own cell; the second is the break code's.
    assert [carried_break(carried.body, carried.positions, at) for at in range(4)] == [
        False,
        False,
        True,
        False,
    ]


def test_a_named_code_reads_as_its_name_and_types_back() -> None:
    """The fourth case of the text form: ``[wait]`` rather than ``[$2A]``.

    A name is the one thing in the string a *user* supplied, and it is what the
    case buys: `[wait]` says what the byte does where `[$2A]` only says which
    byte it is. Both reach the same cell, so nothing about the round trip moves.
    """
    alphabet = FontAlphabet(
        [
            Glyph(0x00, "A"),
            Glyph(0x2A, "wait", GlyphRole.CONTROL),
            Glyph(0xFE, "line break", GlyphRole.BREAK),
        ]
    )

    text = alphabet.decode([0x00, 0x2A, 0xFE, 0x77])
    # A break is still a newline, and a code nobody named is still its own hex.
    assert text.body == "A[wait]\n[$77]"
    assert alphabet.encode(text.body).codes == (0x00, 0x2A, 0xFE, 0x77)
    # The hex form of a named code still reaches it, so nothing typed before the
    # name existed stops working.
    assert alphabet.encode("[$2A]").codes == (0x2A,)
    # And the insert row writes whichever form round-trips.
    assert alphabet.token(Glyph(0x2A, "wait", GlyphRole.CONTROL)) == "[wait]"


def _commanding() -> FontAlphabet:
    """A font whose `speed` reads the cell after it and whose `window` reads two."""
    return FontAlphabet(
        [
            *sequential(0, "ABCDE"),
            Glyph(0x7A, "speed", GlyphRole.CONTROL, params=1),
            Glyph(0x6B, "window", GlyphRole.CONTROL, params=2),
            Glyph(0xFF, "end", GlyphRole.CONTROL),
        ]
    )


def test_a_command_swallows_the_cells_it_declared_and_types_them_back() -> None:
    """A parameter is a cell of the stream that is *not* a character.

    Without the declaration the byte after `[speed]` reads as whatever letter it
    happens to draw - `[speed]A` for speed $00 - which is honest and unreadable.
    With it the pair reads as one thing and still types back to both cells.
    """
    alphabet = _commanding()

    text = alphabet.decode([0x7A, 0x00, 0x01, 0x6B, 0x02, 0x03, 0xFF])
    assert text.body == "[speed, $00]B[window, $02, $03][end]"
    written = alphabet.encode(text.body).codes
    assert written == (0x7A, 0x00, 0x01, 0x6B, 0x02, 0x03, 0xFF)

    # Every character of the token belongs to the **command's** cell, operands
    # included: it is one thing to type over, and a caret between a command and
    # its operand would be a caret on half a piece.
    assert set(text.positions[: len("[speed, $00]")]) == {0}
    # A command with nothing left to swallow still reads, and still types back.
    tail = alphabet.decode([0x00, 0x7A]).body
    assert tail == "A[speed]"
    assert alphabet.encode(tail).codes == (0x00, 0x7A)


def test_an_operand_that_is_not_a_value_is_reported_rather_than_half_written() -> None:
    """The one mistake a text form cannot show: a command eating the wrong byte.

    Writing `[speed` and then giving up on `, X]` would put the command in the
    stream with whatever followed it as its operand, and nothing on screen would
    say so. So the token is read whole or handed back as unspellable.
    """
    alphabet = _commanding()

    written = alphabet.encode("A[speed, X]B")
    assert written.unknown == ("[speed, X]",)
    # One blank cell where the token was, so nothing after it slides left.
    assert written.codes == (0x00, alphabet.blank, 0x01)

    # The count itself is not policed: a user mid-edit is owed the cells the
    # string says, not the cells the table expected.
    assert alphabet.encode("[speed, $01, $02]").codes == (0x7A, 0x01, 0x02)


def test_a_commands_operand_count_travels_in_the_table_form() -> None:
    """`7A=[speed, 1]` - the count beside the name, where a font table is kept.

    The value varies per occurrence and lives in the stream; the *count* is a
    property of the code and is the only thing a table can state.
    """
    (glyph,) = parse_table("7A=[speed, 1]")
    assert glyph == Glyph(0x7A, "speed", GlyphRole.CONTROL, params=1)
    # A comma that is not a count is part of the name, hyphenated like any other
    # loose spelling rather than read as a declaration.
    assert parse_table("7A=[wait, then go]")[0].params == 0

    # And the insert row writes a token that round-trips: zeroed operands, since
    # nothing can know what the user wants in one.
    assert FontAlphabet([glyph]).token(glyph) == "[speed, $00]"


def test_a_name_two_codes_share_falls_back_to_hex() -> None:
    """Both would parse back to the first, so the second says which byte it is.

    Silently writing the pair as one name is the one thing the text form never
    does: a string that decodes to something typing back to a *different* cell
    is worse than one that reads as a number.
    """
    alphabet = FontAlphabet(
        [
            Glyph(0x2A, "wait", GlyphRole.CONTROL),
            Glyph(0x3B, "wait", GlyphRole.CONTROL),
        ]
    )

    assert alphabet.decode([0x2A, 0x3B]).body == "[wait][$3B]"
    assert alphabet.encode("[wait][$3B]").codes == (0x2A, 0x3B)


def test_a_bracketed_name_the_font_lacks_is_reported_not_written() -> None:
    """Reported whole, the rule an unparseable ``[...]`` already followed: encoded
    letter by letter it would silently write the punctuation as glyphs."""
    alphabet = FontAlphabet([Glyph(0x00, "A"), Glyph(0x2A, "wait", GlyphRole.CONTROL)])

    encoded = alphabet.encode("A[nope]")

    assert encoded.unknown == ("[nope]",)
    assert not encoded.ok
