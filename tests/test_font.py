"""The alphabet: codes to text and back, and the table forms it is stated in.

The regression risk here is the **round trip**. A fontmap's text window is the
only editing surface in celPix that shows a lossy projection of its document, so
every one of these tests is really the same question — does what came out go back
in as the same bytes — asked of the case that would break it.
"""

from __future__ import annotations

import pytest

from celpix.core.font import (
    Alphabet,
    Glyph,
    GlyphRole,
    carried_break,
    glyphs_from_spec,
    parse_table,
    sequential,
)


def _alphabet(**kwargs) -> Alphabet:
    """A font with one of everything a general reader can express: letters, a
    code standing for a pair, two line breaks and a named command."""
    return Alphabet(
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
        # Every other command is its own hex code - including a *second* break,
        # which stays unambiguous precisely by not being a newline too.
        ([0, 0xFD, 1], "A[$FD]B"),
        ([0, 0xFF, 1, 0xFF], "A[$FF]B[$FF]"),
        # A code standing for a pair is spelled out as the pair.
        ([0x20, 0], "thA"),
        # A code nothing claims reads exactly like a named command does: there is
        # one fallback, and it is the whole vocabulary.
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
    alphabet = Alphabet([Glyph(5, "["), *sequential(0, "AB")])
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


def test_what_the_font_cannot_say_is_reported_and_not_substituted() -> None:
    """Unknown characters come back as a list, in order, without repeats.

    Never raised and never substituted: the window has to keep showing what the
    user typed while refusing to write it, and a silent fallback character is how
    a string gets saved with a letter nobody typed.
    """
    encoded = _alphabet().encode("AzBz!")
    assert encoded.unknown == ("z", "!")
    assert not encoded.ok
    # The encodable subset only - which is why a caller must not write this.
    assert list(encoded.codes) == [0, 1]


def test_a_newline_with_no_break_code_is_unknown_rather_than_dropped() -> None:
    """A bare index-only run has no punctuation, so Enter has nothing to encode to.

    Dropping it silently would let a user lay out a string that the file cannot
    hold and be told nothing about it.
    """
    plain = Alphabet(sequential(0, "AB"))
    assert plain.line_break is None
    assert plain.encode("A\nB").unknown == ("\\n",)


def test_the_stream_controls_win_over_the_fonts_letters() -> None:
    """A code the font spells and the stream reserves belongs to the **stream**.

    The font's table was authored against tiles and has no way of knowing which
    codes a given text format has taken for itself, so the merge is one-way
    (``docs/design/fontmap-entry.md`` §3).
    """
    font = Alphabet(sequential(0, "ABC"))
    controls = Alphabet([Glyph(2, "end of string", GlyphRole.CONTROL)])
    merged = font.merged(controls)
    assert merged.decode([0, 1, 2]).body == "AB[$02]"
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
        # something. It still reads as `[$FE]` in the text like any other.
        (0xFE, "line break", GlyphRole.CONTROL),
    ]


def test_a_table_file_reads_text_first_when_told_to() -> None:
    """The assembler's spelling of the same thing, and the mirror-image split.

    Here the *last* ``=`` separates, so ``==3D`` is the ``=`` character again.
    Never detected: both sides of ``20=A`` parse as hex, so a guess reads an
    all-hex table backwards and says nothing.
    """
    glyphs = parse_table('cleartable\ntable "x"\nA=00\n==3D\n', order="text-first")
    assert [(g.code, g.text) for g in glyphs] == [(0x00, "A"), (0x3D, "=")]


def test_a_control_spec_defaults_to_control_and_a_font_spec_to_text() -> None:
    """Who is stating the list decides what an entry with no ``role`` is.

    A font's glyphs are letters unless they say otherwise and a cell format's
    controls are controls unless they say otherwise, so neither has to spell out
    the only thing its every line could be.
    """
    spec = [
        {"code": 0xFE, "text": "line break", "role": "break"},
        {"code": 0xFF, "text": "end of string"},
    ]
    assert [g.role for g in glyphs_from_spec(spec)] == [
        GlyphRole.BREAK,
        GlyphRole.TEXT,
    ]
    assert [g.role for g in glyphs_from_spec(spec, GlyphRole.CONTROL)] == [
        GlyphRole.BREAK,
        GlyphRole.CONTROL,
    ]


@pytest.mark.parametrize(
    ("typed", "codes"),
    [
        ("A[br]B", [0, 1]),  # a named token left over from another tool
        ("A[$ZZ]B", [0, 1]),  # brackets, a $, and not hex
        ("A[]B", [0, 1]),  # empty
        ("A[unclosed", [0]),  # half-typed, and the rest of the line with it
    ],
)
def test_brackets_around_anything_but_a_code_are_refused(typed, codes) -> None:
    """The text form's only syntax is ``[$FF]``, so everything else in brackets
    is a mistake to report rather than letters to write.

    Encoding it character by character is the failure this guards: a leftover
    ``[br]`` pasted from another tool would silently become the four codes for
    ``b``, ``r`` and two brackets, and be written to the ROM as text.
    """
    encoded = Alphabet(sequential(0, "ABC")).encode(typed)
    assert not encoded.ok
    assert list(encoded.codes) == codes  # the letters outside, and nothing more


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
    alphabet = Alphabet(sequential(0, "ABC"), flag_break=True)
    text = alphabet.decode([0, 1, 2], [False, True, False])
    assert text.body == "AB\nC"
    assert text.positions == (0, 1, 1, 2)


def test_a_typed_newline_sets_the_bit_and_costs_no_cell() -> None:
    """The whole reason ``flag_break`` is on the alphabet rather than the writer.

    A newline in such a stream is not a code, so the budget readout must not
    count one — telling the user a string is a cell too long for its region when
    it fits exactly is a refusal they cannot act on.
    """
    alphabet = Alphabet(sequential(0, "ABC"), flag_break=True)
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
    assert Alphabet(sequential(0, "ABC"), flag_break=True).encode(typed).unknown == (
        "\\n",
    )


def test_a_break_code_beside_the_bit_is_what_makes_a_blank_line_expressible() -> None:
    """A format with both punctuations uses the bit first — it is free — and
    falls back to the code for the newline the bit cannot hold."""
    alphabet = Alphabet(
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
    the first declared is the newline, and the rest stay hex - so the mark has to
    stay off them too.
    """
    alphabet = _alphabet()  # 0xFE is the canonical break, 0xFD the second
    codes = [0, 0xFE, 1, 0xFD, 2, 0xFF]
    flags = [True, False, False, False, False, False]
    body = alphabet.decode(codes, flags).body
    assert body == "A\n\nB[$FD]C[$FF]"

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
