"""Copy, cut, clear and paste over the tile selection.

The gesture layer between the selection and the bytes. What is selected is
:mod:`~celpix.ui.main_window.selection`'s answer and how a run reaches the file
is :mod:`~celpix.ui.main_window.tile_bytes`'; this is the part in between - what
each of the four clipboard verbs *means* on a run of tiles, and how the world
outside celPix is met half way.

Two things run through all of it. A copy leaves **both representations** on the
clipboard (see :mod:`celpix.ui.clipboard`): the tiles themselves, so a paste back
into celPix is lossless whatever palette either view renders through, and a
rendered picture, so every other program sees an ordinary image. A paste reads
them back in that order of fidelity, quantizing into the active subpalette only
when nothing better arrived.

And every write is an **overwrite, clipped at the end of the data**. The bytes
sit in a fixed slot of the source file, so a paste replaces exactly as many tiles
as it carries and editing never grows a file. Which tiles those are depends on
the selection shape: a linear payload lands as a contiguous run, a picture - and
anything pasted in Rectangle shape - lands as the rectangle it looks like,
anchored at the selection's cell.

The actual encode, the undo command and the byte splice are not here: every write
below leaves through :meth:`~...tile_bytes.TileBytesMixin._apply_tile_edit` or
:meth:`~...tile_bytes.TileBytesMixin._edit_run`. Neither is the cell clipboard -
a tilemap's copy moves indices into somebody else's tiles, which is a different
payload and lives in :mod:`~celpix.ui.main_window.tilemap_edit`; the four verbs
below dispatch to it through :class:`~...capability_sync.Gesture`. The pixel-mode
clipboard is likewise :mod:`~celpix.ui.main_window.pixel_edit`'s.
"""

from __future__ import annotations

from PySide6.QtGui import QImage

from celpix.core import ceil_div
from celpix.core.arrangement import (
    BlockLayout,
    compose_window,
)
from celpix.core.quantize import QuantizeReport
from celpix.pipeline import importer
from celpix.pipeline.importer import ImportedTiles
from celpix.ui import clipboard, render_bridge
from celpix.ui.main_window.capability_sync import Gesture
from celpix.ui.main_window.selection import SelectionShape
from celpix.ui.tools import EditMode
from celpix.ui.widgets import (
    counted,
)


class ClipboardOpsMixin:
    """The four clipboard verbs over the tile selection, and the paste anchor.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _paste_anchor(self) -> int:
        """:meth:`_anchor_tile`, guaranteed on-screen.

        A paste maps its anchor to a cell through ``anchor - self._offset``, so a
        selection scrolled out of the visible window resolves to a cell off the
        grid and writes nothing. Snap it onto the visible top-left tile first, so
        a paste or import lands where the user can see it. The single guard every
        entry point (paste, Import from PNG, a dropped PNG) goes through.
        """
        if self._selection_offscreen():
            self._select_tiles(self._offset, self._offset)
        return self._anchor_tile()

    def _copy_selection(self) -> bool:
        """Put the selected tiles on the clipboard; False if there are none.

        Both representations go out at once (see :mod:`celpix.ui.clipboard`):
        the tiles themselves for a lossless paste back into celPix, and a
        rendered image so every other program sees an ordinary picture. A
        rectangle selection copies only its own cells - the enclosing run is
        decoded (the file is linear), then the gap tiles are dropped.
        """
        if self._edit_mode is EditMode.PIXEL:
            self._pixel_copy()
            return True
        if (copy := self._kind_handler(Gesture.COPY)) is not None:
            # Cells are indices into a tile source another program knows nothing
            # about, so they stay in celPix rather than going out as numbers
            # (:mod:`celpix.ui.main_window.tilemap_edit`).
            return copy()
        selected = self._selection_tiles()
        run = self._selection_bounding_run()
        if self._doc is None or run is None:
            return False
        first, count = run
        decoded = self._decode_run(first, count)
        if not decoded:
            return False
        kept = [t for t in selected if t - first < len(decoded)]
        tiles = [decoded[t - first] for t in kept]
        if not tiles:
            return False
        target = self._import_target()
        cols = self._copy_columns(len(tiles))
        clipboard.put(
            clipboard.TilePayload.from_tiles(tiles, target.colors, columns=cols),
            self._copy_image(tiles, cols, self._tile_biases(kept)),
        )
        self._sync_edit_actions()
        self.statusBar().showMessage(f"Copied {counted(len(tiles), 'tile')}.")
        return True

    def _copy_columns(self, count: int) -> int:
        """How many cells wide a copy of ``count`` tiles reads on screen.

        A rectangle copies at its own width; a linear run wraps at the view's
        columns, or is a single short row when it doesn't reach that far.
        """
        if self._rect_size is not None:
            return max(1, min(self._rect_size[0], count))
        view_cols = self._columns.value()
        return view_cols if count > view_cols else max(1, count)

    def _copy_image(
        self, tiles: list, columns: int, biases: list[int] | None = None
    ) -> QImage:
        """Render a copied run the way the canvas shows it.

        A linear run is laid out through the view's own arrangement, so a blocked
        view copies a 16×16 metatile as a square rather than as a strip of four
        tiles. A **rectangle** is already in screen order, so it composes plainly
        at its own width - re-applying the block layout would scramble it. Colors
        are the canvas's - no forced index-0 transparency, so a copy that goes out
        to an image editor and comes back matches its own palette exactly.

        ``biases`` carries pinned palette regions, one per tile in ``tiles``, so a
        copy of a pinned region leaves in the colours it was shown in. It applies
        only to this rendered *image*: the lossless payload beside it on the
        clipboard keeps the tiles' real indices, because that is what a paste back
        into celPix has to reproduce.
        """
        assert self._doc is not None
        if biases:
            tiles = [
                tile.shifted(bias) if bias and tile.bytes_per_pixel == 1 else tile
                for tile, bias in zip(tiles, biases, strict=True)
            ]
        layout = (
            BlockLayout(columns)
            if self._rect_size is not None
            else BlockLayout(
                columns,
                self._block_cols.value(),
                self._block_rows.value(),
                self._block_order.currentData(),
            )
        )
        rows = 1 + max(layout.slot_to_pos(slot)[1] for slot in range(len(tiles)))
        grid = compose_window(tiles, columns, 0, rows, layout)
        return render_bridge.render(grid, self._doc.palette, self._palette_base())

    def _blank_selection(self, text: str) -> int:
        """Blank every selected tile as one edit; returns how many were written.

        The edit is expressed over the selection's *enclosing* run because that
        is what encodes back to a contiguous byte region; a rectangle's gap tiles
        are decoded and written back unchanged, so only its own cells clear.
        """
        selected = self._selection_tiles()
        run = self._selection_bounding_run()
        if run is None:
            return 0
        first, count = run
        if len(selected) == count:  # contiguous - nothing to preserve
            tiles = self._blank_tiles(count)
        else:
            tiles = self._decode_run(first, count)
            if not tiles:
                return 0
            for blank, tile in zip(
                self._blank_tiles(len(selected)), selected, strict=True
            ):
                if tile - first < len(tiles):
                    tiles[tile - first] = blank
        written = self._apply_tile_edit(first, tiles, text)
        return sum(1 for tile in selected if tile - first < written)

    def _cut_selection(self) -> None:
        if self._edit_mode is EditMode.PIXEL:
            self._pixel_cut()
            return
        if (cut := self._kind_handler(Gesture.CUT)) is not None:
            cut()
            return
        if not self._copy_selection():
            return
        written = self._blank_selection("cut tiles")
        if written:
            self.statusBar().showMessage(f"Cut {counted(written, 'tile')}.")

    def _clear_selection_contents(self) -> None:
        if self._edit_mode is EditMode.PIXEL:
            self._pixel_clear()
            return
        if (clear := self._kind_handler(Gesture.CLEAR)) is not None:
            clear()
            return
        written = self._blank_selection("clear tiles")
        if written:
            self.statusBar().showMessage(f"Cleared {counted(written, 'tile')}.")

    def _paste(self) -> None:
        """Write the clipboard over the tiles from the selection anchor onward.

        Overwrite, never insert: the bytes sit in a fixed slot in the source
        file, so a paste replaces exactly as many tiles as it carries and is
        clipped at the end of the data. With nothing selected - or a selection
        scrolled off-screen (:meth:`_paste_anchor`) - it lands at the top-left
        tile of the view.

        A foreign **image** is pixels, not tiles, so it always lands as the
        picture it shows, anchored at the selection's cell - the same landing
        Import from PNG gives it. A celPix **tile** payload follows the
        selection shape: in Rectangle it is laid down as a rectangle of its own
        width down from the anchor cell - copy a 2×2 metatile, click anywhere,
        and it lands as a 2×2 metatile - while in Linear shape a paste is what
        it has always been: a contiguous run.
        """
        if self._doc is None:
            return
        if self._edit_mode is EditMode.PIXEL:
            self._pixel_paste()
            return
        if (paste := self._kind_handler(Gesture.PASTE)) is not None:
            paste()
            return
        first = self._paste_anchor()
        incoming, picture = self._clipboard_tiles()
        if not incoming.tiles:
            self.statusBar().showMessage("Nothing on the clipboard to paste here.")
            return
        note = self._fit_note(incoming.report)
        if picture or self._selection_shape.currentData() is SelectionShape.RECT:
            written = self._paste_pixel_rect(first, incoming, "paste tiles")
        else:
            written = self._write_run(first, incoming, "paste tiles")
        if not written:
            self.statusBar().showMessage("Nothing pasted - no room at this offset.")
            return
        message = f"Pasted {counted(written, 'tile')}"
        if len(incoming.tiles) > written:
            clipped = len(incoming.tiles) - written
            message += f" ({clipped} clipped at the end of the data)"
        self.statusBar().showMessage(message + (f" - {note}." if note else "."))

    def _write_run(self, first: int, incoming: ImportedTiles, text: str) -> int:
        """Write ``incoming`` as a contiguous run from ``first`` - a linear paste.

        Only celPix tile payloads land here (an image always lands as a
        picture), and those carry whole tiles - no partial coverage to merge.
        """
        written = self._apply_tile_edit(first, incoming.tiles, text)
        if written:
            self._select_tiles(first, first + written - 1)
        return written

    def _paste_pixel_rect(self, anchor: int, incoming: ImportedTiles, text: str) -> int:
        """Write ``incoming`` as the picture it is, at ``anchor``'s canvas slot.

        ``incoming.tiles`` are the picture's tiles in screen reading order,
        ``incoming.columns`` wide - the pasted pixels as they should *look*.
        Each slot becomes an absolute tile through the view's arrangement, so
        the write lands where it looks like it lands, exactly as if the pixels
        had been painted by hand; slots that fall off the right edge of the
        view are dropped rather than wrapped, since wrapping would scatter the
        picture. The write itself goes out over the enclosing run, with the
        untouched tiles decoded and put back unchanged - and each partly
        covered edge tile merged with the one already there, so only the
        pixels the source actually reached change.
        """
        assert self._doc is not None
        columns = max(1, incoming.columns)
        layout = self._view_layout()
        x0, y0 = layout.slot_to_pos(anchor - self._offset)
        placed: dict[int, tuple[object, tuple[int, int] | None]] = {}
        for i, tile in enumerate(incoming.tiles):
            target = self._cell_tile(layout, x0 + i % columns, y0 + i // columns)
            if target is not None:
                placed[target] = (tile, incoming.covered(i))
        if not placed:
            return 0
        first, last = min(placed), max(placed)

        def mutate(run: list) -> None:
            for target, (tile, covered) in placed.items():
                if target - first < len(run):
                    run[target - first] = importer.merge_uncovered(
                        tile, run[target - first], covered
                    )

        if not self._edit_run(first, last - first + 1, mutate, text):
            return 0
        rows = ceil_div(len(incoming.tiles), columns)
        size = (columns, rows)
        rect = self._rect_tiles_for(anchor - self._offset, *size)
        if rect:
            self._set_rect_selection(size, rect)
        return len(placed)

    def _clipboard_tiles(self) -> tuple[ImportedTiles, bool]:
        """The clipboard as tiles in this document's format, plus whether they
        arrived as a *picture* (an image, which always stamps as one) rather
        than a celPix tile payload (which follows the selection shape).

        Three ways in, in decreasing fidelity:

        1. A celPix copy of the same tile geometry whose indices fit this
           format's index space - used **verbatim**. Indices are the data; a
           copy between two spots in a ROM must move them untouched, whatever
           palette either view happens to render through.
        2. A celPix copy that doesn't fit (a 4bpp run into a 2bpp view) - its
           own palette turns the indices back into colors, which are re-matched
           into this view's subpalette.
        3. Anything else on the clipboard that is an image - the import pathway
           (:mod:`celpix.pipeline.importer`), quantized to the subpalette. This
           is the cross-application case, shared with PNG import.

        The first two carry whole tiles, so they report no partial coverage; only
        an image can stop part-way into an edge tile.
        """
        assert self._doc is not None
        target = self._import_target()
        payload = clipboard.take_payload()
        same_geometry = payload is not None and (
            payload.tile_width == self._doc.tile_width
            and payload.tile_height == self._doc.tile_height
        )
        if payload is not None and same_geometry:
            fits = payload.max_index < len(target.colors)
            if payload.direct_color == target.direct_color and (
                target.direct_color or fits
            ):
                tiles = payload.tiles()
                return ImportedTiles(tiles, payload.columns, 0, QuantizeReport()), False
            if not payload.direct_color:
                tiles, report = importer.import_indexed(
                    payload.tiles(), payload.colors, target
                )
                return ImportedTiles(tiles, payload.columns, 0, report), False
            # A direct-color copy into an indexed view: fall through to the
            # image, which the same copy also put on the clipboard.
        image = clipboard.take_image()
        if image is None:
            return ImportedTiles(), False
        # A foreign image has no tile grid of its own; import_argb cuts it in
        # reading order at its own pixel width in whole tiles.
        return importer.import_argb(clipboard.image_to_argb(image), target), True

    @staticmethod
    def _fit_note(report: QuantizeReport) -> str:
        """How faithfully an import landed, for the status line."""
        if report.source_colors == 0:
            return ""
        if report.lossless:
            return f"all {report.source_colors} colors matched exactly"
        return (
            f"{report.approximated_colors} of {report.source_colors} "
            "colors approximated"
        )
