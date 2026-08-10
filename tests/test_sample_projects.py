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
    load_user_plugins(registry, [project_plugin_dir(str(project))])
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
    params = registry.preset(entry.tilemap_preset_id).params
    alphabet = pipeline.load_alphabet(
        font.alphabet_preset_id,
        registry,
        PipelineContext(),
        controls=params.get("controls", ()),
        code_digits=2,
        base=font.alphabet_base,
        flag_break=bool(params.get("terminator")),
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


def test_the_yi_sample_project_reads_both_of_its_streams() -> None:
    """One font, two streams, and the two halves of the split proved together.

    The **alphabet** is the font's, so both regions read through the one table
    without either having stored it. The **controls** are each stream's own, and
    here they genuinely differ: `$FF` ends a page in the storybook and is an
    escape prefix in the message boxes, so the second declares nothing and reads
    the shipped preset. Asserting both in one test is the point — either alone
    would pass with the split collapsed the wrong way.
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

    # The promise that has to hold whether or not the reading is pretty.
    assert story_exact and boxes_exact
