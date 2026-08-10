"""Which subsprite is picked on a sprite object, and what follows from it.

A tilemap answers "what did I just click" with a **cell**: the canvas divides a
position by the grid and the whole editor reads the answer — the Cell spin, the
hex highlight, the tile source panel's ring. A sprite object has no grid to
divide by. Its records sit at signed pixel offsets that are mostly not
tile-aligned, they overlap, and one 8x8 square of the sheet routinely holds
pieces of three of them (``docs/design/tilemap-entry.md`` §6, OBJ), so the slot
the canvas reports cannot name one — which would otherwise leave a sprite the one
kind of document where clicking the picture says nothing about what was clicked.

So the answer is resolved from the pixel instead: the canvas reports the **pixel**
a tile-mode press landed on
(``Canvas.pixel_picked``), :func:`~celpix.pipeline.pipeline.subsprite_at` runs
the render backwards to find which subsprite draws it, and the pick is held here
for the two things that want it — the outline on the canvas, and the ring in the
tile source panel over the tile that subsprite names.

**A press on the same tile again picks the next one down.** One answer per press
would leave most of an object unreachable: subsprites overlap by design, and the
front-most one hides whole records behind it, so the stack is cycled rather than
resolved once (:meth:`~SpriteSelectMixin._cycled_pick`).

**It sits beside the tile selection rather than replacing it.** A press on a
sprite still selects the sheet tile under it, because that selection is what Copy
lifts and what the palette-row readouts count; the pick says which *record* drew
what is in that square. Two overlapping answers to one press, and they are drawn
apart: the tile selection wears the app's selection white, the pick the grid's
structural blue — white for what the user pointed at, blue for what it resolved
to, which is the same pairing the tile source panel's two rings make.

The pick is **session state, not the file's**. Nothing here writes: a sprite
object's records are not editable through the canvas
(:attr:`~celpix.core.document.Document.cells_editable`), so this is a reading
aid, and it is dropped rather than migrated whenever the frames underneath it
could have moved.
"""

from __future__ import annotations

from PySide6.QtCore import QRect

from celpix.core.sprite import Subsprite
from celpix.pipeline import pipeline


class SpriteSelectMixin:
    """The picked subsprite of the sprite object on screen.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads the window's own single live ``_doc`` and its
    widgets. See the module docstring for what it owns, and the package docstring
    for why these are mixins.
    """

    def _init_sprite_select(self) -> None:
        # (frame, subsprite), both indices into what is *drawn* — which is the
        # file's own numbering, the shown frames being a prefix of it
        # (:func:`~celpix.core.sprite.drawn_frames`).
        self._picked_subsprite: tuple[int, int] | None = None
        # The sheet tile the last press landed in, and how many subsprites it
        # had under it — what makes the *next* press a cycle rather than a
        # fresh pick (:meth:`_cycled_pick`).
        self._pick_tile: tuple[int, int] | None = None
        self._pick_depth: int = 0

    def _connect_sprite_canvas(self) -> None:
        """Wire the canvas's pixel report (called once the canvas exists)."""
        self._canvas.pixel_picked.connect(self._on_pixel_picked)

    # -- picking -------------------------------------------------------------
    def _on_pixel_picked(self, x: int, y: int) -> None:
        """A tile-mode press: pick the subsprite under it, on a sprite object.

        Silent on everything else — the signal is emitted for every tile-mode
        press, and a document with a cell grid has already answered the same
        press through the selection.
        """
        doc = self._doc
        if doc is None or not doc.is_sprite:
            return
        order = pipeline.subsprites_at(
            doc, self._registry, self._tilemap_columns(), x, y
        )
        tile = (x // max(1, doc.tile_width), y // max(1, doc.tile_height))
        pick = self._cycled_pick(order, tile)
        self._pick_tile, self._pick_depth = tile, len(order)
        self._set_picked_subsprite(pick)

    def _cycled_pick(
        self, order: list[tuple[int, int]], tile: tuple[int, int]
    ) -> tuple[int, int] | None:
        """Which of ``order`` this press takes: the front one, or the next one.

        Overlap is the normal case on a sprite object and the front-most piece
        hides the rest, so one press per subsprite is not enough to reach them —
        pressing the same tile again steps down the stack instead of re-picking
        what is already picked, and wraps back to the top. The **tile** is the
        anchor rather than the pixel because the press it has to recognise is the
        user clicking the same spot again, which is never the same pixel twice;
        pressing anywhere else starts over at the front, so the first answer on a
        piece is always the one the eye picks out.
        """
        held = self._picked_subsprite
        if not order:
            return None
        if held is not None and tile == self._pick_tile and held in order:
            return order[(order.index(held) + 1) % len(order)]
        return order[0]

    def _set_picked_subsprite(self, pick: tuple[int, int] | None) -> None:
        """Hold ``pick``, and converge everything that reads it."""
        self._picked_subsprite = pick
        if pick is None:
            # Nothing picked is nothing to cycle: a press that found no piece,
            # and the document being closed, both land here.
            self._pick_tile, self._pick_depth = None, 0
        self._sync_subsprite_outline()
        # The tile source panel's ring marks what the canvas is pointing at, and
        # on a sprite object this is what that is. The palette grid's ring is the
        # same answer read the other way: which row the picked record draws in.
        self._sync_tile_source_marker()
        self._sync_marked_palette_row()
        self._announce_subsprite()

    def _picked_subsprite_record(self) -> Subsprite | None:
        """The picked subsprite itself, or ``None`` with nothing picked."""
        doc, pick = self._doc, self._picked_subsprite
        if doc is None or pick is None or not doc.is_sprite:
            return None
        frames = doc.shown_frames
        at, index = pick
        if not (0 <= at < len(frames) and 0 <= index < len(frames[at])):
            return None
        return frames[at][index]

    def _revalidate_subsprite(self) -> None:
        """Re-derive the outline after a render, dropping a pick that is gone.

        The refresh's counterpart to
        :meth:`~...selection.SelectionMixin._revalidate_selection`, and it has to
        run for the same two reasons: what is on screen may no longer be a sprite
        object at all, and the sheet's geometry moves under a pick that survives —
        Cols re-flows the frames, so the same subsprite is at a different pixel.
        """
        if (
            self._picked_subsprite is not None
            and self._picked_subsprite_record() is None
        ):
            self._picked_subsprite = None
        self._sync_subsprite_outline()

    # -- what reads it -------------------------------------------------------
    def _subsprite_rect(self) -> QRect | None:
        """The picked subsprite's box on the sheet, in image pixels.

        :func:`~celpix.pipeline.pipeline.sprite_image`'s own placement, which is
        why it is spelled out rather than taken from the record's offsets: a
        frame is drawn at its slot in the sheet and every frame shares one
        bounding box, so a subsprite's offset is two translations away from where
        its pixels landed.
        """
        doc, pick = self._doc, self._picked_subsprite
        sub = self._picked_subsprite_record()
        if doc is None or pick is None or sub is None:
            return None
        sheet = self._sprite_sheet()
        if sheet is None:
            return None
        at = pick[0]
        left, top, width, height = sheet.box
        wide, tall = sub.pixels(doc.tile_width, doc.tile_height)
        return QRect(
            (at % sheet.across) * width - left + sub.x,
            (at // sheet.across) * height - top + sub.y,
            wide,
            tall,
        )

    def _sync_subsprite_outline(self) -> None:
        self._canvas.set_pick_outline(self._subsprite_rect())

    def _announce_subsprite(self) -> None:
        """Status-line summary of the pick: which record, and what it draws.

        The tile is stated in the **bank's** numbers rather than the record's own,
        which is the one place they can differ and the reason to say it at all: an
        object bound with a base tile draws tile ``$10`` for a record holding
        ``$00``, and it is the bank number that a hex editor or the tile source
        panel's readout agrees with.

        It also says when there is more under the cursor than the one piece.
        Cycling is the one thing here a user cannot see: the outline moving is
        the only evidence a second press does anything, and that reads as a
        mis-hit unless something said the stack was there.
        """
        doc, pick = self._doc, self._picked_subsprite
        sub = self._picked_subsprite_record()
        if doc is None or pick is None or sub is None:
            return
        across, down = sub.size()
        parts = [
            f"{across}x{down} tiles",
            f"tile ${sub.index + doc.tile_base_index:X}",
            f"row {sub.palette_row}",
        ]
        if sub.flip_h:
            parts.append("H-flip")
        if sub.flip_v:
            parts.append("V-flip")
        cycle = (
            f" - click again for the next of {self._pick_depth} here"
            if self._pick_depth > 1
            else ""
        )
        self.statusBar().showMessage(
            f"Frame {pick[0]}, subsprite {pick[1]} of "
            f"{len(doc.shown_frames[pick[0]])} - {', '.join(parts)}{cycle}"
        )
