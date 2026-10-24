"""The interpreted sprite-object model: frames of freely placed subsprites.

The third thing a file of tile *references* can be, beside a pixel document and
a :class:`~celpix.core.tilemap.CellGrid`. A tilemap says "this cell of the grid
draws that tile"; a sprite object says "this **subsprite** draws that tile at this
pixel offset from the object's origin", and the offsets are not multiples of the
tile size — measured over the 1,341 sprite files of the S-CG-CAD corpus, only 45%
of X offsets and 34% of Y offsets are 8-aligned
(``docs/graphics-formats-reference/scgcad-formats.md`` §8).

That single fact is why this model exists rather than the cell grid being reused:
a grid cannot express a tile at pixel offset 3, and quantising to the nearest
cell would move most of a sprite's subsprites. Everything else about the entry —
binding to the tile bank it draws from, the palette, the save path — is the
tilemap entry's, unchanged (``docs/design/sprite-map.md`` §1).

A **frame** is one drawing of the object: a run of subsprites, drawn back to
front. A
file holds a fixed number of frames whether or not the artist used them, so what
is worth showing is the run up to the last one with anything in it.

Qt-free, like the rest of ``core``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from celpix.core.tilemap import VRAM_ROW_STRIDE, tile_run

# An object's two subsprite sizes, **as multiples of the tile size**, for the
# formats whose record holds a size *bit* rather than a size: the pair itself is
# not in those files — it was a register the game set, so it is a parameter of
# reading one. A format that states each subsprite's rectangle outright needs
# none of this, and says so by declaring `subsprite_size = "stated"`.
#
# Multiples rather than pixels, because that is what a subsprite is built from: a
# subsprite is a rectangle of *tiles*, so `(1, 2)` says "one tile, or two by two"
# and stays true whatever the bound codec's tile size turns out to be. Pixels
# would have to be divided by it again at every use, and each division is a place
# for an assumed 8 to hide.
DEFAULT_SUBSPRITE_TILES: tuple[int, int] = (1, 2)


@dataclass(frozen=True, slots=True)
class Subsprite:
    """One rectangle of tiles from a sprite frame, at a signed offset from the origin.

    ``across`` and ``down`` are its size **in tiles**, stated rather than picked
    from a setting: a format that records a rectangle can say so, and one whose
    record holds a size *bit* resolves that bit against the object's size pair
    while decoding (:data:`DEFAULT_SUBSPRITE_TILES`). Either way what reaches the
    renderer is a shape, which is what it needs — the two consoles disagree about
    whether a subsprite may be oblong, and only the codec knows which is reading.

    ``column_major`` is the other thing only the codec knows: whether the
    rectangle's tiles run down each column or across each row
    (:func:`~celpix.core.tilemap.tile_run`).

    ``group`` is the authoring tool's own byte and means nothing here. It is
    carried for the reason every unmodelled field is — a field dropped at decode
    time is a field silently zeroed on the next save.

    ``record`` is the piece's **identity**: the index into the document's cell
    list of the cell it was decoded from, which is what an edit to the piece
    writes to. ``(frame, k)`` into the drawn frames cannot serve — a slot format
    drops its undrawn slots on the way to a frame, so *k* is "the k-th drawn
    slot", and an add, a remove or a reorder renumbers it — where a cell index
    names the record itself across every such change. ``None`` for a piece
    that came from no cell at all: a sprite *table* reads its pieces out of a
    region input, and such a piece is not editable
    (``docs/design/sprite-map.md`` §8.1). Excluded from comparison so that two
    pieces drawing the same thing still compare equal, which is what the
    inventory's keying and every existing equality test rely on.
    """

    x: int = 0
    y: int = 0
    index: int = 0
    palette_row: int = 0
    priority: int = 0
    flip_h: bool = False
    flip_v: bool = False
    across: int = 1
    down: int = 1
    column_major: bool = False
    group: int = 0
    record: int | None = field(default=None, compare=False)

    def size(self) -> tuple[int, int]:
        """This subsprite's size in tiles, never smaller than one either way."""
        return max(1, self.across), max(1, self.down)

    def pixels(self, tile_w: int, tile_h: int) -> tuple[int, int]:
        """Its size in pixels, for placing it against its neighbours."""
        across, down = self.size()
        return across * tile_w, down * tile_h

    def tile_indices(self) -> list[int]:
        """The source tiles it draws, in the order they appear on screen.

        Both walks are :func:`~celpix.core.tilemap.tile_run`'s, told apart by
        :attr:`column_major`: a VRAM array read across, stepping
        :data:`~celpix.core.tilemap.VRAM_ROW_STRIDE` to the next row, or the run
        of consecutive tiles a Mega Drive sprite spends going down each column.
        A flip reverses the order as well as mirroring each tile, which is the
        walk's business either way.
        """
        across, down = self.size()
        return tile_run(
            self.index,
            across,
            down,
            1 if self.column_major else VRAM_ROW_STRIDE,
            column_stride=down if self.column_major else 1,
            flip_h=self.flip_h,
            flip_v=self.flip_v,
        )


# One drawing of the object: subsprites in file order, which is front to back —
# subsprite 0 is on top, so a renderer walks the run backwards.
Frame = tuple[Subsprite, ...]

# How a size is *stated* to a codec's inverse, in the record's own terms and
# never as a shape: ``"small"`` / ``"large"`` where the record holds a size
# **bit** — a bit resolves against a pair the file does not hold, so a 2x2 under
# pair (2, 2) is both answers and the bit is the only thing that can be written
# back honestly — and an ``(across, down)`` rectangle where the record states
# one, by a nibble, by a code indexing the preset's ``sizes``, or outright.
SizeChoice = str | tuple[int, int]

# How a format lays its records out, which is what decides what a structural
# edit — add a piece, remove one, reorder them — can *mean* there
# (``docs/design/sprite-map.md`` §8.4):
#
#   slots    a fixed stride of slots per frame with a drawn bit; an add sets
#            the bit on a free slot and a remove clears it; the length never
#            changes (S-CG-CAD OBJ/OBX and OBZ)
#   counted  each frame counts its records in the file's structure; an add
#            inserts a cell and the count grows; the length changes (SPR)
#   headed   each frame opens with a header carrying its count, so a frame can
#            never be left empty; the length changes (sprite-record with a
#            frame_header)
#   run      one frame, a bare run whose count lives outside the slice; an
#            insert changes the length and nothing updates the count
#            (md-sprite, header-less sprite-record)
#   table    the records live in a region another entry owns; no structural
#            edit at all (sprite-table)
#   fixed    each frame is a set number of tile references at slots whose
#            positions the drawing routine fixes, not the record; a piece's
#            tile and flips are editable, its offset, size and presence are not,
#            so there is no structural *or* geometric edit (a pose table of six
#            tile words drawn as a 2x3 grid)
StructureKind = Literal["slots", "counted", "headed", "run", "table", "fixed"]


@dataclass(frozen=True, slots=True)
class SubspriteLimits:
    """What a codec's inverse can write, in the record's own terms.

    A codec that can place a piece declares this
    (:meth:`~celpix.plugins.base.TilemapCodecPlugin.subsprite_limits`), and the
    host reads it rather than the record layout: the ranges bound the spins and
    clamp a drag, the size choices are what the Size control offers, and the
    structure kind is what the record ops dispatch on. The host never learns
    where byte 0 bit 7 lives.

    ``x_range`` / ``y_range`` are the offsets the field holds, **signed at the
    field's own width** — ``range(-128, 128)`` for a byte, ``range(-32768,
    32768)`` for a word. A drag that would carry any piece outside them is
    refused whole rather than pinning one piece at the edge. Under ``fixed``
    both are ``range(0)``: the record holds no offset at all, and the empty
    range says so rather than inventing one a spin could offer.

    ``size`` is every :data:`SizeChoice` the record can spell, or ``None`` where
    the preset fixes the size and the record holds none.

    ``structure`` is the :data:`StructureKind`. ``per_frame`` is the slot stride
    under ``slots`` and the count ceiling under ``counted`` (255 for a count
    byte); ``None`` elsewhere. ``frames`` is how many frames the file holds
    where the format fixes it (32 or 64 slots of them), ``None`` where the
    cells decide — a headed run has as many frames as it has headers, a bare run
    exactly one.

    A ``fixed`` format answers ``SubspriteLimits(structure="fixed",
    x_range=range(0), y_range=range(0))`` and leaves ``size``, ``per_frame``
    and ``frames`` ``None``: the drawing routine owns every piece's place and
    size and the frame's record count, so the record states none of them.
    """

    structure: StructureKind
    x_range: range
    y_range: range
    size: tuple[SizeChoice, ...] | None = None
    per_frame: int | None = None
    frames: int | None = None


def signed_range(bits: int) -> range:
    """The values a two's-complement field ``bits`` wide holds — the form
    :attr:`SubspriteLimits.x_range` states an offset field in."""
    half = 1 << (bits - 1)
    return range(-half, half)


def range_text(r: range) -> str:
    """``r`` as a refusal names it, both ends inclusive: ``-128..127``."""
    return f"{r.start}..{r.stop - 1}"


def drawn_frames(frames: list[Frame]) -> list[Frame]:
    """``frames`` up to the last one holding anything, and never fewer than one.

    A file has room for a fixed 32 or 64 frames and the artist rarely filled
    it — 61% of the corpus's frames are empty — so showing every slot would put
    a mostly blank sheet on screen and bury the sprite at the top of it. The
    trailing run is dropped rather than the empty frames squeezed out, because a
    frame's *number* is what an animation step names.
    """
    last = 0
    for at, frame in enumerate(frames):
        if frame:
            last = at
    return frames[: last + 1] or [()]


# Where the origin sits in the frame box along each axis (:class:`FrameLayout`).
# ``fit`` is no anchor at all: the box is the pieces' own bounds, and the origin
# falls wherever that leaves it — outside the box, for an object drawn entirely
# to one side of it.
FRAME_ALIGN_X: tuple[str, ...] = ("fit", "left", "center", "right")
FRAME_ALIGN_Y: tuple[str, ...] = ("fit", "top", "center", "bottom")
# The largest frame a spin can ask for, in pixels. Well past any console's
# sprite (a 4x4 Mega Drive piece is 32 px; an S-CG-CAD object's offsets are a
# signed byte), and small enough that a sheet of them cannot approach the
# allocation limit the read already enforces.
FRAME_SIZE_MAX = 1024


@dataclass(frozen=True)
class FrameLayout:
    """How the box every frame is drawn in is placed around the object's origin.

    The pieces alone say what the box must *contain*; this says what it is
    anchored to and how big it is at least — the Frame bar's answer, kept on the
    view (``docs/design/sprite-map.md`` §2, "The frame box"). ``width`` and
    ``height`` are a minimum in pixels, measured from the anchored side (from
    the origin under ``left``/``top``, to it under ``right``/``bottom``, either
    side of it under ``center``, from the pieces' own edge under ``fit``); 0
    asks for nothing beyond the pieces. The box always grows to hold every
    piece, so no setting can clip the art: the alignment is kept where it can be
    and the spill is added on the side it falls.
    """

    width: int = 0
    height: int = 0
    align_x: str = "fit"
    align_y: str = "fit"


def frame_bounds(
    frames: list[Frame],
    tile_w: int = 8,
    tile_h: int | None = None,
    layout: FrameLayout | None = None,
) -> tuple[int, int, int, int]:
    """``(x, y, width, height)`` of the box every frame is drawn in, in pixels:
    every subsprite of every frame, placed and sized as ``layout`` asks.

    A subsprite states its size in tiles and its offset in pixels, so the tile
    size is what reconciles them — and it is the same measure the box is rounded
    out to, since "whole tiles" is the tile size whatever it is.

    The two axes are taken separately, and ``tile_h`` defaults to ``tile_w`` for
    the square tile most console sprite formats use. A subsprite is a rectangle
    of *tiles*, not of pixels: on an 8x16 tile a 2x2 subsprite is 16 wide and 32
    tall, so one number for both axes would be the assumed-square twin of an
    assumed 8.

    One box for the whole object, not one per frame, so that frames laid out
    side by side stay registered against each other — an animation's motion is
    part of what the strip is showing, and a per-frame box would centre it away.

    Rounded out to whole tiles so the grid overlay keeps landing on tile edges,
    and never empty: an object with no subsprites at all still needs somewhere to
    draw nothing. With no ``layout`` (or the default one) the box is the pieces'
    own bounds; an anchored axis keeps the origin on a tile edge too, so a
    centred box is rounded out on both sides alike rather than drifting a
    half-tile off centre.
    """
    tile_h = tile_w if tile_h is None else tile_h
    layout = layout or FrameLayout()
    left = top = 0
    right, bottom = tile_w, tile_h
    seen = False
    for frame in frames:
        for sub in frame:
            wide, tall = sub.pixels(tile_w, tile_h)
            if not seen:
                left, top = sub.x, sub.y
                right, bottom = sub.x + wide, sub.y + tall
                seen = True
                continue
            left = min(left, sub.x)
            top = min(top, sub.y)
            right = max(right, sub.x + wide)
            bottom = max(bottom, sub.y + tall)
    x, width = _frame_axis(left, right, layout.width, layout.align_x, tile_w)
    y, height = _frame_axis(top, bottom, layout.height, layout.align_y, tile_h)
    return x, y, width, height


def _frame_axis(lo: int, hi: int, size: int, align: str, step: int) -> tuple[int, int]:
    """One axis of the frame box, ``(start, length)``, from the pieces' span
    ``lo..hi``, the minimum ``size`` and the ``align`` the origin keeps.

    ``center`` is the one anchor that cannot take the span as it comes: the two
    halves have to match, so the longer side — or half the asked size — sets
    both, rounded up to a whole tile *before* it is mirrored. The side anchors
    pin the origin at an edge and let the span push the far edge out; ``fit``
    starts at the span and only lengthens it.
    """
    size = max(0, size)
    if align == "center":
        half = _ceil_to(max(step, -lo, hi, -(-size // 2)), step)
        return -half, 2 * half
    if align in ("left", "top"):
        start, end = min(0, lo), max(hi, size)
    elif align in ("right", "bottom"):
        start, end = min(lo, -size), max(0, hi)
    else:
        start, end = lo, max(hi, lo + size)
    start, end = _floor_to(start, step), _ceil_to(end, step)
    return start, max(step, end - start)


def _floor_to(value: int, step: int) -> int:
    return (value // step) * step


def _ceil_to(value: int, step: int) -> int:
    return -((-value) // step) * step
