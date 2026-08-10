"""The sample projects, opened the way the app opens one.

Not unit tests of anything in ``src`` — they are the guard on
``sample-projects/``. Those are **worked examples**: a real cart, a real font,
real text regions, and the project plugins that name them. Their whole value is
that they work, and the one thing that can silently stop being true is that the
addresses, the preset ids or the project schema moved underneath them while
nobody had the cart to hand.

So each test asks the only question worth asking — does the text read — through
the real project loader and the real pipeline.

They **skip** unless both the project and its cart are there, which is the normal
case: ``sample-projects/`` is git-ignored, and each project addresses a ROM in a
sibling checkout by relative path (``docs/README.md``). So these run on a machine
laid out like the one that authored them and stay quiet everywhere else — which
is the most a tracked test can do for untracked data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from celpix.core.capabilities import ContentKind
from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef
from celpix.plugins.discovery import load_user_plugins, project_plugin_dir
from celpix.plugins.registry import default_registry
from celpix.project.projectfile import load_project

ROOT = Path(__file__).resolve().parents[1] / "sample-projects"


def _open(name: str):
    """A sample project's workspace and the registry it decodes through.

    The registry is built the way the app builds one for an open project —
    built-ins plus that project's own ``plugins/`` — because for these two that
    folder is where the cell formats, the alphabets and (for YI) the pixel format
    all live. A registry without it decodes nothing.
    """
    project = ROOT / name
    if not project.is_file():
        pytest.skip(f"{name} not present")
    workspace = load_project(str(project))
    if not all(Path(entry.path).is_file() for entry in workspace.entries):
        pytest.skip("cart not present - see sample-projects/README.md")
    registry = default_registry()
    # Approved rather than prompted, because a project's own ``plugins/`` is as
    # trusted as the project (``discovery`` module docstring) and a test has
    # nobody to ask. Without it a code plugin is refused by default and the
    # entries that name one open with no format at all.
    load_user_plugins(
        registry, [project_plugin_dir(str(project))], confirm=lambda *a, **k: True
    )
    return workspace, registry


def _read(workspace, registry, name: str) -> tuple[str, bool]:
    """One fontmap entry's text, and whether it types back to the same bytes.

    The font is reached **through the binding** rather than by name, because that
    is the claim being tested: the alphabet belongs to whichever entry supplies
    the tiles, and the controls to the stream's own cell format.
    """
    entry = next(e for e in workspace.entries if e.name == name)
    font = entry.tile_source.entry
    assert font.content_kind is ContentKind.PIXELS

    loaded = pipeline.load_tilemap_data(
        PathwayConfig(
            source=FileRef(
                paths=[entry.path],
                offset=entry.slice_offset,
                length=entry.slice_length,
            ),
            interpret_preset_id=entry.tilemap_preset_id,
        ),
        registry,
    )
    preset = registry.preset(entry.tilemap_preset_id)
    params = preset.params
    engine = registry.plugin(Stage.INTERPRET_TILEMAP, preset.engine_id)
    alphabet = pipeline.load_font_alphabet(
        font.font_chars,
        font.font_codes,
        PipelineContext(),
        controls=params.get("controls", ()),
        code_digits=2,
        base=font.font_base,
        flag_break=engine.has_line_flag(params),
    )
    codes = [cell.index for cell in loaded.cells]
    ends = [cell.ends_line for cell in loaded.cells]
    body = alphabet.decode(codes, ends).body
    typed = alphabet.encode(body)
    # Both halves of a cell, since on a flag-terminated format the line break is
    # the second one: codes alone would agree while every break had moved.
    return body, (list(typed.codes), list(typed.ends_line)) == (codes, ends)


def test_the_smw_sample_project_reads_its_level_names_as_words() -> None:
    """The whole worked example in one assertion.

    Every layer has to be right for this string to come out: the slice
    addresses, the LZ2 unpack of the font, the binding, the text-first table, and
    the cell format splitting the terminator bit out of the character. Any one of
    them wrong and the text is hex or nonsense - which is exactly why this is the
    assertion rather than five weaker ones.
    """
    workspace, registry = _open("smw-text/smw-text.celpix")
    body, exact = _read(workspace, registry, "level names")

    lines = body.splitlines()
    # The trailing space is the terminator byte itself: `$9F` is a space with the
    # bit set, and it is how nearly every name in this region ends.
    assert lines[:3] == ["YOSHI'S ", "STAR ", "#1 IGGY'S "]
    assert "TOP SECRET AREA " in lines
    # And the name that ends on a letter instead reads as that letter, because
    # the bit was never part of it: `PALAC` + `E|$80` is `PALACE`, one line.
    assert "PALACE" in lines
    assert exact


def test_the_smw_sample_project_reads_its_message_boxes_as_sentences() -> None:
    """The same cart's other region, and the case the terminator bit is for.

    Its lines end on whatever letter falls in column 18 rather than on a space,
    so before the bit had a field of its own every one of them read as a stray
    hex code and drew a tile past the end of the sheet.
    """
    workspace, registry = _open("smw-text/smw-text.celpix")
    body, exact = _read(workspace, registry, "message boxes")

    assert body.startswith("Welcome!   This is\nDinosaur Land.  In\n")
    assert "[$" not in body.split("Looks")[0]
    assert exact


def test_the_smw_sample_project_reads_its_stripe_text_through_a_second_font() -> None:
    """The cart's other way of storing a string, over its other Layer 3 sheet.

    A stripe image is tilemap words copied straight to VRAM, so the cell is two
    bytes and the character is one of them: `index` takes the low byte, `flags`
    carries the palette and priority beside it. Both facts are load-bearing —
    without the split the letters would be right and the colours lost on the
    first edit, and without the second font entry the codes would index the
    wrong sheet.
    """
    workspace, registry = _open("smw-text/smw-text.celpix")

    clear, clear_exact = _read(workspace, registry, "course clear")
    # `[$51]0` is a four-byte stripe header read as two cells. The `0` is real:
    # the header's third byte is $00 and this sheet draws a `0` at tile $00.
    assert "[$51]0COURSE CLEAR!" in clear

    lives, lives_exact = _read(workspace, registry, "life exchange")
    # The same letters one palette along - `$28` here against `$38` on the score
    # card - which reads identically because the attribute is not the character.
    assert "MARIO" in lives and "LUIGI" in lives

    stars, stars_exact = _read(workspace, registry, "bonus stars")
    assert "BONUS!" in stars

    # And the two byte-per-character regions still read through the *other*
    # sheet, which is the half a second font entry could quietly have broken.
    names, names_exact = _read(workspace, registry, "level names")
    assert names.splitlines()[0] == "YOSHI'S "

    assert clear_exact and lives_exact and stars_exact and names_exact


def test_the_yi_sample_project_reads_all_of_its_streams() -> None:
    """One font, four streams over it, and the two halves of the split together.

    The **alphabet** is the font's, so all four regions read through the one
    table without any of them having stored it. The **controls** are each
    stream's own, and here they genuinely differ: `$FF` ends a page in the
    storybook, is an escape prefix in the message boxes and the ending text
    (which therefore declare nothing and read the shipped preset), and is a
    column-setting prefix in the level names, which break on `$FD` instead.
    Asserting them in one test is the point — any one alone would pass with the
    split collapsed the wrong way.

    The credits are a fifth stream and a *second* font entry, tested separately
    below: their codes are not this font's codes.
    """
    workspace, registry = _open("yi-text/yi-text.celpix")

    story, story_exact = _read(workspace, registry, "storybook intro")
    assert story.startswith("A long, long time ago ...")
    assert "baby Mario and Yoshi." in story
    # Positioning codes take a parameter byte each and nothing says so, so they
    # read as their own hex - the case fontmap-entry.md §5 chose over describing
    # per-command arity.
    assert "[$FE][$02][$FD][$10][$FC][$38]This is a story about" in story

    boxes, boxes_exact = _read(workspace, registry, "message boxes")
    # A 16-bit `$XXFF` control split by a one-byte cell. Ugly, and exactly right:
    # this is the format the text form was argued against, kept here as evidence.
    assert boxes.startswith("[$FF][$05]This paradise is[$FF][$06]Yoshi's Island,")

    ending, ending_exact = _read(workspace, registry, "ending text")
    # The same 16-bit controls, and the one region of the four whose prose
    # therefore starts at cell zero with nothing in front of it.
    assert ending.startswith("Thus, due to the marvelous[$FF][$0A]team work of")

    names, names_exact = _read(workspace, registry, "level names")
    # `$FD` ends a name here and is a set-Y prefix in the storybook - the same
    # byte, two streams, and the reason a stream's controls are not the font's.
    lines = names.splitlines()
    assert lines[0] == "[$FF][$00]      Welcome To[$FE][$10][$00]   Yoshi's Island"
    assert "[$FF][$00]1 - 1:  Make Eggs,[$FE][$10][$00]         Throw Eggs" in lines
    # 54 level names, the world-1 splash, and the garbage sentinel the 21
    # unreachable pointer slots share.
    assert len(lines) == 56

    # The promise that has to hold whether or not the reading is pretty.
    assert story_exact and boxes_exact and ending_exact and names_exact


def test_the_yi_credits_read_through_a_second_font_entry() -> None:
    """The case that needs two entries over one run of bytes, end to end.

    The credits roll references a sprite sheet the chip rasterizes from the
    message font, so a letter byte is a *position in that sheet* and not a font
    code. Everything it takes is in this project rather than in celPix:

    - a **second slice** of the same 3072 font bytes, carrying its own alphabet,
      which is the whole reason an alphabet belongs to an entry and not to a file;
    - a **reshape** putting those records in the stream's order, so the map draws
      the glyph each cell names;
    - a cell format placing the letter on ``index`` and the advance byte on
      ``flags``, which is what carries a number celPix has no meaning for through
      an edit instead of zeroing it.

    Drop any one and this reads as hex, draws the wrong glyphs, or corrupts the
    line spacing on save.
    """
    workspace, registry = _open("yi-text/yi-text.celpix")
    body, exact = _read(workspace, registry, "credits")

    lines = body.splitlines()
    # `[$57]` is the page header's X; `[$DA]`/`[$5C]` the cursor glyphs either
    # side of a heading, which no single character stands for and which
    # therefore read as themselves.
    assert lines[0] == "[$57][$DA]Directors[$5C]"
    # Two words with nothing between them, and that is the format rather than a
    # misreading: word gaps are folded into the preceding letter's advance, so
    # the stream has no space code for the text window to find.
    assert "[$49]TakashiTezuka" in lines
    assert "[$43]ShigeruMiyamoto" in lines
    assert exact


def test_the_yi_credits_font_reshape_is_an_exact_permutation() -> None:
    """The half of the credits entry the text cannot prove: the picture.

    ``_read`` above exercises codes and controls; it never asks which glyph a
    cell draws. This does, on the two facts a save depends on — that the record
    reached by a letter byte is the one the font keeps at that letter's code, and
    that ``unshape`` puts every byte back. A reshape whose inverse is wrong
    corrupts the font on the first pixel edit, silently.
    """
    project = ROOT / "yi-text/yi-text.celpix"
    if not project.is_file():
        pytest.skip("yi-text not present")
    workspace, registry = _open("yi-text/yi-text.celpix")
    entry = next(e for e in workspace.entries if e.name == "credits font")
    source = Path(entry.path)
    font = source.read_bytes()[
        entry.slice_offset : entry.slice_offset + entry.slice_length
    ]

    reshape = registry.plugin(Stage.RESHAPE, entry.reshape_id)
    sheet = reshape.reshape(font, PipelineContext())

    # 'A' is font code $AA and letter byte $B2; 'i' is $E0 and $98.
    assert sheet[0xB2 * 12 : 0xB3 * 12] == font[0xAA * 12 : 0xAB * 12]
    assert sheet[0x98 * 12 : 0x99 * 12] == font[0xE0 * 12 : 0xE1 * 12]
    assert reshape.unshape(sheet, PipelineContext()) == font
