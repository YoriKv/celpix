"""The interpreted tilemap model: a grid of cells, each naming a tile.

A tilemap is a file of tile *indices* drawn as a layout — a screen, a level's
background, a stamp sheet. Where an :class:`~celpix.core.index_grid.IndexGrid`
holds one palette index per pixel, a :class:`CellGrid` holds one :class:`Cell`
per grid position, and the tile that cell names lives in a *different* document
(``docs/design/tilemap-entry.md`` §3). Nothing here resolves that reference:
this is the codec-neutral model the tilemap pathway decodes into and encodes
back out of, exactly as the index grid is for pixels. Qt-free.

A cell is not a bare index. Every hardware tilemap format worth reading carries
per-cell **attributes** alongside the index — which palette row to draw through,
whether to mirror the tile, and a priority bit that orders it against other
layers. celPix renders none of the priority and only some of the mirroring, and
carries all of it anyway: a field dropped at decode time is a field silently
zeroed on write, which corrupts every file that used it.

Cells are frozen. A tilemap edit replaces a cell rather than mutating one, which
is what lets an undo command hold a plain before/after pair of cells without
copying, and what makes two equal grids compare equal.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from enum import Enum
from functools import lru_cache

from celpix.core import ceil_div
from celpix.core.arrangement import BlockLayout
from celpix.core.tilerearrangement import TILE_FLIP_H, TILE_FLIP_V

# The index step between one row of a multi-tile cell and the next. **Not** the
# cell's own width: console VRAM behaves as a 16-tile-wide array, so a 16x16 BG
# cell draws tiles N, N+1, N+0x10, N+0x11
# (``docs/graphics-formats-reference/snes-hardware-notes.md`` §5). A sprite
# object's multi-tile subsprite steps through the same array the same way
# (:data:`~celpix.core.sprite.Subsprite.tile_indices`), which is why this is one
# constant rather than a coincidence in two places.
VRAM_ROW_STRIDE = 0x10


def tile_run(
    first: int,
    across: int,
    down: int,
    stride: int,
    *,
    column_stride: int = 1,
    flip_h: bool = False,
    flip_v: bool = False,
) -> list[int]:
    """The tiles a multi-tile thing draws, in the order they appear on screen.

    One index for an ordinary hardware cell; four for a 16x16 metatile or a 16x16
    subsprite, stepped by ``stride`` rather than by ``across``
    (:data:`VRAM_ROW_STRIDE`).

    ``column_stride`` is the step to the *next column*, and the two together are
    what let one walk serve both orders a console uses. Left at 1 it is the
    row-major walk of a VRAM array read across (:data:`VRAM_ROW_STRIDE` being the
    step down); set to the thing's own height with ``stride`` at 1 it is the
    **column-major** walk a Mega Drive sprite takes, whose tiles run down each
    column before starting the next. Neither is more natural than the other —
    they are two hardwares' answers, and a walk that assumed one would scramble
    every multi-tile piece of the other.

    **A flip reverses the order as well as mirroring each tile.** A mirrored
    metatile shows its right-hand tile on the left, mirrored; toggling the bits
    alone would mirror each tile in place and leave the layout unmirrored, and
    reversing alone would move them without turning them. Both halves are needed
    and neither is sufficient — the same rule :meth:`CellGrid.flipped_h` follows
    over a rectangle of cells. Only the ordering is here; mirroring the tile's pixels
    is the renderer's half.

    Shared by the two things that index a tile array this way, a tilemap cell
    (:meth:`~celpix.core.document.Document.cell_tile_indices`) and a subsprite
    (:meth:`~celpix.core.sprite.Subsprite.tile_indices`), so the rule cannot come out
    different depending on which document is on screen.
    """
    if across == 1 and down == 1:
        # The ordinary hardware cell, and the one a whole map is made of: no
        # stride to step and no order for a flip to reverse. Answered without
        # the walk because a repaint asks this once per cell, thousands of times.
        return [first]
    return [
        first
        + (down - 1 - row if flip_v else row) * stride
        + (across - 1 - col if flip_h else col) * column_stride
        for row in range(down)
        for col in range(across)
    ]


class CellOp(str, Enum):
    """A transform a tool asks of one cell, for the *format* to answer.

    Which of these a tilemap can do is a property of the format, not of tilemaps:
    a console BG entry has both mirror bits, a Game Boy map entry has neither and
    a stamp layout's word is a coordinate with no room for any. So the tool names
    the operation and the codec decides — it is the only thing that knows which
    bits, if any, say it (``docs/design/tilemap-entry.md`` §4).

    The rotations are here because the question is the same one and a format that
    has a rotation bit should be able to answer yes. Nothing shipped does, so
    every built-in codec refuses them; what supporting one would take is written
    up in :meth:`~celpix.plugins.base.TilemapCodecPlugin.transform_cell`.

    ``value`` is the toolbar's own field name for the button, so the two cannot
    drift apart (:data:`~celpix.ui.main_window.transform.OP_BY_FIELD`).
    """

    FLIP_H = "flip_h"
    FLIP_V = "flip_v"
    ROTATE_CW = "rotate_cw"
    ROTATE_CCW = "rotate_ccw"

    @property
    def label(self) -> str:
        """How the operation reads in a sentence: "no horizontal flip"."""
        return {
            CellOp.FLIP_H: "horizontal flip",
            CellOp.FLIP_V: "vertical flip",
            CellOp.ROTATE_CW: "rotation",
            CellOp.ROTATE_CCW: "rotation",
        }[self]


@dataclass(frozen=True, slots=True)
class Cell:
    """One tilemap position: which tile, and how to show it.

    ``index`` is the format's own tile number, *before* any base offset the tile
    source applies (``docs/design/tilemap-entry.md`` §3) — the file's number, so
    what is written back is what was read.

    ``priority`` is carried but never rendered. It orders a cell against other
    background layers on hardware, which celPix does not simulate; storing it is
    what keeps a round-trip byte-exact.

    ``visible`` is whether the position is drawn at all. A stamp layout's entry
    carries such a bit (``docs/graphics-formats-reference/scgcad-formats.md`` §4)
    and the authoring tool skips a position that lacks it, so a map read without
    it draws panel content over roughly a quarter of the positions its author
    left blank. Cleared, the position paints the background rather than its cell
    (``docs/design/tilemap-entry.md`` §6). Defaults True, which is what every
    format with no such bit means.

    ``ends_line`` is whether this position is the **last of its line**, for the
    text formats that say so with a bit on the character rather than with a code
    of their own (``docs/graphics-formats-reference/text-formats.md`` §4.4). It
    is the one field that says nothing about the picture and everything about the
    reading: the cell still draws the glyph its ``index`` names, and what the bit
    adds is the newline the text window shows after it
    (``docs/design/fontmap-entry.md`` §5). False for every format with no such
    bit, which is most of them.

    ``flags`` is the same idea as ``priority`` generalised: bits a format has that
    celPix has no meaning for at all. Naming such a bit ``priority`` to get it
    round-tripped would be a lie; dropping it would corrupt the file on the next
    write. So it rides here, uninterpreted and intact.

    **Four of these fields reach a *tile*, and the render memoizes on exactly
    those four.** ``index``, ``palette_row``, ``flip_h`` and ``flip_v`` decide
    entirely what a cell draws; ``priority`` and ``flags`` decide nothing and are
    carried for the round trip alone. So a **new field that changes the tile** —
    the rotation bit ``docs/design/tilemap-entry.md`` §4 contemplates is the live
    example — has to be added to that key as well as here
    (:func:`~celpix.pipeline.pipeline.expand_cells`). Miss it and two cells
    differing only in the new field draw the same, silently, and only the second
    one is wrong.

    ``visible`` is deliberately **not** in that key, and is the one field that
    changes the picture without changing a tile: a hidden position is painted
    over the composed image by position
    (:attr:`~celpix.pipeline.pipeline.TilemapImage.hidden`), so the tile it would
    have drawn is still the tile its twin draws and the memo stays sound.
    ``ends_line`` is not in it either, and for the stronger reason: it reaches no
    pixel at all.
    """

    index: int = 0
    palette_row: int = 0
    priority: int = 0
    flip_h: bool = False
    flip_v: bool = False
    visible: bool = True
    ends_line: bool = False
    flags: int = 0

    def flipped_h(self) -> Cell:
        """This cell mirrored horizontally — the attribute toggled, not the tile.

        A tilemap flip costs one bit and no pixels, which is the whole reason
        hardware has the bit. The pixel-side equivalent rewrites the tile
        (``celpix.core.transform``) or stores a display orientation
        (``celpix.core.tilerearrangement``); all three are the same gesture over
        different documents (``docs/design/tilemap-entry.md`` §4).
        """
        return replace(self, flip_h=not self.flip_h)

    def flipped_v(self) -> Cell:
        """This cell mirrored vertically."""
        return replace(self, flip_v=not self.flip_v)


BLANK = Cell()


def cell_orientation(cell: Cell) -> int:
    """``cell``'s flips as the orientation flags the transform helpers take.

    A cell mirrors its tile exactly as a rearranged view does, so the two say it
    the same way and the *same* pair of functions turns a tile for display
    (:func:`~celpix.core.tilerearrangement.apply_orientation`) and back for a
    write (:func:`~celpix.core.tilerearrangement.unapply_orientation`). A pixel
    edit made through a tilemap needs the second, and spelling the inverse out by
    hand instead is how the order of a future third operation comes to differ
    between the two paths — a rotation is already contemplated on :class:`Cell`
    and would land here rather than in every caller.
    """
    return (TILE_FLIP_H if cell.flip_h else 0) | (TILE_FLIP_V if cell.flip_v else 0)


def resolve_cell(
    cell: Cell, source: list[Cell], *, carry_rows: bool, at: int | None = None
) -> Cell:
    """The cell ``cell`` names in the tilemap it draws through, or a blank.

    Where an ordinary cell's ``index`` is a tile number, a chained one is a
    position in another tilemap's cells (``docs/design/tilemap-entry.md`` §3.1).
    What comes back is that cell whole — its tile, its palette row, its flips —
    with the referring cell's own attributes composed on top.

    ``at`` overrides which source position is read while leaving the referring
    cell's own attributes in force. That is what a **stamp** needs: one entry
    names a whole stamp of cells, and the positions inside it read neighbouring
    source cells off the one coordinate (:func:`expand_stamps`). A parameter
    rather than a rebuilt ``Cell`` because a restamp re-resolves every position
    in the map and the copies would be the bulk of the work.

    Composed rather than dropped because the referring format may carry
    attributes of its own, and discarding them would draw a picture neither file
    describes. Both rules reduce to *the source cell untouched* for a format with
    no such field, which is exactly what a stamp layout's coordinate word is:

    - **Flips toggle** rather than overwrite, the same rule :meth:`CellGrid.
      flipped_h` follows — a mirrored reference to an already-mirrored stamp
      faces its original way. A format with no flip bits decodes ``False`` and
      changes nothing.
    - **The palette row is the referrer's only where its format has one to
      state** (``carry_rows``, from
      :meth:`~celpix.plugins.base.TilemapCodecPlugin.has_palette_rows`). A
      decoded 0 from a format without the field means "no row here", not "row
      0", and letting it through would silently black out the source's rows.

    Priority and ``flags`` come from the source cell, and neither is rendered.

    - **Visibility is the referrer's**, and is the one attribute that does not
      compose. The bit lives on the *layout* entry — it is the layout's author
      who decided this position stays blank — while the panel cell it names knows
      nothing about where it is stamped and is drawn elsewhere on the same map
      (``docs/graphics-formats-reference/scgcad-formats.md`` §4). Taking the
      source's would blank every position stamping a hidden cell; ``and``-ing the
      two would do the same. A source with no such bit reads True and leaves the
      referrer's answer standing, which is every format but one.
    """
    index = cell.index if at is None else at
    if not 0 <= index < len(source):
        # A reference the source does not have draws blank rather than failing: a
        # layout outliving the panel it was authored against is ordinary, and so
        # is a restamp typed past the end of the panel.
        return BLANK if cell.visible else replace(BLANK, visible=False)
    found = source[index]
    if (
        cell.visible
        and found.visible
        and not carry_rows
        and not (cell.flip_h or cell.flip_v)
    ):
        # A coordinate-only format has nothing to compose, so the source cell
        # comes back as itself — the same object, which is what keeps a 4096-cell
        # layout from rebuilding every cell it resolves. Either cell being hidden
        # takes the slow path: a hidden *referrer* has something to say, and a
        # hidden *source* has something that must not be said, since returning it
        # whole is the one way this function can leak the source's visibility into
        # the answer — the exact composition the rule above forbids.
        return found
    return replace(
        found,
        palette_row=cell.palette_row if carry_rows else found.palette_row,
        flip_h=found.flip_h != cell.flip_h,
        flip_v=found.flip_v != cell.flip_v,
        visible=cell.visible,
    )


def stamp_origin(
    position: int, columns: int, stamp: tuple[int, int], *, dense: bool = False
) -> int:
    """Which entry the drawn position ``position`` takes its stamp from.

    A stamped map's entries are not one per drawn position, and there are **two
    ways a file can hold fewer**. Which one it is, is the referring format's
    answer (:attr:`~celpix.core.document.CellChain.dense`), and it is the only
    thing that differs between the two branches here:

    - **Sparse** — the file still has a slot per drawn position and only the
      stamp corners are meaningful, the rest holding whatever it last had there.
      So a position reads the entry at its stamp's top-left corner, and the
      three-quarters of a 2x2 map that were never written are never read either
      (``docs/graphics-formats-reference/scgcad-formats.md`` §4).
    - **Dense** — the file has exactly one entry per stamp and no filler at all,
      which is what a map of 16x16 metatiles over an 8x8 tile bank looks like on
      most hardware. The entry grid is then a *different shape* from the drawn
      one: dividing the position by the stamp is the whole of the answer, with
      nothing to snap.

    ``columns`` is the width the **file** states, counted in entries either way,
    so the drawn grid is that many stamps across when dense and that many
    positions across when sparse. Taking the file's number in both is what lets
    the two callers below pass the same thing.

    The same snap answers both directions, which is why it is one function: it
    picks the entry a position *draws*, and the entry a click on that position
    *restamps* (:meth:`~celpix.core.document.Document.cell_at`). An edit anywhere
    inside a stamp changes the one entry that stamp came from.
    """
    across, down = max(1, stamp[0]), max(1, stamp[1])
    width = columns * across if dense else columns
    x, y = position % width, position // width
    if dense:
        return y // down * columns + x // across
    return (y - y % down) * columns + (x - x % across)


def expand_stamps(
    cells: list[Cell],
    source: list[Cell],
    columns: int,
    stamp: tuple[int, int],
    source_columns: int,
    *,
    carry_rows: bool,
    dense: bool = False,
) -> list[Cell]:
    """Resolve a stamped map into one source cell per **drawn position**.

    The referring map is a grid of stamps and the source is a grid of cells, and
    this is the one place the two shapes meet: the entry a position's stamp comes
    from (:func:`stamp_origin`) names the source cell that stamp's corner draws,
    and the rest of the stamp walks the source's *own* rows from there — offset
    ``x % across + (y % down) * source_columns``. That last term is why
    ``source_columns`` is a parameter and not the referrer's width: a stamp is a
    rectangle cut out of the source, so stepping down a row inside it is a step
    of the source's width.

    How many positions come back is the one thing ``dense`` changes, and it
    follows from what the file holds. A **sparse** map already has a slot per
    drawn position, so the list is the same length as ``cells``. A **dense** one
    holds a single entry per stamp, so it expands to ``across * down`` positions
    per entry — but laid out in **whole rows of the picture**, which is the number
    that has to be produced rather than the per-entry one. The list is row-major
    at ``columns * across`` wide, so an entry row the entries do not fill is
    padded out to the width instead of stopping short: cut short, the rows after
    it would start in the wrong place and the last row's lower half would simply
    not be emitted — the whole bottom of the picture missing, at every width that
    does not divide the entry count. The padding is :data:`BLANK`, which is
    already this walk's answer wherever a position's stamp points past the last
    entry. Either way the list is in
    **drawn** order rather than file order, and everything that indexes the file
    (a save, the hex dump, a restamp) goes through
    :meth:`~celpix.core.document.Document.cell_at` instead, which is what keeps
    the two orders from being confused for each other.

    **Not** :func:`tile_run`, and the difference is the whole reason both exist.
    Each makes a 2x2 unit out of four tiles and they are otherwise nothing alike:

    - A **hardware metatile** is one cell whose single index names four
      *tiles*, stepped by the VRAM row (:data:`VRAM_ROW_STRIDE`) and sharing
      that one cell's palette row and flips. One set of attributes, four tiles.
    - A **stamp** is one coordinate naming four *cells* of another map, stepped by
      that map's row, each carrying its own tile, row, priority and flips. Four
      sets of attributes, four tiles.

    Applying the first rule to the second's data reads four tiles from the wrong
    place and flattens four attribute sets into one. The two can also compose — a
    stamped map whose source has metatile cells — which is why the strides are
    separate parameters rather than one shared notion of "how big a unit is".
    """
    across, down = max(1, stamp[0]), max(1, stamp[1])
    stride = max(1, source_columns)
    entries = max(1, columns)
    width = entries * across if dense else columns
    # Whole entry rows rounded up, not a flat `entries x across x down` budget:
    # the two agree wherever the width divides the entry count and part company
    # everywhere else, and the flat one runs out mid-picture (see above).
    rows = ceil_div(len(cells), entries)
    positions = rows * down * width if dense else len(cells)
    out: list[Cell] = []
    for position in range(positions):
        at = stamp_origin(position, columns, (across, down), dense=dense)
        if not 0 <= at < len(cells):
            out.append(BLANK)
            continue
        entry = cells[at]
        offset = position % width % across + position // width % down * stride
        out.append(
            resolve_cell(entry, source, carry_rows=carry_rows, at=entry.index + offset)
        )
    return out


class CellGrid:
    """A row-major grid of :class:`Cell`, the tilemap pathway's decoded form.

    Sized in **cells**, not pixels or tiles: how many tiles a cell covers is the
    codec's parameter (a panel cell is 2x2 tiles) and how big a tile is belongs
    to the pixel document this grid indexes into. Neither is knowable from the
    grid alone, and neither is needed to hold one.

    Mutable, like the pixel model and unlike :class:`Cell`: an edit sets a
    position, and the positions are the document.
    """

    __slots__ = ("_cells", "_height", "_width")

    def __init__(self, width: int, height: int, fill: Cell = BLANK) -> None:
        if width < 0 or height < 0:
            raise ValueError(f"negative grid size: {width}x{height}")
        self._width = width
        self._height = height
        self._cells: list[Cell] = [fill] * (width * height)

    # -- shape -------------------------------------------------------------
    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    def __len__(self) -> int:
        return len(self._cells)

    def __iter__(self) -> Iterator[Cell]:
        """Cells in row-major order — the order a codec encodes them back in."""
        return iter(self._cells)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CellGrid):
            return NotImplemented
        return (
            self._width == other._width
            and self._height == other._height
            and self._cells == other._cells
        )

    def __repr__(self) -> str:
        return f"CellGrid({self._width}x{self._height})"

    # -- access ------------------------------------------------------------
    def contains(self, x: int, y: int) -> bool:
        return 0 <= x < self._width and 0 <= y < self._height

    def get(self, x: int, y: int) -> Cell:
        if not self.contains(x, y):
            raise IndexError(f"({x}, {y}) outside {self._width}x{self._height}")
        return self._cells[y * self._width + x]

    def set(self, x: int, y: int, cell: Cell) -> None:
        if not self.contains(x, y):
            raise IndexError(f"({x}, {y}) outside {self._width}x{self._height}")
        self._cells[y * self._width + x] = cell

    def at(self, position: int) -> Cell:
        """The cell at a flat row-major position — the codec's own coordinate."""
        return self._cells[position]

    # -- construction ------------------------------------------------------
    @classmethod
    def from_cells(cls, width: int, height: int, cells: Iterable[Cell]) -> CellGrid:
        """A grid of ``width`` x ``height`` filled from ``cells``, row-major.

        Short input is padded with blanks and long input is truncated, because a
        decode is fed a byte window that need not divide evenly: a file holding
        a partial final row is a file to show, not one to refuse. The pixel
        pathway takes the same position on a trailing partial tile.
        """
        grid = cls(width, height)
        for i, cell in enumerate(cells):
            if i >= len(grid._cells):
                break
            grid._cells[i] = cell
        return grid

    def copy(self) -> CellGrid:
        grid = CellGrid(self._width, self._height)
        grid._cells = list(self._cells)
        return grid

    # -- regions -----------------------------------------------------------
    def region(self, x: int, y: int, width: int, height: int) -> CellGrid:
        """The ``width`` x ``height`` rectangle at ``(x, y)``, clipped to bounds.

        Clipped rather than refused so a selection dragged past the edge yields
        what is actually there — the same rule the pixel selection follows.
        """
        w = max(0, min(width, self._width - x))
        h = max(0, min(height, self._height - y))
        out = CellGrid(w, h)
        for row in range(h):
            start = (y + row) * self._width + x
            out._cells[row * w : (row + 1) * w] = self._cells[start : start + w]
        return out

    def paste(self, x: int, y: int, patch: CellGrid) -> None:
        """Lay ``patch`` over this grid at ``(x, y)``, clipped to bounds."""
        for row in range(patch.height):
            ty = y + row
            if not 0 <= ty < self._height:
                continue
            for col in range(patch.width):
                tx = x + col
                if 0 <= tx < self._width:
                    self._cells[ty * self._width + tx] = patch.at(
                        row * patch.width + col
                    )

    # -- transforms --------------------------------------------------------
    def flipped_h(self) -> CellGrid:
        """This grid mirrored horizontally: cell order reversed per row, **and**
        every cell's own H-flip toggled.

        Both halves are needed and neither is sufficient. Reversing the order
        alone mirrors the layout while leaving each tile facing its original way;
        toggling the bits alone mirrors each tile in place. A block flip on the
        pixel side is the same compound operation over tiles rather than cells
        (``docs/design/tilemap-entry.md`` §4).
        """
        out = CellGrid(self._width, self._height)
        for y in range(self._height):
            row = self._cells[y * self._width : (y + 1) * self._width]
            out._cells[y * self._width : (y + 1) * self._width] = [
                cell.flipped_h() for cell in reversed(row)
            ]
        return out

    def flipped_v(self) -> CellGrid:
        """This grid mirrored vertically — row order reversed, V-flip toggled."""
        out = CellGrid(self._width, self._height)
        for y in range(self._height):
            src = self._height - 1 - y
            out._cells[y * self._width : (y + 1) * self._width] = [
                cell.flipped_v()
                for cell in self._cells[src * self._width : (src + 1) * self._width]
            ]
        return out


# -- page assembly ---------------------------------------------------------
#
# Some formats hold several independent maps end to end in one file — a screen
# file is four 32x32 screens — and read back to back those four stack in a
# column, which is a reading no console ever used; laid two across they are the
# 64x64 screen the hardware assembles
# (``docs/graphics-formats-reference/scgcad-formats.md`` §2).
#
# **Which assembly it is comes from the file**, not from a control: a container
# publishes it where its header says (``KEY_TILEMAP_PAGES_ACROSS``) and an entry
# format may state it where no container can, so there is nothing here to pick
# (``docs/design/tilemap-entry.md`` §6). These four functions are what is left:
# which assemblies a page count admits, which to fall back on where nothing has
# stated one, how to reject a stored one the file cannot have, and the placement
# itself.
#
# Placement is display-only, exactly like the block arrangement it reuses: the
# cells stay in the file's own order and only *where each one is drawn* moves.


def page_assemblies(pages: int) -> tuple[int, ...]:
    """The pages-across values that lay ``pages`` pages out as a whole rectangle.

    The divisors of ``pages``, so every choice fills its last row of pages: a
    partial one would leave holes in the middle of the picture at some other
    width, and there is no reading of a screen file in which a quarter of it is
    absent. Four screens therefore assemble 1x4, 2x2 or 4x1 — the arrangements
    that show all of them.
    """
    if pages <= 0:
        return ()
    return tuple(across for across in range(1, pages + 1) if pages % across == 0)


def default_pages_across(page_columns: int, page_rows: int, pages: int) -> int:
    """The assembly to open a paged file on: the one closest to square in cells.

    The fallback, reached only where neither the container nor the entry format
    states a layout (:attr:`~celpix.core.document.Document.stated_pages_across`).
    Nothing then says which assembly was meant, so the guess to make is the least
    misleading one. Four 32x32 screens are the case it is calibrated against:
    square is 2x2, which is also the console's own multi-screen order and what
    the one independent viewer of that format draws (``scgcad-formats.md`` §2
    "Screen assembly") — so the general rule agrees with the layout a screen file
    states for itself rather than having to be reconciled with it.

    Ties go to the **wider** layout: two pages read side by side before they read
    stacked, and the console's own two-screen mode is the horizontal one.
    """
    options = page_assemblies(pages)
    if not options:
        return 1
    width, height = max(1, page_columns), max(1, page_rows)

    def squareness(across: int) -> tuple[float, int]:
        w, h = across * width, (pages // across) * height
        # Aspect as a ratio ≥ 1 either way, so tall and wide are compared by how
        # far from square they are rather than by which axis is longer.
        return max(w, h) / min(w, h), -across

    return min(options, key=squareness)


def resolve_pages_across(
    wanted: int, page_columns: int, page_rows: int, pages: int
) -> int:
    """``wanted`` if it is an assembly this file has, else the default one.

    One place decides, so a project that stored an assembly for a file whose
    format has since been read differently — a different cell size, so a
    different page count — opens on something sensible instead of a layout that
    would shear the picture. 0 means "nothing chosen", which is what a project
    written before assemblies existed says, and lands on the default too.
    """
    if wanted in page_assemblies(pages):
        return wanted
    return default_pages_across(page_columns, page_rows, pages)


@lru_cache(maxsize=16)
def page_order(
    page_columns: int, page_rows: int, pages: int, pages_across: int
) -> tuple[int, ...]:
    """Which cell each drawn position shows, for a ``pages_across`` assembly.

    A page is the unit handed to :class:`~celpix.core.arrangement.BlockLayout`
    here — a rectangle of *cells* rather than of tiles — and the assembly is a
    row of those, which is exactly what that class already maps — so the
    placement is that class's, asked at cell scale instead of tile scale, and the
    one thing this adds is inverting it: the composer wants "position *p* draws
    cell *n*" where the layout answers "cell *n* is drawn at (x, y)".

    Cached because a repaint asks for it and it depends on four numbers that
    change only when the file or the assembly does. The result is a tuple for the
    same reason: a shared cached list a caller could mutate is a bug waiting for
    the second caller.
    """
    width = max(1, pages_across) * max(1, page_columns)
    count = max(0, pages) * max(1, page_columns) * max(1, page_rows)
    if pages <= 0 or pages_across <= 0 or pages % pages_across:
        # Not a whole rectangle of pages: some drawn position would have no cell
        # and the inversion below would leave it pointing at cell 0. The file's
        # own order is the honest fallback (:func:`resolve_pages_across` keeps
        # callers off this path).
        return tuple(range(count))
    layout = BlockLayout(width, page_columns, page_rows)
    order = [0] * count
    for position in range(count):
        x, y = layout.slot_to_pos(position)
        order[y * width + x] = position
    return tuple(order)
