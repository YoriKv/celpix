"""The interpreted sprite-object model: frames of freely placed parts.

The third thing a file of tile *references* can be, beside a pixel document and
a :class:`~celpix.core.tilemap.CellGrid`. A tilemap says "this cell of the grid
draws that tile"; a sprite object says "this **part** draws that tile at this
pixel offset from the object's origin", and the offsets are not multiples of the
tile size — measured over the 1,341 sprite files of the S-CG-CAD corpus, only 45%
of X offsets and 34% of Y offsets are 8-aligned
(``docs/graphics-formats-reference/scgcad-formats.md`` §8).

That single fact is why this model exists rather than the cell grid being reused:
a grid cannot express a tile at pixel offset 3, and quantising to the nearest
cell would move most of a sprite's parts. Everything else about the entry — the
binding to the tile bank it draws from, the palette, the save path — is the
tilemap entry's, unchanged (``docs/design/tilemap-entry.md`` §6).

A **frame** is one drawing of the object: a run of parts, drawn back to front. A
file holds a fixed number of frames whether or not the artist used them, so what
is worth showing is the run up to the last one with anything in it.

Qt-free, like the rest of ``core``.
"""

from __future__ import annotations

from dataclasses import dataclass

# The console's sprite-size pairs: an object picks one pair globally and each
# part picks *which of the two* with a single bit. Which pair is not recorded in
# the file — it was a register the game set — so it is a parameter of reading
# one, and (8, 16) is both the commonest choice and what the one independent
# viewer of these files opens on.
SIZE_PAIRS: tuple[tuple[int, int], ...] = (
    (8, 16),
    (8, 32),
    (8, 64),
    (16, 32),
    (16, 64),
    (32, 64),
)

# The tile array a part's extra tiles are stepped through: VRAM is 16 tiles wide,
# so a 16x16 part is N, N+1, N+0x10, N+0x11 — the same arithmetic a 16x16 BG cell
# uses (``docs/graphics-formats-reference/snes-hardware-notes.md`` §5).
TILE_ROW_STRIDE = 0x10


@dataclass(frozen=True, slots=True)
class Part:
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

    def size(self, pair: tuple[int, int]) -> int:
        """This part's side in pixels, under an object size ``pair``."""
        return pair[1] if self.large else pair[0]

    def tile_indices(self, pair: tuple[int, int]) -> list[int]:
        """The source tiles this part draws, in the order they appear on screen.

        Flips reverse the *order* as well as each tile: a mirrored 16x16 part
        shows its right-hand tile on the left, mirrored. Both halves are needed
        and neither is sufficient — the same compound rule a block flip follows
        on the tilemap side (:meth:`~celpix.core.tilemap.CellGrid.flipped_h`).
        """
        across = max(1, self.size(pair) // 8)
        return [
            self.index
            + (across - 1 - col if self.flip_h else col)
            + (across - 1 - row if self.flip_v else row) * TILE_ROW_STRIDE
            for row in range(across)
            for col in range(across)
        ]


# One drawing of the object: parts in file order, which is front to back — part 0
# is on top, so a renderer walks the run backwards.
Frame = tuple[Part, ...]


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
    frames: list[Frame], pair: tuple[int, int]
) -> tuple[int, int, int, int]:
    """``(x, y, width, height)`` enclosing every part of every frame, in pixels.

    One box for the whole object, not one per frame, so that frames laid out
    side by side stay registered against each other — an animation's motion is
    part of what the strip is showing, and a per-frame box would centre it away.

    Rounded out to whole tiles so the grid overlay keeps landing on tile edges,
    and never empty: an object with no parts at all still needs somewhere to
    draw nothing.
    """
    left = top = 0
    right = bottom = 8
    seen = False
    for frame in frames:
        for part in frame:
            size = part.size(pair)
            if not seen:
                left, top, right, bottom = part.x, part.y, part.x + size, part.y + size
                seen = True
                continue
            left = min(left, part.x)
            top = min(top, part.y)
            right = max(right, part.x + size)
            bottom = max(bottom, part.y + size)
    left, top = _floor8(left), _floor8(top)
    return left, top, max(8, _ceil8(right) - left), max(8, _ceil8(bottom) - top)


def _floor8(value: int) -> int:
    return (value // 8) * 8


def _ceil8(value: int) -> int:
    return -((-value) // 8) * 8
