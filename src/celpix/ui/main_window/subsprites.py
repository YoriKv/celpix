"""The Subsprites window's window side: what it is handed, and when.

The window itself is presentation only (:mod:`celpix.ui.subsprite_window`) — it
holds a composed sheet, the records it covers and the one the canvas picked, and
it never reads the model. This is the half that builds those from the live
document.

Three things it has to get right, and none of them is the window's to know:

- **The records are the canvas's.** They are indices into
  :attr:`~celpix.core.document.Document.shown_frames`, which is the numbering the
  canvas's own pick speaks (:mod:`celpix.ui.main_window.sprite_select`), so a
  square and a pick can be compared directly. Numbering the sheet off the
  untrimmed frames instead — the animation player's reading, and right for a
  player, since a sequence can name a frame the trim drops — would ring the wrong
  square on every object with a trailing template.
- **The colour rule is the document's.** The sheet goes through the same
  ``_tilemap_grid_image`` the canvas uses, so a piece here is the piece on the
  canvas rather than a second rendering that could drift from it.
- **Offered on every sprite map, not only an animated one.** Its neighbour in the
  View menu is gated on there being a sequence with a step in it; the gate here
  is a record to show, which all but an empty object has.

The window's **Frames** box picks which of two sheets it is showing — the file's
listing, one square per record, or its inventory, one per distinct piece — and
that choice reaches the ring as well as the picture: with the repetition taken
out, "which square is the pick in" stops being a lookup and becomes a question
about the art (:meth:`~SubspritesMixin._subsprite_square`).

**The squares are addressable** (``docs/design/sprite-map.md`` §10.4): every
editor surveyed splits the work as *the canvas moves; the list holds the
structure*, and this window is the list. A click picks, with the Files pane's
Ctrl and Shift, so a piece covered on the canvas is reachable by its square
(:meth:`~SubspritesMixin._on_subsprite_square_clicked`); a drag reorders within
a frame or moves to another (:meth:`~SubspritesMixin._on_subsprite_square_dragged`).
A click picks on a view-only map too — the pick is a reading — where a drag,
which writes, is refused with the reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt

from celpix.core.sprite_edit import move_subsprite, reorder_subsprite
from celpix.pipeline import pipeline
from celpix.ui.widgets import Badge, counted

if TYPE_CHECKING:
    # For the checker only: a mixin is a slice of the window, so its
    # ``self`` is the window, widgets, state and all (see the package
    # docstring). At runtime it is a plain mixin.
    from celpix.ui.main_window.window import MainWindow as _Host
else:
    _Host = object


class SubspritesMixin(_Host):
    """Building and gating the subsprite sheet.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads the window's own widgets and its single live
    ``_doc``. See the package docstring for why these are mixins.
    """

    def _connect_subsprite_sheet(self) -> None:
        """Wire the window's square reports (called once the window exists)."""
        self._subsprites.square_clicked.connect(self._on_subsprite_square_clicked)
        self._subsprites.square_dragged.connect(self._on_subsprite_square_dragged)

    def _subsprites_available(self) -> bool:
        """Whether this document has subsprites worth opening a sheet on.

        Every sprite map is made of records and almost every one holds some, so
        this is a far wider gate than the animation player's — what it excludes
        is the object whose frames are all empty, which would otherwise open a
        window with nothing in it.
        """
        doc = self._doc
        return bool(doc is not None and doc.is_sprite and any(doc.shown_frames))

    def _show_subsprites(self) -> None:
        """View ▸ Subsprites — compose the sheet and hand it to the window."""
        if not self._subsprites_available():
            self._subsprites.hide_overlay()
            return
        doc = self._doc
        entry = self._workspace.current
        # The pieces are decoded through the object's tile codec, which can
        # raise as the canvas's render can: inside the same boundary, so it is
        # told once, and the sheet closes rather than the error escaping the
        # window's refresh.
        sheet = self._quiet_render(
            lambda: pipeline.subsprite_sheet(
                doc,
                self._registry,
                self._subsprites.columns(),
                by_frame=self._subsprites.by_frame(),
            )
        )
        if sheet is None:
            self._subsprites.hide_overlay()
            return
        name = entry.name if entry is not None else "object"
        self._subsprites.show_sheet(
            self._tilemap_grid_image(sheet.grid),
            sheet.records,
            sheet.boxes,
            sheet.cell,
            f"Subsprites - {name}",
            # Resolved against the sheet just composed, not the one on screen:
            # under the inventory reading the squares move when the reading is
            # switched, and the ring has to land on the new ones.
            marked=self._subsprite_square(sheet.records),
            status=self._subsprites_status(sheet),
            badge=self._subsprites_badge(sheet),
        )

    def _subsprites_status(self, sheet) -> str:  # noqa: ANN001 — a SubspriteSheet
        """The readout: what the squares are counting, and how big one is.

        Two sentences for the two readings, because the same number means
        different things in them: under the listing it is how many records the
        file holds, under the inventory how much art there is *behind* them —
        and that second one is only worth anything beside the total it collapsed.
        """
        doc = self._doc
        across = sheet.cell[0] // max(1, doc.tile_width)
        down = sheet.cell[1] // max(1, doc.tile_height)
        squares = f"squares are {across}x{down} tiles"
        if not self._subsprites.by_frame():
            total = sum(len(frame) for frame in doc.shown_frames)
            return (
                f"{counted(len(sheet.records), 'subsprite')} in "
                f"{counted(total, 'record')} - {squares}"
            )
        frames = len({at for at, _index in sheet.records})
        return (
            f"{counted(len(sheet.records), 'subsprite')} over "
            f"{counted(frames, 'frame')} - {squares}"
        )

    def _subsprites_badge(self, sheet) -> Badge | None:  # noqa: ANN001 — SubspriteSheet
        """Said in words where the sheet's own layout is hiding something.

        A square is the largest subsprite in the object, so on one that mixes
        sizes the smaller pieces are drawn in a box bigger than they are. That is
        legible once you know it and misleading until you do — a 1x1 piece
        centred in a 2x2 square looks like a 2x2 piece three quarters erased.
        """
        doc = self._doc
        frames = doc.shown_frames
        sizes = {frames[at][index].size() for at, index in sheet.records}
        if len(sizes) < 2:
            return None
        return Badge(
            f"{len(sizes)} sizes",
            "Subsprites of several sizes: every square is the\n"
            "largest size, with smaller pieces centered in it",
        )

    def _sync_subsprites(self) -> None:
        """Close the sheet when the entry it was opened on is no longer showing.

        Called where the document changes rather than on a timer: the window
        holds its own copy of the sheet, so one left open over a different entry
        would go on showing pieces of a file that is nowhere on screen.
        """
        if not self._subsprites.isVisible():
            return
        if not self._subsprites_available():
            self._subsprites.hide_overlay()
            return
        # Asked for rather than done, the animation player's rule: this runs on
        # each refresh of the entry underneath — once per pixel of a stroke — and
        # the window coalesces the burst (`request_refresh`), so an open sheet
        # costs one recompose per burst instead of one per repaint.
        self._subsprites.request_refresh()

    def _sync_subsprite_square(self) -> None:
        """Ring the picked record on the sheet, if one is open.

        The canvas outline and the tile source panel's ring read the pick the
        same way and this is the third of them; it moves a ring rather than
        recomposing, so it can run on every press.

        Nothing while the sheet is hidden: its records were composed for
        whatever entry it last showed, and are recomposed when it opens again.
        """
        if not self._subsprites.isVisible():
            return
        self._subsprites.set_marked(self._subsprite_square(self._subsprites.records()))

    def _subsprite_square(
        self, records: list[tuple[int, int]]
    ) -> tuple[int, int] | None:
        """Which of ``records`` holds the canvas's pick — the ring's square.

        Under the **listing** reading every record is its own square and the
        answer is the pick itself. Under the **inventory** reading a square is
        the first of the records drawing one piece, so a pick landing on any of
        the others is not in the list at all — and the honest answer is not "no
        square" but the square its art is in, which is what
        :func:`~celpix.pipeline.pipeline.subsprite_key` decides. Clicking any
        occurrence of a piece therefore rings the one square that stands for it,
        which is the only reading of the ring that survives the repetition being
        taken out.

        Answered here rather than in the panel because it is a question about the
        model: the panel holds pairs and has no frames to look a piece up in.
        """
        doc, pick = self._doc, self._picked_subsprite
        if doc is None or pick is None or not records:
            return None
        if pick in records:
            return pick
        frames = doc.shown_frames
        if not (0 <= pick[0] < len(frames) and 0 <= pick[1] < len(frames[pick[0]])):
            return None
        key = pipeline.subsprite_key(frames[pick[0]][pick[1]])
        # Bounds-checked: an open sheet's records trail an entry switch until its
        # coalesced refresh lands, and may name records this object has not got.
        return next(
            (
                record
                for record in records
                if 0 <= record[0] < len(frames)
                and 0 <= record[1] < len(frames[record[0]])
                and pipeline.subsprite_key(frames[record[0]][record[1]]) == key
            ),
            None,
        )

    # -- the squares, addressed ----------------------------------------------
    def _square_piece(self, frame: int, sub: int):  # noqa: ANN202 — a Subsprite
        """The piece a square names in the shown frames, or None.

        Bounds-checked: an open sheet's records trail an entry switch until its
        coalesced refresh lands, and may name a square this object has not got.
        """
        doc = self._doc
        if doc is None or not doc.is_sprite:
            return None
        frames = doc.shown_frames
        if not (0 <= frame < len(frames) and 0 <= sub < len(frames[frame])):
            return None
        return frames[frame][sub]

    def _on_subsprite_square_clicked(
        self, frame: int, sub: int, modifiers: Qt.KeyboardModifier
    ) -> None:
        """A press on a square: pick its record, with the Files pane's keys.

        Plain picks the one record; Ctrl toggles it in or out; Shift spans from
        the anchor in that frame's draw order — the canvas pick's own reading
        (:meth:`~...sprite_select.SpriteSelectMixin._on_pixel_picked`), so a
        piece covered on the canvas is reached by its square on the same
        terms. Under the inventory a square already *is* the first occurrence,
        so a click picks that; with *Select All Occurrences* on it picks every
        record whose art matches (:func:`~celpix.pipeline.pipeline.
        subsprite_key`), the clicked one last so it is the primary.
        """
        piece = self._square_piece(frame, sub)
        if piece is None:
            return
        record = piece.record
        if record is None:
            self.statusBar().showMessage(
                "This piece comes from no record - a table's pieces are not "
                "editable here."
            )
            return
        if self._subsprites.all_occurrences():
            key = pipeline.subsprite_key(piece)
            same = [
                other.record
                for drawn in self._doc.shown_frames
                for other in drawn
                if other.record is not None
                and other.record != record
                and pipeline.subsprite_key(other) == key
            ]
            self._set_picked_records([*same, record])
            return
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            picked = [r for r in self._picked_records if r != record]
            if len(picked) == len(self._picked_records):
                picked.append(record)
            self._set_picked_records(picked, anchor=record)
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            self._set_picked_records(
                self._spanned_records((frame, sub)), anchor=self._pick_anchor
            )
        else:
            self._set_picked_records([record])
        # Neither cycles a stack: the next canvas press starts at the front.
        self._pick_tile, self._pick_depth = None, 0

    def _on_subsprite_square_dragged(
        self, source: tuple[int, int], target: tuple[int, int]
    ) -> None:
        """A drag between squares: reorder within a frame, or move to another.

        The listing's alone — under the inventory a square is several records
        and a position in no frame. Within a frame the piece takes the target's
        place in draw order (:func:`~celpix.core.sprite_edit.reorder_subsprite`,
        the step Alt+Up/Down pushes); onto another frame's square it joins the
        back of that frame (:func:`~celpix.core.sprite_edit.move_subsprite`).
        Either is one step that keeps the piece picked, and a format that
        cannot spell it says why on the status line.
        """
        doc = self._doc
        if doc is None or not doc.is_sprite or doc.cells is None:
            return
        if not self._subsprites.by_frame():
            self.statusBar().showMessage(
                "Turn Frames on to reorder - a square here stands for several records."
            )
            return
        limits = self._subsprite_limits()
        if limits is None:
            self._refuse_view_only()
            return
        piece = self._square_piece(*source)
        if piece is None or self._square_piece(*target) is None:
            return
        record = piece.record
        if record is None:
            return
        frames = doc.sprite_frames or []
        before = self._selected_cells()
        try:
            if source[0] == target[0]:
                cells = reorder_subsprite(
                    doc.cells,
                    frames,
                    limits,
                    record,
                    target[1],
                    assign=self._assign_frames,
                )
                # The frame's pieces are permuted among the indices it already
                # holds, so the moved one lands on the target's place among them.
                held = sorted(
                    sub.record for sub in frames[source[0]] if sub.record is not None
                )
                moved = held[min(target[1], len(held) - 1)]
                said = f"Moved subsprite {source[1]} to place {target[1]}"
                said += f" in frame {source[0]}."
            else:
                cells, moved = move_subsprite(
                    doc.cells,
                    frames,
                    limits,
                    record,
                    target[0],
                    place=self._place_subsprite,
                    assign=self._assign_frames,
                )
                said = f"Moved subsprite {source[1]} of frame {source[0]} to frame"
                said += f" {target[0]}."
        except ValueError as exc:
            reason = str(exc)
            self.statusBar().showMessage(
                f"{reason[:1].upper()}{reason[1:]} - nothing changed."
            )
            return
        if self._apply_cells(cells, "reorder subsprites", pick=(before, [moved])):
            self.statusBar().showMessage(said)
