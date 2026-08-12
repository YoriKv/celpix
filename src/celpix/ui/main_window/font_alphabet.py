"""The Font Alphabet window's window side: what it is handed, and what comes back.

The window is presentation only (:mod:`celpix.ui.font_alphabet_window`) — it
holds an origin, a run of characters and a list of named codes, and never reads
the model. This is the half that finds the font behind whatever is on screen,
draws its sheet, and turns an edited alphabet back into project state.

Four things it has to get right, and none of them is the window's to know:

- **Which entry it is editing.** A fontmap's alphabet is not the fontmap's: it
  belongs to the **pixels entry supplying the tiles**, and is reached through the
  binding (``docs/design/fontmap-entry.md`` §3). So the window opened over a
  string writes to a different entry than the one on screen, and the title says
  whose it is.
- **Every fontmap through that font is re-read.** The alphabet is the font's, so
  a second string bound to the same sheet is just as wrong until it picks the
  change up, and it is the one on screen that would otherwise be the only one
  right — the rule the binding follows when an entry leaves the list
  (``docs/design/tilemap-entry.md`` §1). Nothing about the bytes moves, so this
  is a re-read of the *reading* and not of the document.
- **One reported edit is one undo step.** The window settles a row at a time and
  reports every one, so the sheet and the string follow the caret; each is its
  own step, because a row is a code's whole answer and two rows are two things to
  take back. A gesture that writes many rows at once — a fill-down, a template, a
  shift — reports the finished table once and is therefore already one step.
- **A fontmap opens its alphabet by itself.** The canvas shows a string as the
  grid of glyph tiles it is and the text window shows what those tiles say; this
  is where the second is decided. Landing on a fontmap whose font has no table
  and being shown hex is the state it exists to explain, so it is gated on the
  declaration and not on there being anything to see. Closing it turns that off
  for the session; View ▸ Font Alphabet turns it back on
  (:meth:`_sync_font_alphabet`).
"""

from __future__ import annotations

from celpix.core.arrangement import BlockLayout
from celpix.core.capabilities import ContentKind
from celpix.core.font import HOLE
from celpix.pipeline import pipeline
from celpix.project.workspace import Entry
from celpix.ui import render_bridge
from celpix.ui.undo_commands import FontAlphabetCommand, FontAlphabetState
from celpix.ui.widgets import signals_blocked


class FontAlphabetMixin:
    """Building, gating and committing the font alphabet window.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads the window's own widgets and its single live
    ``_doc``. See the package docstring for why these are mixins.
    """

    # -- which entry, and whether there is one -----------------------------
    def _font_entry(self) -> Entry | None:
        """The entry whose alphabet is on show — the bound font, or this sheet.

        Two ways in, because there are two moments the question comes up. Over a
        **fontmap** it is whatever the Tiles combo names, since that is the sheet
        the codes index into. Over a **pixels entry** it is that entry itself,
        which is how a sheet is typed up before any string has been carved out to
        test it against.

        Both ways ask the same question of the sheet — **Use as Font**
        (:attr:`~celpix.project.workspace.Entry.use_as_font`) — so an editor is
        only ever opened on a table that is actually read. A fontmap bound to a
        sheet that is not declared one has no alphabet to edit: typing into it
        would write a table nothing looks at, and the text window says so on its
        own badge instead.

        None where neither holds, which is most entries.
        """
        entry = self._workspace.current
        if entry is None:
            return None
        doc = self._doc
        if doc is not None and doc.is_fontmap:
            bound = (
                self._binding_target(entry.tile_source) if entry.tile_source else None
            )
            return bound if bound is not None and bound.is_font_sheet else None
        return entry if entry.is_font_sheet else None

    def _font_alphabet_available(self) -> bool:
        """Whether there is a font to edit the alphabet of.

        A fontmap with nothing bound — or bound to a sheet not ticked **Use as
        Font** — answers False and says so on the text window's own badge
        instead: the alphabet has no entry to be written to until the Tiles combo
        names a declared font, and an editor over anything else would be a table
        that goes nowhere.
        """
        return self._font_entry() is not None

    # -- showing it --------------------------------------------------------
    def _show_font_alphabet(self) -> None:
        """View ▸ Font Alphabet — open the window, and mean it.

        Asking for it is also what puts the automatic opening back: the only way
        to have turned it off is to have closed the window, so the only sensible
        reading of opening it again is that you want it.
        """
        self._font_alphabet_dismissed = False
        self._refresh_font_alphabet()

    def _sync_font_alphabet(self) -> None:
        """Open the window on a font, and close it on anything else."""
        if not self._font_alphabet_available():
            self._font_alphabet.hide_overlay()
            return
        if not self._font_alphabet.isVisible() and self._font_alphabet_dismissed:
            return
        self._refresh_font_alphabet()

    def _on_font_alphabet_dismissed(self) -> None:
        """The user shut the window: stop opening it for them until they ask."""
        self._font_alphabet_dismissed = True

    def _refresh_font_alphabet(self, *, sheet: bool = True) -> None:
        """Hand the font's table over, showing the window.

        ``sheet=False`` skips redrawing the tiles, and is what an **edit** takes.
        Composing the sheet is the expensive half of this — a bank of tiles
        decoded, laid out and rasterized — and not one pixel of it moves when a
        character is typed into the table beside it. Only the document changing
        can change the picture, and that route passes ``sheet=True``.
        """
        font = self._font_entry()
        if font is None or self._doc is None:
            self._font_alphabet.hide_overlay()
            return
        if sheet:
            drawn = self._font_sheet()
            if drawn is None:
                self._font_alphabet.hide_overlay()
                return
            image, ids, cell_px, columns = drawn
            self._font_alphabet.set_sheet(image, ids, cell_px, columns)
        self._font_alphabet.show_alphabet(
            font.name,
            font.font_base,
            font.font_prepend,
            font.font_append,
            font.font_chars,
            font.font_codes,
        )
        self._font_alphabet.set_status(self._font_alphabet_status(font))
        if sheet:
            # Only where the sheet was rebuilt, which is the document changing or
            # the window opening. An **edit** must not land here: it would answer
            # every keystroke by dragging the selection back to whatever the
            # canvas points at, and typing down the table would stick on one row.
            self._sync_font_alphabet_row()

    def _font_sheet(self):  # noqa: ANN201 — (QImage, list[int], (int, int), int) | None
        """The font's tiles as one picture, in slot order.

        Two routes, because the two entry points are two different documents.
        Over a **fontmap**, :func:`~celpix.pipeline.pipeline.tile_source_image`
        reaches through the binding and hands back the bank the cells index into,
        already offset by the binding's base tile — the tile source dock's own
        picture, so what the editor shows and what the map draws cannot drift.
        Over the **font sheet itself** there is no binding and no cells: the tiles
        are the document, so they are decoded and laid out directly. Either way
        slot *n* is the tile code ``base + n`` draws, which is the whole premise
        of the top half.

        The colour table is never offset in either. The row is folded into the
        indices upstream — by :func:`~celpix.pipeline.pipeline.expand_cells` for
        the map's synthetic cells, and by the ``base`` handed to the composer
        here — so shifting the table too would draw the bank in row 2n.
        """
        doc = self._doc
        if doc is None:
            return None
        if doc.is_fontmap:
            row = self._tile_source_row()
            source = pipeline.tile_source_image(
                doc, self._registry, FONT_SHEET_COLUMNS, self._cell_index_limit(), row
            )
            if not source.ids:
                return None
            across, down = doc.stamp_tiles
            return (
                render_bridge.render_pinned(source.grid, doc.palette),
                list(source.ids),
                (across * doc.tile_width, down * doc.tile_height),
                FONT_SHEET_COLUMNS,
            )
        if not doc.tile_count:
            return None
        engine, preset = self._registry.engine_for(doc.pixel_config.interpret_preset_id)
        # Every tile, one per slot, in the file's own order — the sheet is what
        # the *codes* index into, so it is not the view's window and takes none of
        # its arrangement: a block grouping or a 2D read would place the tiles
        # somewhere other than where the codes count them.
        image, _filled = self._render_arrangement(
            doc.window_bytes(0, doc.tile_count),
            engine,
            pipeline.tile_params(doc, engine, preset.params),
            BlockLayout(FONT_SHEET_COLUMNS, 1, 1, "row"),
            False,
            max_rows=None,
        )
        return (
            image,
            list(range(doc.tile_count)),
            (doc.tile_width, doc.tile_height),
            FONT_SHEET_COLUMNS,
        )

    def _font_alphabet_status(self, font: Entry) -> str:
        """The readout: how much of the sheet has been spelled, and whose it is.

        No badge of its own. The window is only ever open on a sheet ticked
        **Use as Font** (:meth:`_font_entry`), so everything it shows is read —
        and a table that is *not* read is reported where the codes are, on the
        text window.
        """
        named = len(font.font_codes)
        spelled = sum(1 for char in font.font_chars if char != HOLE)
        return f"{font.name}: {spelled} characters, {named} named codes"

    def _on_font_alphabet_tile(self, tile_id: int) -> None:
        """A tile picked in the editor marks the same tile in the dock.

        One way only. The dock's pick drives the canvas and the Base tile
        readout, and a click in the editor is about which *code* is being typed
        rather than about which tile the map should use — so it points at the
        tile and stops there.
        """
        self._tile_source_panel.set_marked_id(tile_id)

    def _sync_font_alphabet_row(self) -> None:
        """Point the editor at the tile the canvas's selected cell draws.

        The other direction of :meth:`_on_font_alphabet_tile`, and the one the
        canvas starts: clicking a glyph in the string is asking what that code
        says, and the row is where the answer is typed. So the editor follows the
        selection the way the tile source dock's ring does — by the **row
        selection** the user would have made by hand, not a mark of its own, so
        that Enter opens the cell they just pointed at.

        The cell's own index, before the binding's base tile: the same number the
        dock rings and the Cell spin holds, and the number the editor's sheet is
        laid out by (:meth:`_font_sheet`).

        Fontmaps only. Over the font sheet itself the canvas selects tiles of a
        window into the file rather than cells naming codes, and there is no
        second reading of them to follow.
        """
        if not self._font_alphabet.isVisible():
            return
        doc = self._doc
        if doc is None or not doc.is_fontmap or doc.cells is None:
            return
        cells = self._selected_cells()
        if cells:
            self._font_alphabet.select_tile(doc.cells[cells[0]].index)

    # -- taking an edit back -----------------------------------------------
    @staticmethod
    def _font_state(entry: Entry) -> FontAlphabetState:
        """``entry``'s whole alphabet as one value — what a command carries.

        Read in one place so the tuple's shape is stated once: every caller here
        builds a *before* out of it, and the apply path unpacks the same order.
        """
        return (
            entry.use_as_font,
            entry.font_base,
            entry.font_prepend,
            entry.font_append,
            entry.font_chars,
            entry.font_codes,
        )

    def _on_font_alphabet_edited(
        self,
        base: int,
        prepend: int,
        append: int,
        chars: str,
        codes: tuple,
        label: str,
    ) -> None:
        """One edit from the window, as an undo step on the bound font."""
        font = self._font_entry()
        if font is None or self._applying_undo:
            return
        before = self._font_state(font)
        after: FontAlphabetState = (
            font.use_as_font,
            base,
            prepend,
            append,
            chars,
            tuple(codes),
        )
        if after == before:
            return
        # One report, one step. The window reports a gesture rather than a
        # keystroke — a fill-down or a template arrives as the whole table it
        # landed — so nothing here has to be joined back together.
        self._push_command(FontAlphabetCommand(self, font, label, before, after))

    # -- the Use as Font tick ----------------------------------------------
    def _sync_use_as_font(self) -> None:
        """Put the tick where the current entry left it, and only on a sheet.

        Hidden rather than disabled off a pixels entry, and hidden by hand rather
        than through a capability: the view toolbar it sits on is shown for every
        kind of document, and this is the one control on it that says something
        about *tiles* rather than about how many of them to show at once. A map's
        cells are not letters and a palette has no tiles at all, so on both there
        is no question here to answer — a **fontmap** least of all, which reads
        its cells *through* a font and cannot also be one
        (:attr:`~celpix.project.workspace.Entry.is_font_sheet`).

        The tick follows that same answer rather than the raw flag, so a map
        carrying a stale one from an older project shows nothing to untick.

        Hidden by its **toolbar action**, which is the only handle that holds: a
        toolbar wraps a widget in a QWidgetAction and shows the widget again on
        its next re-layout, so hiding the checkbox itself lasts until the first
        window resize (``docs/py-qt-reference/pyside6-pitfalls.md``).
        """
        entry = self._workspace.current
        self._use_as_font_action.setVisible(
            entry is not None and entry.content_kind is ContentKind.PIXELS
        )
        with signals_blocked(self._use_as_font):
            self._use_as_font.setChecked(entry is not None and entry.is_font_sheet)

    def _on_use_as_font_change(self, on: bool) -> None:
        """Declare (or stop declaring) that this sheet's tiles are letters.

        Unticking **discards the table**, which is why it asks first: the tick is
        the entry's whole answer to "are these letters", and a sheet that is not
        a font has no use for an origin, a run and a list of named codes. Keeping
        them would leave the project carrying a table nothing reads and nothing
        shows — the editor is gated on the same tick (:meth:`_font_entry`), so
        there would be no way to see what was being kept.

        Cancelling puts the tick back and pushes nothing. Going ahead is one undo
        step carrying all four fields, so Ctrl+Z brings the table back whole.
        """
        entry = self._workspace.current
        if entry is None or self._applying_undo or entry.use_as_font == on:
            return
        if on:
            self._declare_use_as_font(entry)
            return
        before = self._font_state(entry)
        if self._has_alphabet(entry) and not self._confirm(
            f"{entry.name} has an alphabet typed against it. Unticking Use as "
            "Font deletes it.\n\nUndo brings it back.",
            title="celPix - stop using as a font",
            accept="Delete alphabet",
            warn=True,
        ):
            self._sync_use_as_font()  # put the tick back; nothing was declared
            return
        self._push_command(
            FontAlphabetCommand(
                self,
                entry,
                f"stop using {entry.name} as a font",
                before,
                (False, 0, 0, 0, "", ()),
            )
        )

    @staticmethod
    def _has_alphabet(entry: Entry) -> bool:
        """Whether ``entry`` holds font data an untick would throw away.

        The origin counts. A base with no run is a spin the user dialled against
        the sheet, and losing it silently is the same surprise as losing a
        letter — while a sheet that has been ticked and never typed on has
        nothing to ask about, and asking anyway would train the answer out of
        them.
        """
        return bool(entry.font_chars or entry.font_codes or entry.font_base)

    def _declare_use_as_font(self, entry: Entry) -> None:
        """Tick **Use as Font** on ``entry``, as its own undo step.

        Reached from the tick itself and from binding a fontmap to a sheet that
        is not declared one yet (:meth:`~...tilemap_bar.TilemapBarMixin.
        _on_tile_binding_change`), so both routes land the same state by the same
        command and an undo takes the declaration back either way.
        """
        before = self._font_state(entry)
        after: FontAlphabetState = (True, *before[1:])
        self._push_command(
            FontAlphabetCommand(
                self, entry, f"use {entry.name} as a font", before, after
            )
        )

    # -- the one apply path ------------------------------------------------
    def _apply_font_alphabet(self, font: Entry, state: FontAlphabetState) -> None:
        """Land an alphabet on ``font`` — both command directions, and the tick.

        Every **open fontmap drawn through** ``font`` is re-read for it, not only
        the one on screen: the alphabet is the font's, so a second string bound to
        the same sheet is just as wrong until it picks the change up.

        Nothing here touches bytes, so no document is reloaded — an alphabet is a
        reading of cells that are already decoded, and what changes is only what
        they are read as.
        """
        (
            font.use_as_font,
            font.font_base,
            font.font_prepend,
            font.font_append,
            font.font_chars,
            font.font_codes,
        ) = state
        for entry in self._workspace.entries:
            doc = entry.doc
            if doc is None or not doc.is_fontmap:
                continue
            # An open fontmap with nothing bound is an ordinary state — a string
            # carved out before its sheet was found — and it draws no font, so it
            # is skipped rather than resolved.
            source = entry.tile_source
            if source is None or self._binding_target(source) is not font:
                continue
            doc.font_alphabet = pipeline.load_font_alphabet(
                font.font_chars if font.use_as_font else "",
                font.font_codes if font.use_as_font else (),
                code_digits=max(1, doc.cell_bytes) * 2,
                base=font.font_base,
                flag_break=self._tilemap_flag_break(entry),
            )
        # **Not** ``_refresh_view``, which is the whole window and, on a tilemap,
        # a recompose of the entire map. Nothing here can move a pixel of it: an
        # alphabet is a reading of cells that are already decoded, so the picture,
        # the geometry and the bytes are all exactly as they were. What does
        # change is what the string says, where its lines end, and the table
        # itself — so those three are refreshed by name and nothing else runs.
        # Doing it the other way put a full map recompose behind every keystroke.
        self._sync_use_as_font()
        # The tick decides whether there is an alphabet to edit at all, so the
        # menu item follows it here rather than waiting for the next full refresh
        # — an untick closes the window, and leaving View ▸ Font Alphabet enabled
        # would offer to open one over nothing.
        self._font_alphabet_action.setEnabled(self._font_alphabet_available())
        self._canvas.set_line_ends(self._line_end_slots())
        self._refresh_text()
        self._refresh_font_alphabet(sheet=False)
        self._refresh_project_modified()


# How many tiles across the editor's sheet is laid out. Fixed rather than
# following the dock's Cols spin, because the row a code sits on is the only
# thing tying the picture to the table, and 16 is what makes a hex code readable
# off the grid: a row is one high nibble.
FONT_SHEET_COLUMNS = 16
