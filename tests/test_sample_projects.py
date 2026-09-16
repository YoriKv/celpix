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

from celpix.core.arrangement import BlockLayout
from celpix.core.capabilities import ContentKind
from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_INPUTS,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef
from celpix.plugins.discovery import load_user_plugins, project_plugin_dir
from celpix.plugins.registry import default_registry
from celpix.project.projectfile import load_project
from celpix.project.workspace import (
    EntryKind,
    Workspace,
    pixel_config_for,
    tilemap_config_for,
)

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
    # A **composite** names no file — it is assembled out of other entries, and
    # its path is empty by construction — so it is the one kind this cannot ask
    # about. Reading it as a missing cart skipped every test in the file.
    named = [e for e in workspace.entries if e.kind is not EntryKind.COMPOSITE]
    if not all(Path(entry.path).is_file() for entry in named):
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


def _workspace(loaded) -> Workspace:  # noqa: ANN001 — a LoadedProject
    """``loaded``'s entries adopted into a workspace, the way the app does it —
    what a config factory needs to find a slice's parent."""
    ws = Workspace()
    ws.replace(loaded.entries, loaded.current)
    return ws


def _read(workspace, registry, name: str) -> tuple[str, bool]:
    """One fontmap entry's text, and whether it types back to the same bytes.

    The font is reached **through the binding** rather than by name, because that
    is the claim being tested: the whole alphabet — letters and punctuation both
    — belongs to whichever entry supplies the tiles, and the cell format states
    only where the bits go.
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


def test_the_yi_sample_project_reads_all_of_its_streams() -> None:
    """One run of font bytes, four streams, and three readings of it.

    The whole alphabet is the **font entry's** — the letters and the punctuation
    alike — so a stream that punctuates differently is a second entry over the
    same 3072 bytes rather than a second cell format. All three here carry the
    same 74 glyphs and disagree only about the top four codes: `$FF` ends a page
    on `storybook font`, opens a level name's first line on `level-name font`
    (which breaks on `$FD`, the storybook's row prefix), and is an escape prefix
    on the plain `message font` the boxes and the ending text read through, which
    therefore names nothing at all.

    Asserting all four in one test is the point — any one alone would pass with
    the readings collapsed onto each other. The two one-byte streams share the
    shipped `text-8bit` preset, which is what that split buys: a cell format that
    states only where the bits go is reusable, and a table of codes is not.

    The credits are a fifth stream and a fourth font entry, tested separately
    below: their codes are not these codes at all.
    """
    workspace, registry = _open("yi-text/yi-text.celpix")

    story, story_exact = _read(workspace, registry, "storybook intro")
    assert story.startswith("A long, long time ago ...")
    assert "baby Mario and Yoshi." in story
    # The positioning codes are named, so each reads as a word - and each one's
    # *parameter* still reads as its own hex, which is the whole of what naming
    # buys and does not buy (fontmap-entry.md §5: no per-command arity).
    assert "[line][$02][set-row][$10][set-column][$38]This is a story about" in story

    boxes, boxes_exact = _read(workspace, registry, "message boxes")
    # A 16-bit `$XXFF` control split by a one-byte cell. Ugly, and exactly right:
    # this is the format the text form was argued against, kept here as evidence.
    assert boxes.startswith("[$FF][$05]This paradise is[$FF][$06]Yoshi's Island,")

    ending, ending_exact = _read(workspace, registry, "ending text")
    # The same 16-bit controls, and the one region of the four whose prose
    # therefore starts at cell zero with nothing in front of it.
    assert ending.startswith("Thus, due to the marvelous[$FF][$0A]team work of")

    names, names_exact = _read(workspace, registry, "level names")
    # `$FD` ends a name here and sets a row in the storybook - the same byte,
    # the same tiles, two font entries, two answers, and the reason a font's
    # punctuation is a *reading* of it. `$FE` is the same story one code along.
    lines = names.splitlines()
    assert lines[0] == (
        "[set-column][$00]      Welcome To[set-position][$10][$00]   Yoshi's Island"
    )
    assert (
        "[set-column][$00]1 - 1:  Make Eggs,[set-position][$10][$00]         Throw Eggs"
        in lines
    )
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


def test_the_alttp_sample_project_reads_its_dialogue_as_sentences() -> None:
    """The cart whose font is 8x16 and whose text is a dictionary.

    Two things have to be right that no other sample exercises. The **glyph** is
    two tiles rather than one, stated as the sheet's own Pattern and nothing
    else — no reshape, no glyph codec — so what draws code `c` is the block at
    `c`, which has to come out as the game's own
    ``top = ((c & $F0) << 1) | (c & $0F)``. And the top of the byte space is a
    **dictionary**: 97 codes standing for `the`, `you` and the rest, which the
    text has to spell out and type back.
    """
    workspace, registry = _open("alttp/alttp.celpix")
    body, exact = _read(
        workspace, registry, "Dialogue — first block, $E0000-$E7F29 (text)"
    )

    assert "They have taken her to\nthe castle." in body
    assert "Prices as marked!" in body
    # A dictionary code reading as the characters it stands for, and the whole
    # region typing back to the same bytes — the two halves of that being
    # lossless. `$E3` is `you` and `$D8` is `the`, both above the 128-glyph sheet.
    assert "the wise men will open." in body
    assert exact

    # The font's geometry, which the text above says nothing about. The Pattern
    # is the whole statement: 16 blocks per sheet row, two tiles each, tops row
    # then bottoms row (``docs/design/fontmap-entry.md`` §4).
    font = next(e for e in workspace.entries if e.name.startswith("Dialogue font"))

    # The character space is the game's own: `.widths` in vwf.asm has an entry
    # for $00-$62 and stops, which the disassembly remarks on itself. $5F-$61 are
    # a second I, i and ! — `Text_FilterPlayerNameCharacters` rewrites a stored
    # name's $5F/$60/$61 to $08/$22/$3E, which are exactly those three — so they
    # keep their codes here and read as hex, the canonical spellings having been
    # claimed thirty codes earlier.
    assert len(font.font_chars) == 0x63
    assert font.font_chars[0x5F:0x62] == "Ii!"
    assert font.font_chars[0x08], font.font_chars[0x22] == ("I", "i")

    view = font.pending_view
    assert (view.columns, view.block_columns, view.block_rows) == (16, 1, 2)
    assert view.block_order == "row-interleave"
    assert font.reshape_id in (None, "", "reshape.none")

    layout = BlockLayout(
        view.columns, view.block_columns, view.block_rows, view.block_order
    )
    assert layout.blocks(256) == 128  # a 4096-byte 2bpp sheet is 128 glyphs
    for code in range(128):
        top = ((code & 0xF0) << 1) | (code & 0x0F)
        assert layout.block_slots(code) == [top, top + 16]

    # And the same bytes carved a second time with the plain arrangement, which
    # is how the console actually has them: `CopyFontToVram` copies all $1000 to
    # VRAM as one 2bpp block for BG3, so every tile is also an ordinary 8x8 tile
    # a tilemap can index — which is what the HUD and the file select do, and
    # what the sets past tile $C7 are for.
    flat = next(e for e in workspace.entries if e.name.startswith("Font sheet as BG3"))
    assert (flat.slice_offset, flat.slice_length) == (font.slice_offset, 0x1000)
    assert (flat.pending_view.block_columns, flat.pending_view.block_rows) == (1, 1)


def test_the_counting_cafe_huffman_art_decodes_through_its_bound_inputs() -> None:
    """The cart that motivated plugin inputs (``docs/design/plugin-inputs.md``).

    Its Huffman node tables sit outside the streams, shared by ten and
    thirty-nine of them, and the decoded lengths in a third table. The project's
    own codec declares both as inputs and every art slice binds them to the ROM
    — the table as a region, the length *read from bytes* — so what is asserted
    is the whole path: project file, binding resolution against the parent,
    the context hand-off, and the codec, byte-identical to the reference decoder
    the survey was made with. Then the same table writes the bytes back.
    """
    import importlib.util  # noqa: PLC0415 — the reference decoder lives beside the sample
    import sys  # noqa: PLC0415

    workspace, registry = _open("counting-cafe/counting-cafe.celpix")
    ws = _workspace(workspace)
    spec = importlib.util.spec_from_file_location(
        "cafe", ROOT / "counting-cafe" / "cafe.py"
    )
    cafe = importlib.util.module_from_spec(spec)
    # Registered before it runs: its dataclasses resolve their annotations
    # through ``sys.modules[__module__]`` and find nothing otherwise.
    sys.modules["cafe"] = cafe
    spec.loader.exec_module(cafe)

    art = [e for e in ws.entries if e.name.startswith("huffman — ") and "art" in e.name]
    assert len(art) == 49
    packed = 0
    for entry, reference in zip(art, [*cafe.BG_SETS, *cafe.SPRITE_SETS], strict=True):
        cfg = pixel_config_for(entry, entry.session.pixel_preset_id, registry, ws)
        assert cfg.compression_id == "compression.counting-cafe-huffman", entry.name
        assert cfg.write_enabled, entry.name
        px = pipeline.load_pixel_data(cfg, registry)
        expected, consumed = cafe.huffman(
            reference.tree, reference.offset, reference.out_len
        )
        assert px.data == expected, entry.name
        assert len(px.data) == reference.out_len
        # The size came from the length table, so the decode found its own end
        # and the consumed extent is the slot the generator cut the slice at.
        assert px.ctx.get(KEY_DECOMPRESS_COMPLETE) is True
        assert px.ctx.get(KEY_COMPRESSED_SIZE) == consumed == entry.slice_length
        packed += consumed
    assert packed == 104_012

    # Written back through the same table: the codes are the tree's, so a
    # round trip is exact even where the game's packer padded differently.
    entry = art[0]
    cfg = pixel_config_for(entry, entry.session.pixel_preset_id, registry, ws)
    px = pipeline.load_pixel_data(cfg, registry)
    plugin = registry.plugin(Stage.COMPRESSION, cfg.compression_id)
    ctx = PipelineContext()
    ctx.set(KEY_INPUTS, cfg.inputs[Stage.COMPRESSION])
    again = plugin.decompress(plugin.compress(px.data, ctx), ctx)
    assert again == px.data


def test_the_counting_cafe_sprite_mappings_frame_through_five_bound_arrays() -> None:
    """The second consumer of plugin inputs, one stage over from the first.

    A frame here is a byte range into three parallel word tables plus a size
    from a fourth, and the entry's own bytes are only the attribute words; the
    other five arrays are region inputs the mapping format declares. What is
    asserted is every piece of all 277 frames against the reference reader —
    position, size, tile, row, flips — with each frame's tiles offset to where
    its art lands in the composite the entry draws through: its raw frame for
    0-132, and for the rest the Huffman sprite set its animations name, found by
    walking the four bound animation tables.
    """
    import importlib.util  # noqa: PLC0415 — the reference decoder lives beside the sample
    import sys  # noqa: PLC0415

    workspace, registry = _open("counting-cafe/counting-cafe.celpix")
    ws = _workspace(workspace)
    spec = importlib.util.spec_from_file_location(
        "cafe", ROOT / "counting-cafe" / "cafe.py"
    )
    cafe = importlib.util.module_from_spec(spec)
    sys.modules["cafe"] = cafe
    spec.loader.exec_module(cafe)

    entry = next(e for e in ws.entries if e.name.startswith("sprite mappings"))
    assert entry.tile_source is not None
    assert entry.tile_source.entry.kind is EntryKind.COMPOSITE
    cfg = tilemap_config_for(entry, entry.tilemap_preset_id, registry, ws)
    assert cfg.interpret_preset_id == entry.tilemap_preset_id  # nothing degraded
    loaded = pipeline.load_tilemap_data(cfg, registry)
    assert len(loaded.cells) == cafe.MAP_PIECES
    assert len(loaded.frames) == cafe.MAP_FRAMES

    assert entry.tile_source.entry.pieces[-1].entry.name.startswith(
        "huffman — sprite art 38"
    )
    bases, total = [], 0
    for _offset, length in cafe.FRAMES:
        bases.append(total // 32)
        total += length
    set_starts = []
    for art in cafe.SPRITE_SETS:
        set_starts.append(total // 32)
        total += art.out_len
    bases += [
        set_starts[cafe.frame_sets()[i]]
        for i in range(cafe.FRAME_COUNT, cafe.MAP_FRAMES)
    ]
    for i in range(cafe.MAP_FRAMES):
        expected = cafe.mapping(i)
        got = loaded.frames[i]
        assert len(got) == len(expected), i
        for piece, sub in zip(expected, got, strict=True):
            assert (sub.x, sub.y, sub.across, sub.down) == (
                piece.x,
                piece.y,
                piece.w,
                piece.h,
            )
            assert sub.index == (piece.attr & 0x7FF) + bases[i]
            assert sub.palette_row == (piece.attr >> 13) & 3
            assert (sub.flip_h, sub.flip_v) == (
                bool(piece.attr & 0x800),
                bool(piece.attr & 0x1000),
            )
            assert sub.column_major
