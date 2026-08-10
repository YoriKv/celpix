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
tilemap entry's, unchanged (``docs/design/tilemap-entry.md`` §6).

A **frame** is one drawing of the object: a run of subsprites, drawn back to
front. A
file holds a fixed number of frames whether or not the artist used them, so what
is worth showing is the run up to the last one with anything in it.

Qt-free, like the rest of ``core``.
"""

from __future__ import annotations

from dataclasses import dataclass

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


def frame_bounds(
    frames: list[Frame],
    tile_w: int = 8,
    tile_h: int | None = None,
) -> tuple[int, int, int, int]:
    """``(x, y, width, height)`` enclosing every subsprite of every frame, in pixels.

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
    draw nothing.
    """
    tile_h = tile_w if tile_h is None else tile_h
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
    left, top = _floor_to(left, tile_w), _floor_to(top, tile_h)
    return (
        left,
        top,
        max(tile_w, _ceil_to(right, tile_w) - left),
        max(tile_h, _ceil_to(bottom, tile_h) - top),
    )


def _floor_to(value: int, step: int) -> int:
    return (value // step) * step


def _ceil_to(value: int, step: int) -> int:
    return -((-value) // step) * step
