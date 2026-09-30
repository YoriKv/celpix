"""The view's arrangement: Pattern, Block W×H, Order, 2D and the bitmap width.

How decoded tiles are laid out on the canvas — a display axis, not a decode
one, except where a **bitmap width** re-cuts the codec's tiles. One undo step
per move (:class:`~celpix.ui.undo_commands.ArrangementCommand`): a Pattern pick
moves four controls and withdraws the width at once, so the whole state is
captured before and after rather than per widget.

A slice of :class:`~celpix.ui.main_window.window.MainWindow`. The controls it
drives are built by :meth:`~...interpretation.InterpretationMixin._build_toolbar`
— ``_pattern``, ``_block_cols``, ``_block_rows``, ``_block_order``, ``_two_d``,
``_bitmap_width`` and ``_columns`` — which also seeds ``_pattern_choice`` (the
Pattern selection as of the last settle, since the picker has already moved by
the time a gesture reads it). ``_columns_before_bitmap`` (the Cols value a
bitmap width displaced) is created in ``MainWindow.__init__`` and reset by a
session restore.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import QWidget

from celpix.core.arrangement import ArrangementPreset, arrangement_preset_for
from celpix.ui.undo_commands import ArrangementCommand, ArrangementState
from celpix.ui.widgets import select_combo_data, signals_blocked


class ArrangementMixin:
    """The arrangement controls' push/apply cycle and the bitmap-width settle.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object; see the module docstring for the widgets it reads.
    """

    @property
    def _arrangement_controls(self) -> tuple[QWidget, ...]:
        """The individual block/order/2D widgets a Pattern preset drives.

        Exactly the four axes a preset states — the bitmap width is not one of
        them (no preset carries a width), so it is not locked away with these;
        :meth:`_settle_bitmap_width_and_columns` gates it on its own condition instead.
        """
        return (
            self._block_cols,
            self._block_rows,
            self._block_order,
            self._two_d,
        )

    def _apply_pattern_lock(self) -> None:
        """Enable the individual arrangement controls only under Custom; a named
        preset owns them, so they're read-only while one is selected.

        Locked is not inert — every one of these still drives the view while a
        preset holds it; the lock only says the preset is the thing choosing.
        The width is gated separately (see
        :meth:`_settle_bitmap_width_and_columns`), so it is settled afterwards
        rather than by the blanket rule.
        """
        custom = self._pattern.currentData() == "custom"
        for widget in self._arrangement_controls:
            widget.setEnabled(custom)
        self._settle_bitmap_width_and_columns()

    def _set_arrangement(
        self, block_columns: int, block_rows: int, block_order: str, two_d: bool
    ) -> None:
        """Push the four arrangement values onto their widgets with signals
        blocked - a preset fill (or a session restore) is one coherent change the
        caller re-renders once, not four cascading _on_view_change calls."""
        with signals_blocked(self._block_cols, self._block_rows, self._two_d):
            self._block_cols.setValue(block_columns)
            self._block_rows.setValue(block_rows)
            self._two_d.setChecked(two_d)
        select_combo_data(self._block_order, block_order)

    def _on_pattern_change(self) -> None:
        """Apply a chosen Pattern: a preset fills + locks the block/order/2D
        controls; Custom just unlocks them (leaving the current values as the
        starting point).

        Picking a Pattern also **clears the bitmap width**. A width is an
        override of the codec's own geometry chosen for one particular asset, not
        a standing preference, so it does not follow the user to a different
        arrangement - and leaving it set would be worse than untidy: it stays in
        force invisibly (the spin greys out under a preset) and springs back the
        moment the new arrangement is 2D. Clearing it hands the tile size and
        Cols back at the same time (:meth:`_settle_bitmap_width_and_columns`).

        Which is why this is one undo step and not five: the pick moves four
        controls and withdraws a fifth at once, and afterwards nothing on screen
        says what any of them held. The widgets are not touched here - the state
        is computed and pushed, and the command's first ``redo()`` is what fills
        them (:meth:`_apply_arrangement`).
        """
        data = self._pattern.currentData()
        live = self._live_arrangement()
        after = replace(live, bitmap_width=0)
        if isinstance(data, ArrangementPreset):
            after = replace(
                after,
                block_columns=data.block_columns,
                block_rows=data.block_rows,
                block_order=data.block_order,
                two_dimensional=data.two_dimensional,
            )
        self._push_arrangement("arrangement", after)

    def _arrangement_slot(self, field: str):  # noqa: ANN202 - a bare Qt slot
        """A slot for one arrangement control, naming which one it is.

        The bar's five controls share one push site, and the name is both the
        step's text and its merge key - so stepping the bitmap width up in ones
        is a single step, while a width followed by an Order change stays two.
        Bound at connect time for the reason
        :meth:`~...rendering.RenderingMixin._axis_slot` binds an axis.
        """
        return lambda *_args: self._push_arrangement(field, self._live_arrangement())

    def _live_arrangement(self) -> ArrangementState:
        """The arrangement the **widgets** are showing, right now."""
        return ArrangementState(
            block_columns=self._block_cols.value(),
            block_rows=self._block_rows.value(),
            block_order=self._block_order.currentData(),
            two_dimensional=self._two_d.isChecked(),
            bitmap_width=self._bitmap_width.value(),
            columns=self._columns.value(),
            columns_before_bitmap=self._columns_before_bitmap,
            byte_position=(
                self._recorded_byte_position() if self._doc is not None else 0
            ),
            pattern=self._pattern.currentData(),
        )

    def _stored_arrangement(self) -> ArrangementState:
        """The arrangement the last refresh **settled**, off ``doc.view``.

        The before state of a gesture: the widget that fired has already moved,
        and the view is where the value it moved from was written down. The two
        derived fields are read live all the same - Cols and the count a bitmap
        width displaced are settled by the refresh, not by the gesture, so at
        push time they still hold what they held before it.

        The Pattern picker is the one thing here the view does not record, and
        it has already moved too, so it is shadowed: :attr:`_pattern_choice` is
        the selection as of the last settle.
        """
        assert self._doc is not None
        view = self._doc.view
        return ArrangementState(
            block_columns=view.block_columns,
            block_rows=view.block_rows,
            block_order=view.block_order,
            two_dimensional=view.two_dimensional,
            bitmap_width=view.bitmap_width,
            columns=view.columns,
            columns_before_bitmap=self._columns_before_bitmap,
            byte_position=self._recorded_byte_position(),
            pattern=self._pattern_choice,
        )

    def _push_arrangement(self, field: str, after: ArrangementState) -> None:
        """Push one arrangement move; the command's first redo is the apply.

        With **no document** there is nothing to step against - a Pattern picked
        against an empty window only fills and locks the controls - so that lands
        straight through the apply helper, the way a plugin refresh does.
        """
        if self._applying_undo:
            return
        entry = self._workspace.current
        if self._doc is None or entry is None:
            self._apply_arrangement(after, recut=False)
            return
        before = self._stored_arrangement()
        if before == after:
            # A Pattern picked on the values it already had: the controls lock or
            # unlock and nothing the project file keeps has moved, so this lands
            # without costing a step.
            self._apply_arrangement(after, recut=False)
            return
        self._push_command(
            ArrangementCommand(self, entry, field, before=before, after=after)
        )

    def _apply_arrangement(self, state: ArrangementState, *, recut: bool) -> None:
        """Land a whole arrangement on the bar and redraw through it.

        The single application path for a Pattern pick, a hand-edited axis, an
        undo and a redo. Signals stay blocked throughout (the ``_restore_session``
        pattern): five controls settling one at a time would re-render five times
        and push four more commands.

        ``recut`` says the **bitmap width in force** moved, which alone among
        these changes the codec's *geometry* - bytes per tile, and therefore what
        a tile index means. That takes the same re-interpretation path a format
        switch does, landing on the byte position the state carries rather than
        on a tile index that now points somewhere else. Everything else is a
        repaint.
        """
        blocked = (
            self._block_cols,
            self._block_rows,
            self._block_order,
            self._two_d,
            self._bitmap_width,
            self._columns,
        )
        with signals_blocked(*blocked):
            self._block_cols.setValue(state.block_columns)
            self._block_rows.setValue(state.block_rows)
            self._two_d.setChecked(state.two_dimensional)
            self._bitmap_width.setValue(state.bitmap_width)
            self._columns.setValue(state.columns)
        select_combo_data(self._block_order, state.block_order)
        # The count a width displaced travels with the width itself: without it
        # a withdrawn width would hand Cols back a number derived from a bitmap
        # nobody is reading any more (:meth:`_settle_bitmap_width_and_columns`).
        self._columns_before_bitmap = state.columns_before_bitmap
        # The picker is landed from the state rather than re-derived from the
        # axes: a user in Custom whose hand-edited values happen to match a
        # preset must not have the controls re-locked under them. The lock pass
        # settles the bitmap width and Cols with it.
        select_combo_data(self._pattern, state.pattern)
        self._pattern_choice = state.pattern
        self._apply_pattern_lock()
        if self._doc is None:
            return
        if not recut:
            self._refresh_view()
        elif self._apply_pixel_config(self._pixel_preset_id(), state.byte_position):
            self.statusBar().showMessage(self._bitmap_width_note())

    def _sync_pattern_selection(self) -> None:
        """Reselect the Pattern entry that matches the live block/order/2D widgets
        (or Custom), and relock accordingly. Called after a session restore, whose
        widget values are the truth; signals stay blocked so this reselection does
        not re-enter _on_pattern_change and re-render."""
        preset = arrangement_preset_for(
            self._block_cols.value(),
            self._block_rows.value(),
            self._block_order.currentData(),
            self._two_d.isChecked(),
        )
        target = preset if preset is not None else "custom"
        select_combo_data(self._pattern, target)
        self._pattern_choice = target
        self._apply_pattern_lock()

    def _effective_bitmap_width(self) -> int:
        """The bitmap width actually in force — 0 unless the 2D walk is on.

        The width describes a *wide-bitmap* read, so it means nothing to the
        back-to-back tile walk; gating it here is what keeps the greyed-out
        spin from still quietly driving the codec's geometry.
        """
        return self._bitmap_width.value() if self._two_d.isChecked() else 0

    def _bitmap_width_note(self) -> str:
        """What the bitmap width did to the tile size, for the status footer.

        The effect is invisible in the picture — a re-cut grid looks like any
        other grid — and a codec whose tile size is fixed silently ignores the
        whole setting, so the footer is where those two outcomes are told apart.
        """
        width = self._effective_bitmap_width()
        tile_w, tile_h = self._pixel_tile_size()
        if width <= 0:
            if self._bitmap_width.value() > 0:  # set, but the walk is off
                return f"Bitmap width needs 2D - {tile_w}x{tile_h} tiles"
            return f"Bitmap width off - {tile_w}x{tile_h} tiles"
        if width % tile_w:
            return (
                f"Bitmap width {width} px - no effect: "
                f"{self._pixel_preset.currentText()} has a fixed "
                f"{tile_w}x{tile_h} tile"
            )
        return (
            f"Bitmap width {width} px - {tile_w}x{tile_h} tiles, "
            f"{width // tile_w} columns"
        )

    def _settle_bitmap_width_and_columns(self) -> None:
        """Gate the width, and point Cols at it while it is in force.

        Editable wherever it means anything, which is exactly: the 2D walk is on
        (a back-to-back tile read has no bitmap width). Deliberately *not* also
        gated on Custom, unlike the block controls: no Pattern preset carries a
        width, so a preset has nothing to say about it and locking it under one
        would leave a width that is still in force with no way to change it —
        which is what a session restore lands on, since the Pattern reads back as
        whichever preset the four axes match.

        With a bitmap width the column count stops being a free choice: it is
        however many tiles span that width, and any other value would show the
        bitmap at the wrong stride. Cols is left alone when the tiles don't
        divide the width — a codec that ignored the override keeps its own tile
        size, and no column count spans the width with it.

        Runs from the render path, so every route into a new arrangement — the
        checkbox, a Pattern preset, a session restore — lands here without each
        having to remember to.
        """
        self._bitmap_width.setEnabled(self._two_d.isChecked())
        self._refresh_tile_size()
        width = self._effective_bitmap_width()
        tile_w = self._pixel_tile_size()[0]
        spans = width > 0 and tile_w > 0 and width % tile_w == 0
        self._columns.setEnabled(not spans)
        if spans:
            # Remembered on the take-over only, so repeated refreshes under the
            # same width don't record the derived count as if it were a choice.
            if self._columns_before_bitmap is None:
                self._columns_before_bitmap = self._columns.value()
            if width // tile_w != self._columns.value():
                with signals_blocked(self._columns):
                    self._columns.setValue(width // tile_w)
        elif self._columns_before_bitmap is not None:
            # The width stopped applying (cleared, 2D off, a codec that ignores
            # it): hand Cols back at the value it had before, since the derived
            # one described a bitmap that is no longer being read.
            with signals_blocked(self._columns):
                self._columns.setValue(self._columns_before_bitmap)
            self._columns_before_bitmap = None
