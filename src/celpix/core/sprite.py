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

# An object's two subsprite sizes, **as multiples of the tile size**: each
# subsprite stores one bit choosing which of the two it is, and the pair itself is
# not in the file
# — it was a register the game set, so it is a parameter of reading one.
#
# Multiples rather than pixels, because that is what a subsprite is built from: a
# subsprite is a square of *tiles*, so `(1, 2)` says "one tile, or two by two" and
# stays true whatever the bound codec's tile size turns out to be. Pixels would
# have to be divided by it again at every use, and the division is where an
# assumed 8 used to hide.
DEFAULT_SUBSPRITE_TILES: tuple[int, int] = (1, 2)


@dataclass(frozen=True, slots=True)
class Subsprite:
    """One tile-square of a sprite frame, at a signed offset from the origin.

    ``large`` selects between the two sizes of the object's size pair rather
    than naming a size, because that is what the file stores: one bit, resolved
    against a setting that lives outside it.

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
    large: bool = False
    group: int = 0

    def tiles(self, pair: tuple[int, int]) -> int:
        """This subsprite's side **in tiles**, under an object's size ``pair``.

        ``pair`` is (small, large) as multiples of the tile size, so this is the
        number the tile walk wants directly — no pixel size to divide back down.
        """
        return max(1, pair[1] if self.large else pair[0])

    def pixels(self, pair: tuple[int, int], tile_px: int) -> int:
        """This subsprite's side in pixels, for placing it against its neighbours."""
        return self.tiles(pair) * tile_px

    def tile_indices(self, pair: tuple[int, int]) -> list[int]:
        """The source tiles this subsprite draws, in the order they appear on screen.

        A subsprite is square, so its two axes are the same count; everything else —
        the VRAM stride, and the way a flip reverses the order as well as each
        tile — is the walk a tilemap cell makes
        (:func:`~celpix.core.tilemap.tile_run`).
        """
        side = self.tiles(pair)
        return tile_run(
            self.index,
            side,
            side,
            VRAM_ROW_STRIDE,
            flip_h=self.flip_h,
            flip_v=self.flip_v,
        )


# One drawing of the object: subsprites in file order, which is front to back —
# subsprite 0 is on top, so a renderer walks the run backwards.
Frame = tuple[Subsprite, ...]


def drawn_frames(frames: list[Frame]) -> list[Frame]:
    """``frames`` up to the last one holding anything, and never fewer than one.

    A file has room for a fixed 32 or 128 frames and the artist rarely filled
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
    pair: tuple[int, int],
    tile_w: int = 8,
    tile_h: int | None = None,
) -> tuple[int, int, int, int]:
    """``(x, y, width, height)`` enclosing every subsprite of every frame, in pixels.

    ``pair`` is in tiles and a subsprite's offsets are in pixels, so the tile size is
    what reconciles them — and it is the same measure the box is rounded out to,
    since "whole tiles" is the tile size whatever it is.

    The two axes are taken separately, and ``tile_h`` defaults to ``tile_w`` for
    the square tile every console sprite format actually uses. A subsprite is a
    square of *tiles*, not a square of pixels: on a 8x16 tile a 2x2 subsprite is 16
    wide and 32 tall, so one number for both axes would be the assumed-square twin
    of an assumed 8 (:data:`DEFAULT_SUBSPRITE_TILES`).

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
            wide, tall = sub.pixels(pair, tile_w), sub.pixels(pair, tile_h)
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
