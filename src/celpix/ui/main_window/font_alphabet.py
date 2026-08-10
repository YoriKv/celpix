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
- **A run of typing is one step.** The window settles a cell at a time and
  reports every one, so the sheet and the string follow the caret — but the undo
  stack must not fill with letters. The run number is kept here, handed to each
  command, and bumped whenever the window says the run has ended.
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
from celpix.ui.widgets import Badge, signals_blocked


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
        the codes index into. Over a **pixels entry** that has been ticked
        **Use as Font** it is that entry itself, which is how a sheet is typed up
        before any string has been carved out to test it against.

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
            return bound
        return entry if entry.use_as_font else None

    def _font_alphabet_available(self) -> bool:
        """Whether there is a font to edit the alphabet of.

        A fontmap with nothing bound answers False and says so on the text
        window's own badge instead: the alphabet has no entry to be written to
        until the Tiles combo is set, and an editor over no font would be a table
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
            font.font_chars,
            font.font_codes,
        )
        self._font_alphabet.set_status(*self._font_alphabet_status(font))

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

    def _font_alphabet_status(self, font: Entry) -> tuple[str, Badge | None]:
        """The readout: how much of the sheet has been spelled, and whose it is."""
        named = len(font.font_codes)
        spelled = sum(1 for char in font.font_chars if char != HOLE)
        summary = f"{font.name}: {spelled} characters, {named} named codes"
        if not font.use_as_font:
            return summary, Badge(
                "not a font",
                "This entry is not ticked Use as Font, so its\n"
                "table is not read. Tick it beside the tile size\n"
                "to put these letters into the text.",
                warning=True,
            )
        return summary, None

    def _on_font_alphabet_tile(self, tile_id: int) -> None:
        """A tile picked in the editor marks the same tile in the dock.

        One way only. The dock's pick drives the canvas and the Base tile
        readout, and a click in the editor is about which *code* is being typed
        rather than about which tile the map should use — so it points at the
        tile and stops there.
        """
        self._tile_source_panel.set_marked_id(tile_id)

    # -- taking an edit back -----------------------------------------------
    def _on_font_alphabet_edited(
        self, base: int, chars: str, codes: tuple, fresh: bool, label: str
    ) -> None:
        """One edit from the window, as an undo step on the bound font."""
        font = self._font_entry()
        if font is None or self._applying_undo:
            return
        before: FontAlphabetState = (
            font.use_as_font,
            font.font_base,
            font.font_chars,
            font.font_codes,
        )
        after: FontAlphabetState = (font.use_as_font, base, chars, tuple(codes))
        if after == before:
            return
        # A fresh gesture ends the run before it starts a step of its own, so a
        # paste can never merge into the word typed just before it.
        if fresh:
            self._font_alphabet_run += 1
        self._push_command(
            FontAlphabetCommand(
                self,
                font,
                label,
                before,
                after,
                run=None if fresh else self._font_alphabet_run,
            )
        )

    # -- the Use as Font tick ----------------------------------------------
    def _sync_use_as_font(self) -> None:
        """Put the tick where the current entry left it, and only on a sheet.

        Hidden rather than disabled off a pixels entry, and hidden by hand rather
        than through a capability: the view toolbar it sits on is shown for every
        kind of document, and this is the one control on it that says something
        about *tiles* rather than about how many of them to show at once. A map's
        cells are not letters and a palette has no tiles at all, so on both there
        is no question here to answer.
        """
        entry = self._workspace.current
        self._use_as_font.setVisible(
            entry is not None and entry.content_kind is ContentKind.PIXELS
        )
        with signals_blocked(self._use_as_font):
            self._use_as_font.setChecked(entry is not None and entry.use_as_font)

    def _on_use_as_font_change(self, on: bool) -> None:
        """Declare (or stop declaring) that this sheet's tiles are letters.

        The table is deliberately **kept** when the tick comes off. It is the
        user's own work and there is no reading of unticking a box that means
        "and throw that away"; what the tick decides is whether it is read.
        """
        entry = self._workspace.current
        if entry is None or self._applying_undo or entry.use_as_font == on:
            return
        before: FontAlphabetState = (
            entry.use_as_font,
            entry.font_base,
            entry.font_chars,
            entry.font_codes,
        )
        after: FontAlphabetState = (on, *before[1:])
        text = (
            f"use {entry.name} as a font"
            if on
            else f"stop using {entry.name} as a font"
        )
        self._push_command(FontAlphabetCommand(self, entry, text, before, after))

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
        font.use_as_font, font.font_base, font.font_chars, font.font_codes = state
        for entry in self._workspace.entries:
            doc = entry.doc
            if doc is None or not doc.is_fontmap:
                continue
            if self._binding_target(entry.tile_source) is not font:
                continue
            doc.font_alphabet = pipeline.load_font_alphabet(
                font.font_chars if font.use_as_font else "",
                font.font_codes if font.use_as_font else (),
                doc.pixel_ctx,
                controls=self._tilemap_declares(entry, "controls") or (),
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
        self._canvas.set_line_ends(self._line_end_slots())
        self._refresh_text()
        self._refresh_font_alphabet(sheet=False)
        self._refresh_project_modified()


# How many tiles across the editor's sheet is laid out. Fixed rather than
# following the dock's Cols spin, because the row a code sits on is the only
# thing tying the picture to the table, and 16 is what makes a hex code readable
# off the grid: a row is one high nibble.
FONT_SHEET_COLUMNS = 16
