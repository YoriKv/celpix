"""The palette dock's Edit Palette window: preview a colour operation, commit it.

One operation over a run of entries — the selected colour, the active palette
row or the whole palette — previewed live while the window is up and written
as **one undo step** on OK (``docs/design/palette-editing.md`` §1).

The toolbar's **Edit Palette…** on a swatch view (:meth:`PaletteEditMixin.
_edit_view_palette`) is the same window over the **canvas** instead: a colour
table viewed as swatches is edited where it is looked at, and what the dock
shows may be another palette entirely. There the operation is a pixel edit —
each swatch is a colour word in the entry's own bytes — previewed through the
pixel tools' own preview and committed through their commit, one
:class:`~celpix.ui.undo_commands.PixelEditCommand` for the run.

**The preview bypasses the undo stack.** Pushing a command per slider tick
would re-encode and splice every entry's bytes on every move; instead the
operation's result is swapped in as the document's palette — the same swap a
committed edit ends on — and the view refreshed, so the canvas and the swatch
grid show it. Cancel swaps the original back; OK swaps it back too, and only
then writes the result through the ordinary colour-edit path, which diffs
against what is shown. Nothing can move the view in between: the window is
modal, which is what makes the swap safe to undo by hand.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog

from celpix.core.palette import Palette
from celpix.plugins.builtins.palette_swatch import SWATCH
from celpix.project.workspace import EntryKind
from celpix.ui.palette_edit_dialog import EditScope, PaletteEditDialog
from celpix.ui.widgets import counted


class PaletteEditMixin:
    """The Edit Palette window and its preview, a slice of
    :class:`~celpix.ui.main_window.window.MainWindow`."""

    def _edit_palette(self) -> None:
        """Open the Edit Palette window on the palette the dock is showing."""
        doc = self._palette_doc()
        if doc is None or self._applying_undo:
            return
        original = doc.palette
        selected = self._palette_panel.selected_index()
        if selected is not None and selected >= len(original):
            selected = None
        dialog = PaletteEditDialog(
            self,
            selected_color=None if selected is None else original.color(selected),
            # A direct-colour format - View as Palette among them - indexes no
            # row, and the row its geometry would size is a 2-colour accident.
            rows=not self._is_direct_color(),
        )

        def preview() -> None:
            start, count = self._palette_edit_range(dialog.scope(), selected)
            colors = list(original.colors)
            colors[start : start + count] = dialog.op().apply_all(
                colors[start : start + count]
            )
            self._show_palette(Palette(colors))

        dialog.changed.connect(preview)
        preview()
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            # Back to the real palette before anything is written: the commit
            # diffs each entry against what is on screen, and on Cancel this
            # *is* the whole undo.
            self._show_palette(original)
        if not accepted:
            return
        op = dialog.op()
        start, count = self._palette_edit_range(dialog.scope(), selected)
        colors = op.apply_all([original.color(i) for i in range(start, start + count)])
        if self._write_colors_at(start, colors, count, "edit palette"):
            self.statusBar().showMessage(
                f"Applied {op.describe()} to {counted(count, 'color')}."
            )
        else:
            self.statusBar().showMessage(f"{op.describe()} changed no color.")

    def _palette_edit_range(
        self, scope: EditScope, selected: int | None
    ) -> tuple[int, int]:
        """``(start, count)`` of the entries ``scope`` names, clamped to the palette.

        The row is the one the view indexes through — the Copy/Paste Palette
        Row unit — and the selection is the grid's; both are asked of the
        palette's length, so a short palette never yields an index off its end.
        """
        length = len(self._shown_palette())
        if scope is EditScope.SELECTED and selected is not None:
            start, count = selected, 1
        elif scope is EditScope.ALL:
            start, count = 0, length
        else:
            start, count = self._active_palette_row()
        start = min(start, length)
        return start, max(0, min(count, length - start))

    # -- the toolbar's window: the swatches on the canvas ----------------------
    def _edit_view_palette(self) -> None:
        """Toolbar ▸ Edit Palette… on a swatch view: one colour operation over
        the swatches on screen, committed as one pixel edit.

        The dock's window with the canvas for its subject. The working grid is
        the view's composed image, the one every pixel tool paints on, taken
        once up front since the window is modal; each swatch is recoloured
        whole — the codec encodes a tile's most common colour, so a square is
        the smallest thing that reads back as one colour — and the result goes
        out through the pixel tools' preview and commit, which is what makes
        it one undo step and what takes the preview down on Cancel (the
        commit's own refresh, or an explicit one).
        """
        doc = self._doc
        if (
            doc is None
            or doc.is_tilemap
            or doc.is_sprite
            or self._applying_undo
            or not self._palette_view_active()
        ):
            return
        base = self._window_grid()
        if base is None:
            return
        selected = self._palette_panel.selected_index()
        palette = self._shown_palette()
        if selected is not None and selected >= len(palette):
            selected = None
        in_view = [
            (x, y)
            for y in range(0, base.height, SWATCH)
            for x in range(0, base.width, SWATCH)
        ]
        in_selection = self._selected_swatches(base)
        dialog = PaletteEditDialog(
            self,
            selected_color=None if selected is None else palette.color(selected),
            on_view=True,
            has_selection=bool(in_selection),
        )

        def edited():  # noqa: ANN202 — a grid
            squares = in_selection if dialog.scope() is EditScope.SELECTION else in_view
            op = dialog.op()
            grid = base.copy()
            # Memoised per colour: a table is a few hundred distinct colours
            # drawn as thousands of pixels.
            results: dict[int, int] = {}
            for x, y in squares:
                before = base.get(x, y)
                after = results.get(before)
                if after is None:
                    after = results[before] = op.apply(before)
                if after == before:
                    continue
                for py in range(y, min(y + SWATCH, base.height)):
                    for px in range(x, min(x + SWATCH, base.width)):
                        grid.set(px, py, after)
            return grid, len(squares)

        def preview() -> None:
            self._render_preview(edited()[0])

        dialog.changed.connect(preview)
        preview()
        accepted = False
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            if not accepted:
                self._refresh_view()  # the committed picture, preview gone
        if not accepted:
            return
        grid, count = edited()
        op = dialog.op()
        if self._commit_grid(grid, base, "edit palette", no_op_step=False):
            self.statusBar().showMessage(
                f"Applied {op.describe()} to {counted(count, 'swatch')}."
            )
        else:
            self.statusBar().showMessage(f"{op.describe()} changed no swatch.")

    def _selected_swatches(self, grid) -> list[tuple[int, int]]:  # noqa: ANN001 — a grid
        """The top-left pixel of every swatch of ``grid`` the canvas selection
        covers, in selection order.

        A selected tile is a slot of the window, and the window's layout says
        which cell that slot was composed into — the same mapping the canvas
        drew it with, so a block grouping scatters nothing. A tile is a whole
        number of swatches side by side (a packed format's several entries), so
        the squares are stepped off along it. Tiles scrolled out of the window
        have no square to name and are passed over.
        """
        assert self._doc is not None
        doc = self._doc
        layout = self._view_layout()
        tile_w, tile_h = doc.tile_width, doc.tile_height
        slots = self._window_slots()
        squares = []
        for tile in self._selection_tiles():
            slot = tile - self._offset
            if not 0 <= slot < slots:
                continue
            tile_x, tile_y = layout.slot_to_pos(slot)
            base_x, base_y = tile_x * tile_w, tile_y * tile_h
            for y in range(base_y, min(base_y + tile_h, grid.height), SWATCH):
                for x in range(base_x, min(base_x + tile_w, grid.width), SWATCH):
                    squares.append((x, y))
        return squares

    def _show_palette(self, palette: Palette) -> None:
        """Put ``palette`` on screen as the document's, with no record of it.

        The swap a committed colour edit ends on, minus the dirt and the
        splice: the owning document takes it, a file palette's graphics mirror
        it, and the view repaints — canvas and swatch grid both.
        """
        doc = self._palette_doc()
        if doc is None:
            return
        doc.palette = palette
        owner = self._palette_owner_entry()
        if owner is not None and owner.kind is EntryKind.PALETTE:
            self._mirror_palette(owner)
        if self._doc is not None:
            self._refresh_view()
