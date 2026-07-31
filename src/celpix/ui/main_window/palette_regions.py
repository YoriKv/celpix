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

What is **stored** is that row taken back through the entry's palette row base
(:attr:`~celpix.core.document.Document.palette_row_base`), because a region's row
is a *named* row like a cell's — the file's own numbering, which the base carries
onto the palette that got loaded. Both ends of that follow: pinning lands on the
colours that were selected, and moving the base afterwards re-aims every pin at
once, which is what a bank whose per-tile rows seeded them needs.

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
from celpix.pipeline.pipeline import drawn_palette_row
from celpix.ui.undo_commands import PaletteRegionsCommand
from celpix.ui.widgets import counted, load_bool_setting, save_bool_setting

# QSettings keys for the two "show me the pins" toggles. Local preferences rather
# than project state, for the reason the grid's are (``window.py``): what the pins
# *are* is a fact about the art and belongs to the entry, but whether you want
# them drawn or numbered says how you are reading the sheet right now — and
# carrying that in the .celpix would mean opening someone else's project changed
# your view.
SHOW_PALETTE_REGIONS_KEY = "view/show_palette_regions"
SHOW_PALETTE_ROWS_KEY = "view/show_palette_rows"
# And the third, for the same reason: whether a named row the palette row base
# pushes off the end of the palette wraps round or stops short is a way of
# reading a file against the colours in hand, not a property of either.
WRAP_PALETTE_ROWS_KEY = "view/wrap_palette_rows"


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
        self._show_palette_regions = load_bool_setting(SHOW_PALETTE_REGIONS_KEY, True)
        # Whether each pinned tile is *labelled* with its row number, as
        # opposed to drawn through it. A local preference like the one above,
        # and off by default: a number over every pinned tile is a lot of ink
        # for something wanted in bursts.
        self._show_palette_rows = load_bool_setting(SHOW_PALETTE_ROWS_KEY, False)
        # Whether the palette row base wraps rather than stopping at the palette's
        # first row. Off by default: where a file's rows and the loaded palette
        # line up — the ordinary case — wrapping can only turn a base that is
        # wrong into art drawn through a plausible-looking row, where stopping
        # short leaves the mismatch on screen to be seen and corrected.
        self._wrap_palette_rows = load_bool_setting(WRAP_PALETTE_ROWS_KEY, False)
        self._build_pin_actions()

    def _palette_row_count(self) -> int:
        """How many subpalette rows the loaded palette serves at this bit depth.

        One more than the highest row anything may pin to. At 8bpp it is 1 for
        any ordinary palette, which is the honest answer rather than a special
        case: one row of 256 indices leaves nothing to pin *to*.
        """
        if self._doc is None:
            return 1
        return self._doc.palette_rows(self._index_space())

    def _max_subpalette_row(self) -> int:
        """The highest row the loaded palette can serve — the bound
        :meth:`~celpix.core.paletteregions.PaletteRegions.bounded` trims to."""
        return self._palette_row_count() - 1

    def _drawn_palette_row(self, row: int) -> int:
        """A **named** row as the palette row it is drawn through.

        The document's base applied, wrapping only where Wrap Palette Rows asks
        for it (:func:`drawn_palette_row`). Every reader of a stored row goes
        through here — the recolour, the label, the grid's mark — so a pinned
        tile and the ring pointing at its colours cannot disagree.
        """
        doc = self._doc
        if doc is None:
            return row
        return drawn_palette_row(
            row, doc.palette_row_base, doc.palette_row_wrap(self._index_space())
        )

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
        reading for a moment". Whichever way it is left is remembered app-wide
        across launches (:data:`SHOW_PALETTE_REGIONS_KEY`), not per entry: it is
        a way of reading, and the pins it shows are what belongs to the entry.

        The shortcut is set for the label it puts in the menu and the F1 guide but
        given a widget context so it never fires here: bare letters are routed by
        the app-wide event filter, which yields to focused text inputs.
        """
        # Mnemonic "h": "S" is Pin Selection's and "P" Palette from Selection's,
        # both of which share this menu.
        self._show_palette_regions_action = QAction("S&how Pinned Palette Colors", self)
        self._show_palette_regions_action.setCheckable(True)
        self._show_palette_regions_action.setChecked(self._show_palette_regions)
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
        self._show_palette_rows_action.setChecked(self._show_palette_rows)
        self._show_palette_rows_action.setToolTip(
            "Number each pinned tile with the subpalette row it uses\n"
            "Drawn in the grid's own color, in the tile's top-left corner"
        )
        self._show_palette_rows_action.toggled.connect(self._set_show_palette_rows)
        # The third switch, and the one that is not about the pins alone: it says
        # how the palette row **base** behaves at the ends of the palette, which a
        # map's cells and a sprite's parts obey as much as a pinned row does. It
        # sits with these two because it is the same kind of thing — a way of
        # reading what is loaded, remembered app-wide and written to no project.
        #
        # Mnemonic "W": free in this menu, where "S", "P", "h" and "l" are taken.
        self._wrap_palette_rows_action = QAction("&Wrap Palette Rows", self)
        self._wrap_palette_rows_action.setCheckable(True)
        self._wrap_palette_rows_action.setChecked(self._wrap_palette_rows)
        self._wrap_palette_rows_action.setToolTip(
            "Let Base Palette Row carry a row off one end of the\n"
            "palette and back on at the other\n"
            "Off, a row pushed below the first stops there"
        )
        self._wrap_palette_rows_action.toggled.connect(self._set_wrap_palette_rows)

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
        save_bool_setting(SHOW_PALETTE_ROWS_KEY, on)
        self._show_palette_rows = on
        if self._doc is not None:
            self._refresh_view()

    def _set_show_palette_regions(self, on: bool) -> None:
        save_bool_setting(SHOW_PALETTE_REGIONS_KEY, on)
        self._show_palette_regions = on
        if self._doc is not None:
            self._refresh_view()

    def _set_wrap_palette_rows(self, on: bool) -> None:
        """Turn the base's wraparound on or off, and redraw through it.

        The refresh is what lands it: the flag reaches the model in the view
        options ``_refresh_view`` captures, and every reader of a named row asks
        the document rather than this member
        (:meth:`~celpix.core.document.Document.palette_row_wrap`).
        """
        save_bool_setting(WRAP_PALETTE_ROWS_KEY, on)
        self._wrap_palette_rows = on
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

    def _tile_first_pixels(self, tiles: list[int]) -> list[int]:
        """The pixel a region lookup resolves each of ``tiles`` by.

        A tile belongs to the region holding its **first** pixel, whatever the
        walk (``docs/design/palette-editing.md`` §3), and a rearrangement is
        resolved first: a region names where the tile *lives*, so dragging it
        elsewhere takes its row with it.
        """
        assert self._doc is not None
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
        return offsets

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
        space = self._index_space()
        # None as the default, so an unpinned tile takes the view's row *without*
        # the base: the view's row is already a row of the loaded palette, and
        # only a pinned one is named relative to the file's own numbering.
        view_row = self._subpalette.value()
        rows = regions.rows_for(self._tile_first_pixels(tiles), None)
        return [
            (view_row if row is None else self._drawn_palette_row(row)) * space
            for row in rows
        ]

    def _sync_marked_palette_row(self) -> None:
        """Point the palette grid's blue ring at the selection's own row.

        Driven from three places, since each can move without the others: the
        selection pass (a different tile or cell), the palette refresh (a pin, an
        unpin, a row base, or the pinned render toggled off under a still
        selection), and a sprite object's pick, which is a selection of its own.
        """
        self._palette_panel.set_marked_row(self._selection_palette_row())

    def _selection_palette_row(self) -> int | None:
        """The palette row the selection draws through, when it names one itself.

        Three documents, three ways of naming it, one question. A **tilemap
        cell** and a **subsprite** carry a row in the file, so the row is theirs
        whatever the view is set to. On a **pixel** view nothing in the bytes
        says a row at all and the answer is a pinned region or nothing. All three
        are named rows, so all three come back through the base
        (:meth:`_drawn_palette_row`) — the mark rings the colours on screen.

        ``None`` unless the whole selection agrees on one row: a single mark
        cannot speak for a selection spanning two, and one that showed the first
        tile's row would be a quiet lie about the rest.
        """
        doc = self._doc
        if doc is None:
            return None
        if doc.is_sprite:
            # No cell grid to select in — the record under the last press is what
            # a sprite object answers with (``sprite_select.py``).
            sub = self._picked_subsprite_record()
            if sub is None:
                return None
            return self._drawn_palette_row(sub.palette_row)
        if doc.is_tilemap:
            if not doc.cells_carry_palette_rows:
                return None  # a coordinate cell has no row field to read
            rows = {
                doc.cells[at].palette_row
                for at in self._selected_cells()
                if 0 <= at < len(doc.cells)
            }
            if len(rows) != 1:
                return None
            return self._drawn_palette_row(rows.pop())
        pinned = self._selection_pinned_row()
        return None if pinned is None else self._drawn_palette_row(pinned)

    def _selection_pinned_row(self) -> int | None:
        """The row the selection is pinned to, as **stored** — the caller applies
        the base.

        ``None`` unless every selected tile answers the same row: a single mark
        cannot speak for a selection spanning two, and one that showed only the
        first tile's row would be a quiet lie about the rest. ``None`` too when
        nothing is pinned, when nothing is selected, and while the pinned render
        is toggled off — the mark says where the colours on screen come from, so
        it goes away with them.
        """
        if self._doc is None:
            return None
        regions = self._active_palette_regions()
        if regions.is_empty():
            return None
        tiles = self._selection_tiles()
        if not tiles:
            return None
        # None as the default, which is how this asks *which* tiles are pinned
        # rather than what each renders through: a tile pinned to the row the
        # view is already on is a pin like any other, and the two questions have
        # different answers exactly there.
        rows = regions.rows_for(self._tile_first_pixels(tiles), None)
        first = rows[0]
        if first is None or any(row != first for row in rows):
            return None
        return first

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
        """Pin the selection to the Subpal spinbox's current row.

        What is *stored* is that row taken back through the base, because a
        region's row is a named one and the base is applied on the way out again
        (:meth:`_drawn_palette_row`). So the colours the pin lands on are the
        colours the user had selected — under a base of 8, pinning while row 11
        is picked stores 3 and draws 11 — and re-aiming the whole entry with the
        base spin afterwards moves this pin with everything else, which is the
        point of storing it relative rather than absolute.

        The plain difference is what makes that true under **either** reading of
        the ends: it draws back through the base as the row that was picked
        whether or not the base wraps. Wrapping only normalizes it into the
        palette, so a pin made under one setting means the same thing under the
        other.
        """
        if self._doc is None or self._applying_undo:
            return
        spans = self._selection_spans()
        if not spans:
            return
        row = self._subpalette.value() - self._doc.palette_row_base
        wrap = self._doc.palette_row_wrap(self._index_space())
        if wrap:
            row %= wrap
        after = self._palette_regions.assigned(spans, row)
        if after == self._palette_regions:
            return  # already pinned there — not a history step
        shown = self._drawn_palette_row(row)
        self._push_command(
            PaletteRegionsCommand(
                self,
                self._workspace.current,
                f"pin subpalette {shown}",
                self._palette_regions,
                after,
            )
        )
        count = len(self._selection_tiles())
        self.statusBar().showMessage(
            f"Pinned {counted(count, 'tile')} to subpalette {shown}."
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
