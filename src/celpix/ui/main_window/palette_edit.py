"""The palette dock's Edit Palette window: preview a colour operation, commit it.

One operation over a run of entries — the selected colour, the active palette
row or the whole palette — previewed live while the window is up and written
as **one undo step** on OK (``docs/design/palette-editing.md`` §1).

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
