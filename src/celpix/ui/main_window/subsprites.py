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
"""

from __future__ import annotations

from celpix.pipeline import pipeline
from celpix.ui.widgets import Badge, counted


class SubspritesMixin:
    """Building and gating the subsprite sheet.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads the window's own widgets and its single live
    ``_doc``. See the package docstring for why these are mixins.
    """

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
        sheet = pipeline.subsprite_sheet(
            doc,
            self._registry,
            self._subsprites.columns(),
            by_frame=self._subsprites.by_frame(),
        )
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
            "This object's subsprites are not all one size, and a\n"
            "sheet has one square. Each square is the largest of\n"
            "them and a smaller piece is centred in it, so the\n"
            "space around a piece is the square, not the record.",
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
        """
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
        return next(
            (
                record
                for record in records
                if pipeline.subsprite_key(frames[record[0]][record[1]]) == key
            ),
            None,
        )
