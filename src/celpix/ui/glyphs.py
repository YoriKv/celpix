"""The icon glyphs celPix draws, as codepoints in the bundled icon font.

Qt-free on purpose, so the tool and transform tables in :mod:`celpix.ui.tools`
can name a button's face as plain data the way they already name its rasterizer
and its key; :mod:`celpix.ui.icon_font` is the half that needs a QPainter.

The font is **Material Symbols Outlined** (Apache 2.0), subset to exactly the
codepoints below — see ``tools/subset_icon_font.py``, which reads this enum, so
**adding a member here means re-running that script** or the new icon draws as
nothing. The members are named for what the mark *is to celPix*, not for what
upstream calls it: the two are unrelated (``height`` is a vertical double arrow,
``priority_high`` an exclamation mark), and naming ours after theirs would mean
renaming every call site the next time the font changes. Each comment carries
the upstream name, which is what to search for at
<https://fonts.google.com/icons?icon.style=Outlined>.

Codepoints are spelled as escapes because they sit in the private-use block and
would otherwise be an invisible glyph in the source.
"""

from __future__ import annotations

from enum import Enum


class Glyph(Enum):
    """One icon in the bundled font. ``value`` is the character to draw."""

    # The drawing tools, and the color editor's pick button (which wears the
    # sampler's own mark, so the button and the tool read as one gesture).
    PENCIL = "\uf097"  # edit
    EYE_DROPPER = "\ue3b8"  # colorize
    # `colors` rather than the canonical `format_color_fill`: that one carries a
    # solid colour bar under the bucket which stays black whatever the weight
    # axis is set to, and reads as an underline beside these light strokes.
    PAINT_BUCKET = "\ue997"  # colors

    # The file list's row markers.
    FLAG = "\uf0c6"  # flag - a bookmark row's ribbon
    IMAGE = "\ue3f4"  # image - a pixel slice: its own little graphic
    QUESTION = "\ueb8b"  # question_mark - this entry's file is unaccounted for
    EXCLAMATION = "\ue645"  # priority_high - it opened, but something had to give

    # The three tilemap layouts, which are one family on purpose: the same
    # framed grid, its cells arranged three ways. What tells them apart at 13x16
    # is how many cells there are and whether they are cells or rows — the
    # tooltip carries the name, since a marker that subtle is a reminder rather
    # than an introduction.
    GRID = "\ue3ec"  # grid_on - an even lattice: a plain tilemap
    GRID_LARGE = "\ue9b0"  # grid_view - looser cells: a sprite map
    GRID_ROWS = "\ue8ef"  # view_list - cells fused into rows: a fontmap

    # The navigation bar's step buttons. All four are the same construction — a
    # shaft with a head — so the row of them reads as one control; the chevrons
    # and the solid triangles both look like something that expands.
    ARROW_DOWN = "\ue5db"  # arrow_downward
    ARROW_UP = "\ue5d8"  # arrow_upward
    ARROW_LEFT = "\ue5c4"  # arrow_back
    ARROW_RIGHT = "\ue5c8"  # arrow_forward

    # Two marks that sit on a button rather than in a list.
    FUNNEL = "\uef4f"  # filter_alt - "filter this list"
    # `adjust` is a ring with a dot at its centre. It marks "go to what this
    # names", and reads as a target rather than as a direction: the button does
    # not step somewhere relative to here, it opens the one thing a control
    # already names.
    TARGET = "\ue39e"  # adjust

    # The transform bar's flip/rotate buttons, one pair per axis. The flips are
    # double-headed arrows rather than a mirror-and-dashed-line "flip" icon,
    # which is what the bar has always shown and what survives 16px.
    FLIP_HORIZONTAL = "\uf69b"  # arrow_range
    FLIP_VERTICAL = "\uea16"  # height
    ROTATE_RIGHT = "\ue41a"  # rotate_right
    ROTATE_LEFT = "\ue419"  # rotate_left
