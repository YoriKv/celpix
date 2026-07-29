"""Pinning a region of the picture to its own subpalette row.

celPix renders everything through one global subpalette row, but a ROM's tile bank
usually isn't drawn under one palette: the status bar sits at palette 0, the player
at 3, the enemies at 5, because the hardware reads the row from tilemap or OAM
attributes that never travel with the pixel data. This tool records "these pixels
render through row *n*" so the whole sheet reads at once instead of a group at a
time.

Nothing moves and nothing is written. A pinned region is display state held in
``ViewOptions`` (:mod:`celpix.core.paletteregions`) and applied at the very end of
rendering, as a shift folded into the *displayed* indices — so an edit inside a
pinned region still stores the index it always did, and the pen still means what it
means everywhere else. That asymmetry is deliberate: the row is a property of how
the game draws these bytes, not of the bytes, so baking it into an edit would be
inventing data.

**The gesture takes the row from the Subpal spinbox** rather than asking for it.
The row is already on screen and already selectable four ways — the spinbox, a
swatch click, the arrow keys, a drag across the grid — so a dialog would be a
worse way to say a thing the user has just finished saying. Set Subpal, select the
tiles, pin.

**A selection becomes pixel spans.** Bytes would not do: most retro codecs are
planar, so a byte is one bitplane row and a byte boundary lands nowhere in
particular in the picture — a byte span is only meaningful at whole-tile
granularity, and silently meaningless inside one. A pixel index is well defined for
every codec, so a span boundary is always one the user can see. It also makes the
2D wide-bitmap walk work rather than having to be locked out: there a tile owns no
contiguous pixel run either, so it contributes one span per pixel row
(:func:`~celpix.core.arrangement.tile_pixel_spans`). A **rectangle** is disjoint in
the picture even in 1D, so it is always taken tile by tile rather than as its
enclosing run; pinning a narrow rectangle must not colour everything between its
rows.

The consequence to know: a region follows the **picture**, not the data. Switching
bit depth re-cuts the bytes but leaves the tile grid alone, so a pinned region keeps
covering the art it was drawn over rather than the bytes it started on — which is
the right way round for a feature whose job is "colour what I am looking at".
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence

from celpix.core.arrangement import tile_first_pixel, tile_pixel_spans
from celpix.core.paletteregions import PaletteRegions
from celpix.ui.undo_commands import PaletteRegionsCommand
from celpix.ui.widgets import counted


class PaletteRegionsMixin:
    """Pinned palette regions, a slice of the main window.

    Owns the live :class:`~celpix.core.paletteregions.PaletteRegions`, the Palette
    menu's view toggle, and the gestures that pin and unpin.
    """

    # -- state -------------------------------------------------------------
    def _init_palette_regions(self) -> None:
        """Seed the pinned-region state. Called from the window's constructor.

        The two gestures are built here rather than with the menu that shows
        them: the transform bar carries them as buttons and is assembled before
        any menu exists, so they have to be there first.
        """
        self._palette_regions = PaletteRegions()
        self._show_palette_regions = True
        # Whether each pinned tile is *labelled* with its row number, as
        # opposed to drawn through it. A local preference, not project
        # state: it says how you want to read the pins, not what they are.
        self._show_palette_rows = False
        self._build_pin_actions()

    def _max_subpalette_row(self) -> int:
        """The highest row the loaded palette can serve at this bit depth.

        A pinned row past the palette would render the magenta missing-colour
        sentinel, and the index shift it implies could run past 255 — so this is
        both the clamp for the gesture and the bound
        :meth:`~celpix.core.paletteregions.PaletteRegions.bounded` trims to. At
        8bpp it is 0 for any ordinary palette, which is the honest answer rather
        than a special case: one row of 256 indices leaves nothing to pin *to*.
        """
        if self._doc is None:
            return 0
        space = self._index_space()
        by_palette = (max(0, len(self._doc.palette) - 1)) // space
        # The shift has to keep every index inside a byte, or a pinned tile could
        # not be expressed in the Indexed8 image at all.
        return min(by_palette, 256 // space - 1)

    def _active_palette_regions(self) -> PaletteRegions:
        """The regions in force for rendering — empty while the toggle is off.

        Bounded on every read rather than on assignment, because what makes a
        region unrenderable is the state *around* it: a shorter palette after a
        source switch, a re-read that truncated the data. The stored set is left
        alone so restoring the palette brings the regions back, exactly as the
        rearrangement survives a pattern that makes it inert.
        """
        if self._doc is None or not self._show_palette_regions:
            return PaletteRegions()
        if self._palette_regions.is_empty():
            return self._palette_regions
        doc = self._doc
        return self._palette_regions.bounded(
            doc.tile_count * doc.tile_width * doc.tile_height,
            self._max_subpalette_row(),
        )

    # -- the view toggle ---------------------------------------------------
    def _build_show_regions_action(self) -> None:
        """The view toggle, for the Palette menu beside the gestures it shows.

        On by default: pinning is only worth doing if the result is what you see,
        so the *off* state is the deliberate one — "show me the file's own
        reading for a moment". A per-entry setting all the same, restored with
        the rest of an entry's session.

        The shortcut is set for the label it puts in the menu and the F1 guide but
        given a widget context so it never fires here: bare letters are routed by
        the app-wide event filter, which yields to focused text inputs.
        """
        # Mnemonic "h": "S" is Pin Selection's and "P" Palette from Selection's,
        # both of which share this menu.
        self._show_palette_regions_action = QAction("S&how Pinned Palette Colors", self)
        self._show_palette_regions_action.setCheckable(True)
        self._show_palette_regions_action.setChecked(True)
        self._show_palette_regions_action.setShortcut(QKeySequence("Shift+P"))
        self._show_palette_regions_action.setShortcutContext(
            Qt.ShortcutContext.WidgetShortcut
        )
        self._show_palette_regions_action.setToolTip(
            "Draw pinned regions through their own rows (Shift+P)\n"
            "Off draws the whole view in one subpalette"
        )
        self._show_palette_regions_action.toggled.connect(
            self._set_show_palette_regions
        )
        # The rows, as a separate switch from the colors. Seeing a tile drawn
        # through row 5 does not tell you it *is* row 5 — several rows often
        # share their first colors, and a bank seeded from a file's own table
        # (`session._seed_tile_palette_rows`) can carry dozens of them. So the
        # number can be shown without the recolor, and either without the other.
        self._show_palette_rows_action = QAction("Show Pinned Palette &Rows", self)
        self._show_palette_rows_action.setCheckable(True)
        self._show_palette_rows_action.setChecked(False)
        self._show_palette_rows_action.setToolTip(
            "Number each pinned tile with the subpalette row it uses\n"
            "Drawn in the grid's own color, in the tile's top-left corner"
        )
        self._show_palette_rows_action.toggled.connect(self._set_show_palette_rows)

    def _build_pin_actions(self) -> None:
        """The pin gestures, shared by the transform bar and two menus.

        Two act on the selection; Unpin All acts on the document, which is why it
        stays live with nothing selected — reaching every region by selecting it
        is exactly the work it exists to save.

        The first two also sit on the transform bar, which is text-only and has
        no room for a menu row's worth of words: ``iconText`` is what a
        QToolButton labels itself with, so it carries the short form while
        ``text`` stays the sentence the menus need.
        """
        # Mnemonic "S": "P" belongs to Palette from Selection beside it, and "N"
        # to New Slice, which shares the canvas menu with both.
        self._pin_palette_action = QAction("Pin &Selection to Subpalette", self)
        self._pin_palette_action.setIconText("Pin")
        self._pin_palette_action.setToolTip(
            "Render the selected tiles through the current\n"
            "Subpal row, whatever the view is set to"
        )
        self._pin_palette_action.triggered.connect(self._pin_selection)
        self._unpin_palette_action = QAction("&Unpin Selection", self)
        self._unpin_palette_action.setIconText("Unpin")
        self._unpin_palette_action.setToolTip(
            "Return the selected tiles to the view's own subpalette"
        )
        self._unpin_palette_action.triggered.connect(self._unpin_selection)
        # Mnemonic "l": "a" is Paste's in the canvas menu these three also sit in.
        self._unpin_all_action = QAction("Unpin A&ll", self)
        self._unpin_all_action.setToolTip(
            "Drop every pinned region, returning the whole\n"
            "picture to the view's own subpalette"
        )
        self._unpin_all_action.triggered.connect(self._unpin_all)

    def _toggle_show_palette_regions(self) -> None:
        """``Shift+P`` — via the action, so key and button can't diverge."""
        if self._show_palette_regions_action.isEnabled():
            self._show_palette_regions_action.toggle()

    def _set_show_palette_rows(self, on: bool) -> None:
        self._show_palette_rows = on
        if self._doc is not None:
            self._refresh_view()

    def _set_show_palette_regions(self, on: bool) -> None:
        self._show_palette_regions = on
        if self._doc is not None:
            self._refresh_view()

    def _sync_pin_actions(self) -> None:
        """Enable the gestures only when there is a selection to pin.

        Unpin All is the exception: it needs pinned regions, not a selection.
        """
        has_selection = self._doc is not None and bool(self._selection_tiles())
        pinned = self._doc is not None and not self._palette_regions.is_empty()
        self._pin_palette_action.setEnabled(has_selection)
        self._unpin_palette_action.setEnabled(has_selection and pinned)
        self._unpin_all_action.setEnabled(pinned)

    # -- the gestures ------------------------------------------------------
    def _selection_spans(self) -> list[tuple[int, int]]:
        """The selected tiles as pixel spans in the document's pixel space.

        Tile by tile rather than as the selection's enclosing run: a rectangle is
        disjoint in the picture, and under the 2D walk even a single tile is. Each
        tile is resolved through the arrangement that is actually in force, so the
        spans name the pixels the user is looking at whatever the walk — and the
        rearrangement is resolved first, so pinning a tile that has been dragged
        elsewhere pins the tile, not the slot.
        """
        assert self._doc is not None
        doc = self._doc
        view_2d = self._two_d.isChecked()
        cols = self._columns.value()
        tile_rearrangement = self._active_tile_rearrangement()
        per_tile = doc.tile_width * doc.tile_height
        origin = self._offset * per_tile
        spans: list[tuple[int, int]] = []
        for tile in self._selection_tiles():
            if tile_rearrangement.is_identity():
                # Slot-relative, because that is what the 2D bitmap space is
                # anchored on; in 1D the two agree and this is just tile * area.
                for start, length in tile_pixel_spans(
                    tile - self._offset,
                    doc.tile_width,
                    doc.tile_height,
                    cols,
                    view_2d,
                ):
                    spans.append((origin + start, length))
                continue
            spans.append((tile_rearrangement.actual(tile) * per_tile, per_tile))
        return spans

    def _tile_biases(self, tiles: list[int]) -> list[int] | None:
        """Index shifts for an arbitrary list of tiles, or None if none is pinned.

        What the clipboard image renders through, so a copied region leaves in the
        colours it was shown in. Takes the tiles it is given rather than a window,
        because a copy carries the *selection* — which for a rectangle is not a
        run — and must stay in step with it one for one.
        """
        assert self._doc is not None
        regions = self._active_palette_regions()
        if regions.is_empty():
            return None
        doc = self._doc
        cols = self._columns.value()
        view_2d = self._two_d.isChecked()
        tile_rearrangement = self._active_tile_rearrangement()
        per_tile = doc.tile_width * doc.tile_height
        origin = self._offset * per_tile
        offsets = []
        for tile in tiles:
            if tile_rearrangement.is_identity():
                offsets.append(
                    origin
                    + tile_first_pixel(
                        tile - self._offset,
                        doc.tile_width,
                        doc.tile_height,
                        cols,
                        view_2d,
                    )
                )
                continue
            offsets.append(tile_rearrangement.actual(tile) * per_tile)
        space = self._index_space()
        rows = regions.rows_for(offsets, self._subpalette.value())
        return [row * space for row in rows]

    def _pinned_palette_base(self, x: int, y: int) -> int:
        """The palette base the pixel at window position ``(x, y)`` is *shown* through.

        What the eyedropper has to sample against. The window grid it reads holds
        the stored index, which is deliberately unbiased — pinning never changes
        what an edit stores — so resolving that index against the view's own
        subpalette would name the swatch the pixel *would* have had, not the one
        the user just clicked on. Inside a pinned region those are different
        colours, which is the whole point of the region.

        Falls back to the view's base wherever nothing is pinned, or when the
        position lands in a block-layout gap or past the last tile.
        """
        assert self._doc is not None
        if self._active_palette_regions().is_empty():
            return self._palette_base()
        tile_w, tile_h = self._pixel_tile_size()
        if tile_w <= 0 or tile_h <= 0:
            return self._palette_base()
        tile = self._cell_tile(self._view_layout(), x // tile_w, y // tile_h)
        if tile is None:
            return self._palette_base()
        biases = self._tile_biases([tile])
        return self._palette_base() if biases is None else biases[0]

    def _pin_selection(self) -> None:
        """Pin the selection to the Subpal spinbox's current row."""
        if self._doc is None or self._applying_undo:
            return
        spans = self._selection_spans()
        if not spans:
            return
        row = min(self._subpalette.value(), self._max_subpalette_row())
        after = self._palette_regions.assigned(spans, row)
        if after == self._palette_regions:
            return  # already pinned there — not a history step
        self._push_command(
            PaletteRegionsCommand(
                self,
                self._workspace.current,
                f"pin subpalette {row}",
                self._palette_regions,
                after,
            )
        )
        count = len(self._selection_tiles())
        self.statusBar().showMessage(
            f"Pinned {counted(count, 'tile')} to subpalette {row}."
        )

    def _unpin_selection(self) -> None:
        """Return the selection to the view's own subpalette row."""
        if self._doc is None or self._applying_undo:
            return
        spans = self._selection_spans()
        if not spans:
            return
        after = self._palette_regions.cleared(spans)
        if after == self._palette_regions:
            return
        self._push_command(
            PaletteRegionsCommand(
                self,
                self._workspace.current,
                "unpin subpalette",
                self._palette_regions,
                after,
            )
        )
        count = len(self._selection_tiles())
        self.statusBar().showMessage(f"Unpinned {counted(count, 'tile')}.")

    def _unpin_all(self) -> None:
        """Drop every pinned region at once.

        The undoable escape hatch from a sheet that has been pinned into a mess:
        unpinning by selection cannot reach a region the user can no longer find.
        """
        if self._doc is None or self._applying_undo:
            return
        if self._palette_regions.is_empty():
            return
        self._push_command(
            PaletteRegionsCommand(
                self,
                self._workspace.current,
                "unpin all subpalettes",
                self._palette_regions,
                PaletteRegions(),
            )
        )
        self.statusBar().showMessage("Unpinned every region.")

    def _set_palette_regions(self, regions: PaletteRegions) -> None:
        """Land a set of regions — :class:`PaletteRegionsCommand`'s apply, and the
        restore path. Rebuilds the view, since it changes how slots are coloured."""
        self._palette_regions = regions
        # Unpin All hangs off whether anything is pinned rather than off the
        # selection, so this is the only path that can arm or disarm it.
        self._sync_pin_actions()
        if self._doc is not None:
            self._refresh_view()
