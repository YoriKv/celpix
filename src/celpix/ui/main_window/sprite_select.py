"""Which subsprites are picked on a sprite object, and what follows from it.

A tilemap answers "what did I just click" with a **cell**: the canvas divides a
position by the grid and the whole editor reads the answer — the Cell spin, the
hex highlight, the tile source panel's ring. A sprite object has no grid to
divide by. Its records sit at signed pixel offsets that are mostly not
tile-aligned, they overlap, and one 8x8 square of the sheet routinely holds
pieces of three of them (``docs/design/sprite-map.md`` §2), so the slot
the canvas reports cannot name one — which would otherwise leave a sprite the one
kind of document where clicking the picture says nothing about what was clicked.

So the answer is resolved from the pixel instead: the canvas reports the **pixel**
a tile-mode press landed on
(``Canvas.pixel_picked``), :func:`~celpix.pipeline.pipeline.subsprites_at` runs
the render backwards to find which subsprites draw it, and the pick is held here
for what wants it — the outlines on the canvas, the ring in the tile source
panel over the tile the primary piece names, the marked palette row, and the
square on the sheet of records where one is open
(:mod:`celpix.ui.main_window.subsprites`).

**The pick holds record indices**, not ``(frame, subsprite)`` positions
(``docs/design/sprite-map.md`` §8.1): a position is a place in a list that
an add, a delete or a reorder renumbers, where a record — an index into
``doc.cells``, stamped on every piece as :attr:`~celpix.core.sprite.Subsprite.
record` — names the piece itself. That is what lets the pick survive an edit,
its undo, a Cols change and a re-read, and what makes the picked pieces the
selection a sprite map's cell controls act on
(:meth:`~...tilemap_edit.TilemapEditMixin._selected_cells`).

**It can hold several**, with the Files pane's vocabulary: a plain click picks
one, Ctrl+click toggles one, Shift+click spans from the anchor. The **last**
record is the primary — the one the status line describes and the single-answer
readers (the tile source ring, the palette row mark, the Subsprites square)
follow.

**A press on the same tile again picks the next one down.** One answer per press
would leave most of an object unreachable: subsprites overlap by design, and the
front-most one hides whole records behind it, so the stack is cycled rather than
resolved once (:meth:`~SpriteSelectMixin._cycled_pick`).

**It sits beside the tile selection rather than replacing it.** A press on a
sprite still selects the sheet tile under it, because that selection is what
pixel Copy lifts and what the palette-row readouts count; the pick says which
*records* drew what is in that square. Two overlapping answers to one press, and
they are drawn apart: the tile selection wears the app's selection white, the
pick the grid's structural blue — white for what the user pointed at, blue for
what it resolved to, which is the same pairing the tile source panel's two rings
make.

The pick is **session state, not the file's**, and it belongs to the entry it
was made on: another object with as many records has one at every index, which
the user never clicked.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import EllipsisType
from typing import TYPE_CHECKING

from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication

from celpix.core.sprite import Subsprite
from celpix.core.sprite_edit import record_positions
from celpix.pipeline import pipeline

if TYPE_CHECKING:
    # For the checker only: a mixin is a slice of the window, so its
    # ``self`` is the window, widgets, state and all (see the package
    # docstring). At runtime it is a plain mixin.
    from celpix.ui.main_window.window import MainWindow as _Host
else:
    _Host = object


class SpriteSelectMixin(_Host):
    """The picked subsprites of the sprite object on screen.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads the window's own single live ``_doc`` and its
    widgets. See the module docstring for what it owns, and the package docstring
    for why these are mixins.
    """

    def _init_sprite_select(self) -> None:
        # Record indices into ``doc.cells``, in pick order; the last is the
        # primary. A record stays here while it is not drawn — an OBJ slot whose
        # drawn bit was just cleared — so the control that cleared it can put it
        # back; it simply has no outline until it is drawn again.
        self._picked_records: list[int] = []
        # The record a Shift+click spans from: the primary as the last plain or
        # Ctrl click left it, which a span itself keeps.
        self._pick_anchor: int | None = None
        # The sheet tile the last plain press landed in, and how many subsprites
        # it had under it — what makes the *next* press a cycle rather than a
        # fresh pick (:meth:`_cycled_pick`).
        self._pick_tile: tuple[int, int] | None = None
        self._pick_depth: int = 0
        # The entry the pick was made on. A record index names a record of
        # *that* object; another object has one at the same index, which the
        # user never clicked.
        self._picked_entry = None

    def _connect_sprite_canvas(self) -> None:
        """Wire the canvas's pixel report (called once the canvas exists)."""
        self._canvas.pixel_picked.connect(self._on_pixel_picked)
        self._connect_subsprite_sheet()

    # -- picking -------------------------------------------------------------
    def _on_pixel_picked(
        self, x: int, y: int, modifiers: Qt.KeyboardModifier | None = None
    ) -> None:
        """A tile-mode press: pick the subsprite under it, on a sprite object.

        Silent on everything else — the signal is emitted for every tile-mode
        press, and a document with a cell grid has already answered the same
        press through the selection.

        The modifiers are the application's at the press, since the signal
        carries none (``modifiers`` stands in for them where a caller has its
        own): plain picks one, cycling; Ctrl toggles the front piece in or out;
        Shift spans from the anchor. A press on no piece clears the pick, unless
        Ctrl is held — a toggle that misses changes nothing, as in a list.
        """
        doc = self._doc
        if doc is None or not doc.is_sprite:
            return
        if modifiers is None:
            modifiers = QApplication.keyboardModifiers()
        frames = doc.shown_frames
        # Pieces with no record (a sprite table's) cannot be held: the pick is
        # a record index, and they have none to name.
        order = [
            (at, k)
            for at, k in pipeline.subsprites_at(
                doc, self._registry, self._tilemap_columns(), x, y
            )
            if frames[at][k].record is not None
        ]
        tile = (x // max(1, doc.tile_width), y // max(1, doc.tile_height))
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if not order:
            if not ctrl:
                self._set_picked_records([])
            return
        if ctrl or shift:
            # Neither cycles, so neither leaves a press for the next plain one
            # to cycle from: it starts at the front again.
            self._pick_tile, self._pick_depth = None, 0
            front = frames[order[0][0]][order[0][1]].record
            if ctrl:
                picked = [r for r in self._picked_records if r != front]
                if len(picked) == len(self._picked_records):
                    picked.append(front)
                self._set_picked_records(picked, anchor=front)
            else:
                self._set_picked_records(
                    self._spanned_records(order[0]), anchor=self._pick_anchor
                )
            return
        pick = self._cycled_pick(order, tile)
        self._pick_tile, self._pick_depth = tile, len(order)
        self._set_picked_records([frames[pick[0]][pick[1]].record])

    def _spanned_records(self, clicked: tuple[int, int]) -> list[int]:
        """The records a Shift+click on ``clicked`` picks: the anchor's span.

        In the clicked frame's draw order, walked **from the anchor's position
        to the clicked piece**, so the clicked piece comes last and is the
        primary, as the row clicked is the current one in a list. The anchor's
        position is its place in its own frame, read in the clicked one — a span
        is a run of one stack, and an anchor in another frame still says where
        that run starts. With no anchor drawn the span is the clicked piece.
        """
        frames = self._doc.shown_frames
        at, end = clicked
        frame = frames[at]
        place = self._record_positions().get(self._pick_anchor)
        start = end if place is None else min(place[1], len(frame) - 1)
        step = 1 if end >= start else -1
        return [
            frame[k].record
            for k in range(start, end + step, step)
            if frame[k].record is not None
        ]

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

    def _set_picked_records(
        self, records: Sequence[int], *, anchor: int | None | EllipsisType = ...
    ) -> None:
        """Hold ``records`` (``[]`` clears), and converge everything that reads it.

        Repeats are dropped, keeping the first. ``anchor`` is where the next
        Shift+click spans from; left out, it is the new primary — which is what
        every pick but a span and a toggle means by it.
        """
        self._picked_records = list(dict.fromkeys(records))
        if self._picked_records:
            self._pick_anchor = self._picked_records[-1] if anchor is ... else anchor
            self._picked_entry = self._workspace.current
        else:
            # Nothing picked is nothing to cycle or span from: a press that found
            # no piece, and the document being closed, both land here.
            self._pick_anchor = self._picked_entry = None
            self._pick_tile, self._pick_depth = None, 0
        self._sync_subsprite_outline()
        # The tile source panel's ring marks what the canvas is pointing at, and
        # on a sprite object this is what that is. The palette grid's ring is the
        # same answer read the other way: which row the picked record draws in.
        self._sync_tile_source_marker()
        self._sync_marked_palette_row()
        # And the third ring, where the sheet of records is open: the same pick,
        # shown as which of the object's pieces it is
        # (:mod:`celpix.ui.main_window.subsprites`).
        self._sync_subsprite_square()
        # The pick is a sprite map's *selection* (`_selected_cells`), and a pick
        # moves without the selection pass or a render running: the controls
        # that act on the selection have to be told here, or the property row
        # and the Cell spin sit describing the pick before last.
        self._sync_edit_actions()
        self._sync_transform_actions()
        self._sync_cell_index()
        self._sync_cell_props()
        self._announce_subsprite()

    def _record_positions(self) -> dict[int, tuple[int, int]]:
        """``record -> (frame, position)`` over the shown frames; ``{}`` off a
        sprite (:func:`~celpix.core.sprite_edit.record_positions`)."""
        doc = self._doc
        if doc is None or not doc.is_sprite:
            return {}
        return record_positions(doc.shown_frames)

    def _picked_subsprites(self) -> list[tuple[int, Subsprite]]:
        """``(record, piece)`` for each picked record that is drawn, in pick order.

        A picked record that is not drawn has no piece to answer with and is left
        out, though it stays picked (:meth:`_init_sprite_select`).
        """
        positions = self._record_positions()
        if not positions:
            return []
        frames = self._doc.shown_frames
        return [
            (record, frames[place[0]][place[1]])
            for record in self._picked_records
            if (place := positions.get(record)) is not None
        ]

    @property
    def _picked_subsprite(self) -> tuple[int, int] | None:
        """The primary record's ``(frame, subsprite)``, or None.

        None as well while the primary is not drawn: the readers that want a
        position (the Subsprites window's square, the cycle) have nothing to
        point at then.
        """
        if not self._picked_records:
            return None
        return self._record_positions().get(self._picked_records[-1])

    def _picked_subsprite_record(self) -> Subsprite | None:
        """The primary picked subsprite itself, or ``None`` with none drawn."""
        pick = self._picked_subsprite
        if pick is None:
            return None
        return self._doc.shown_frames[pick[0]][pick[1]]

    def _revalidate_subsprite(self) -> None:
        """Re-derive the outlines after a render, migrating the pick.

        The refresh's counterpart to
        :meth:`~...selection.SelectionMixin._revalidate_selection`, and it has to
        run for the same two reasons: what is on screen may no longer be a sprite
        object at all, and the sheet's geometry moves under a pick that survives —
        Cols re-flows the frames, so the same subsprite is at a different pixel.

        A record survives while it is still a cell of the same entry's document:
        an edit, its undo, a Cols change and a re-read all leave the record
        naming the same piece, wherever that piece is now drawn. A pick made on
        another entry is dropped whole.
        """
        if self._picked_records:
            doc = self._doc
            same = (
                self._picked_entry is self._workspace.current
                and doc is not None
                and doc.is_sprite
            )
            count = len(doc.cells or ()) if same else 0
            self._picked_records = [r for r in self._picked_records if 0 <= r < count]
            anchor = self._pick_anchor
            if not self._picked_records:
                # Nor is there a press to cycle or span from: the next one on
                # this object takes the front piece.
                self._pick_anchor = self._picked_entry = None
                self._pick_tile, self._pick_depth = None, 0
            elif anchor is None or not 0 <= anchor < count:
                self._pick_anchor = self._picked_records[-1]
        self._sync_subsprite_outline()
        self._sync_origin_marks()
        # Before the sheet is recomposed, not after: that is debounced, and a
        # ring left on a record that is gone is a ring on a different piece.
        self._sync_subsprite_square()

    # -- what reads it -------------------------------------------------------
    def _subsprite_rects(self) -> list[QRect]:
        """Each drawn picked subsprite's box on the sheet, in image pixels.

        :func:`~celpix.pipeline.pipeline.sprite_image`'s own placement, which is
        why it is spelled out rather than taken from the record's offsets: a
        frame is drawn at its slot in the sheet and every frame shares one
        bounding box, so a subsprite's offset is two translations away from where
        its pixels landed.
        """
        doc = self._doc
        sheet = self._sprite_sheet()
        if doc is None or sheet is None or not self._picked_records:
            return []
        left, top, width, height = sheet.box
        positions = self._record_positions()
        frames = doc.shown_frames
        rects = []
        for record in self._picked_records:
            place = positions.get(record)
            if place is None:
                continue
            at, index = place
            sub = frames[at][index]
            wide, tall = sub.pixels(doc.tile_width, doc.tile_height)
            rects.append(
                QRect(
                    (at % sheet.across) * width - left + sub.x,
                    (at // sheet.across) * height - top + sub.y,
                    wide,
                    tall,
                )
            )
        return rects

    def _sync_subsprite_outline(self) -> None:
        self._canvas.set_pick_outline(self._subsprite_rects())

    def _sync_origin_marks(self) -> None:
        """Mark each frame slot's object origin (``Canvas.set_origin_marks``).

        Pixel ``(-left, -top)`` of the slot, the shared box being placed so: the
        strip is trimmed to the pieces' bounds, so without it nothing says where
        the offsets are measured from (``sprite-map.md`` §10.1).
        """
        sheet = self._sprite_sheet()
        if sheet is None:
            self._canvas.set_origin_marks(None)
            return
        left, top, width, height = sheet.box
        self._canvas.set_origin_marks(
            [
                (
                    (at % sheet.across) * width - left,
                    (at // sheet.across) * height - top,
                )
                for at in range(sheet.frames)
            ]
        )

    def _announce_subsprite(self) -> None:
        """Status-line summary of the pick: which record, and what it draws.

        The tile is stated in the **bank's** numbers rather than the record's own,
        which is the one place they can differ and the reason to say it at all: an
        object bound with a base tile draws tile ``$10`` for a record holding
        ``$00``, and it is the bank number that a hex editor or the tile source
        panel's readout agrees with. The offset and the frame's box beside it
        are what a move is measured in: the box says where (0, 0) is.

        It also says when there is more under the cursor than the one piece.
        Cycling is the one thing here a user cannot see: the outline moving is
        the only evidence a second press does anything, and that reads as a
        mis-hit unless something said the stack was there.
        """
        doc = self._doc
        drawn = self._picked_subsprites()
        if doc is None or not drawn:
            return
        if len(self._picked_records) > 1:
            positions = self._record_positions()
            spread = sorted({positions[record][0] for record, _ in drawn})
            where = (
                f"in frame {spread[0]}"
                if len(spread) == 1
                else f"across {len(spread)} frames"
            )
            hidden = len(self._picked_records) - len(drawn)
            tail = f", {hidden} not drawn" if hidden else ""
            self.statusBar().showMessage(
                f"{len(drawn)} subsprites picked {where}{tail}"
            )
            return
        pick, sub = self._picked_subsprite, self._picked_subsprite_record()
        if pick is None or sub is None:
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
        box = ""
        sheet = self._sprite_sheet()
        if sheet is not None:
            left, top, width, height = sheet.box
            box = f" - frame box {width}x{height} from ({left:+d}, {top:+d})"
        cycle = (
            f" - click again for the next of {self._pick_depth} here"
            if self._pick_depth > 1
            else ""
        )
        self.statusBar().showMessage(
            f"Frame {pick[0]}, subsprite {pick[1]} of "
            f"{len(doc.shown_frames[pick[0]])} at ({sub.x:+d}, {sub.y:+d}) - "
            f"{', '.join(parts)}{box}{cycle}"
        )
