"""Editing one color: the shared editor dialog, the eyedropper, the write-back.

One non-modal :class:`~celpix.ui.color_editor.ColorEditorDialog` is reused and
*retargeted* as the palette selection moves, so clicking through the grid with
the editor open works the way it reads.

What the editor offers is probed from the palette's own codec rather than
declared: whether to show an alpha input, and the "Stored as" preview that
round-trips the color through the format so the precision loss is visible
*before* it is written. An edit marks only its own entry, because a color codec
does not round-trip *bytes* - Write splices the edited entries into the bytes
the palette was read from and leaves every other one exactly as found.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QMenu

from celpix.core.document import Document
from celpix.core.errors import PipelineError
from celpix.pipeline import pipeline
from celpix.project.workspace import (
    Entry,
    EntryKind,
    PaletteMode,
)
from celpix.ui import clipboard
from celpix.ui.color_editor import ColorEditorDialog
from celpix.ui.undo_commands import (
    ColorEditCommand,
)


class ColorEditingMixin:
    """The color editor dialog, eyedropper, and what a color edit writes back.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _update_color_details(self) -> None:
        """Render the panel's selected color into the details readout.

        The position reads as subpalette + color-within-it (the pixel format's
        index space sizes the subpalette), matching how tiles actually reference
        the entry - not as a flat palette index.
        """
        index = self._palette_panel.selected_index()
        if self._doc is None or index is None:
            text = "No color selected"
        else:
            subpal, color = divmod(index, self._index_space())
            argb = self._doc.palette.color(index)
            a = (argb >> 24) & 0xFF
            r = (argb >> 16) & 0xFF
            g = (argb >> 8) & 0xFF
            b = argb & 0xFF
            text = (
                f"Subpal {subpal} · Color {color} (${color:X}) · #{argb:08X}\n"
                f"R {r}  G {g}  B {b}  A {a}"
            )
        # Runs on every view refresh (navigation included) - skip the label
        # update when nothing about the selection changed.
        if text != self._color_details.text():
            self._color_details.setText(text)

    # -- color editing ----------------------------------------------------
    def _on_palette_color_selected(self, _index: int) -> None:
        """A new swatch is selected: update the readout and retarget any editor.

        The open editor follows the selection rather than pinning to the entry
        it opened on - clicking through the grid with the editor up is the
        natural way to work through a palette.
        """
        self._update_color_details()
        self._sync_color_editor(retarget=True)

    def _open_color_editor(self, index: int) -> None:
        """Open (or raise) the shared color editor on palette entry ``index``."""
        if self._doc is None or index >= len(self._doc.palette):
            return
        dialog = self._color_editor
        if dialog is None:
            dialog = ColorEditorDialog(self)
            dialog.editor.color_changed.connect(self._on_color_changed)
            dialog.editor.pick_toggled.connect(self._set_pick_mode)
            dialog.closed.connect(self._on_color_editor_closed)
            self._color_editor = dialog
        self._sync_color_editor(retarget=True)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _sync_color_editor(self, *, retarget: bool = False) -> None:
        """Point the open editor at the selected entry's current color.

        Called on every view refresh, so it must be inert when nothing moved:
        rewriting the inputs unconditionally would clobber a half-typed hex
        value. ``retarget`` additionally re-titles the window, re-reads the
        stored-as quantizer for the current mode, and re-arms Revert.
        """
        dialog = self._color_editor
        if dialog is None or self._doc is None:
            return
        index = self._palette_panel.selected_index()
        if index is None:
            return
        color = self._doc.palette.color(index)
        if retarget:
            dialog.set_entry(self._color_entry_label(index))
            dialog.editor.set_alpha_enabled(self._palette_stores_alpha())
            dialog.editor.set_quantizer(self._palette_quantizer())
        if retarget or color != dialog.editor.color():
            dialog.editor.set_color(color, mark_original=retarget)

    def _color_entry_label(self, index: int) -> str:
        subpal, color = divmod(index, self._index_space())
        return f"entry {index} (subpal {subpal}, color {color})"

    def _palette_stores_alpha(self) -> bool:
        """Whether the editor should offer an alpha input for this palette.

        Only a format that really carries an alpha field does. Default and
        Custom palettes are never encoded, so there is no format to ask - and
        their colors come from opaque sources, so alpha stays hidden there too
        rather than inviting edits nothing downstream honours.
        """
        if self._doc is None or not self._palette_mode.has_source:
            return False
        try:
            return pipeline.palette_has_alpha(
                self._doc.palette_config.interpret_preset_id, self._registry
            )
        except PipelineError:
            return False

    def _palette_quantizer(self) -> Callable[[int], int] | None:
        """The round trip behind the editor's "Stored as" preview, or ``None``.

        Only the modes that re-encode through a color codec have one. A custom
        palette holds ARGB verbatim in the project and the generated default is
        never written at all, so neither can lose precision - showing them a
        lossless preview would just be noise.
        """
        if self._doc is None or not self._palette_mode.has_source:
            return None
        preset_id = self._doc.palette_config.interpret_preset_id
        registry = self._registry
        return lambda argb: pipeline.quantize_color(argb, preset_id, registry)

    def _on_color_changed(self, argb: int) -> None:
        """The editor moved a color - fork if needed, then push the edit.

        The edit targets whatever *owns* the on-screen palette: the linked PALETTE
        entry (and its document) in File mode, or the current graphic in every
        other mode. So editing a file palette dirties the palette entry, not the
        graphic that happens to render it.
        """
        if self._doc is None or self._workspace.current is None or self._applying_undo:
            return
        index = self._palette_panel.selected_index()
        if index is None:
            return
        # Default and Emulator palettes can't hold an edit; forking to Custom
        # first is what makes the edit land somewhere (and is its own undo step).
        if self._palette_mode in (PaletteMode.DEFAULT, PaletteMode.EMULATOR):
            self._fork_custom_palette()
        owner = self._palette_owner_entry()
        doc = self._palette_doc()
        if owner is None or doc is None or index >= len(doc.palette):
            return
        before = doc.palette.color(index)
        if before == argb:
            return
        self._push_command(
            ColorEditCommand(self, owner, doc, index, before=before, after=argb)
        )

    def _apply_color_edit(
        self, owner: Entry, doc: Document, index: int, argb: int, revision: int
    ) -> None:
        """Land one color on ``doc`` - :class:`ColorEditCommand`'s apply.

        ``doc`` is the palette's *owning* document (a PALETTE entry's in File mode,
        else the graphic's own). Never mutates in place: undo snapshots hold the
        palette by reference, so the edit swaps in a new one
        (:meth:`Palette.with_color`).
        """
        doc.palette = doc.palette.with_color(index, argb)
        # Mark the entry so Write splices just this one back, leaving every
        # other entry's bytes exactly as they were read (a color codec doesn't
        # round-trip bytes - see Document.palette_bytes). The mark survives an
        # undo: re-encoding an unchanged color is harmless, and the entry is
        # clean again anyway once its revision walks back to the saved one.
        doc.palette_edits.add(index)
        # A file/offset palette now differs from its bytes on disk - dirt on the
        # *palette* pathway of its owner (the PALETTE entry for a file palette, the
        # graphic itself for an offset one), so a graphic Write never rewrites the
        # picture for a color edit. A custom palette is saved with the project, so
        # write_enabled is off and it dirties nothing.
        if doc.palette_config.write_enabled:
            self._workspace.set_palette_revision(owner, revision)
        # A file palette is shown by every graphic that references it: push the new
        # colors onto all of them so one edit updates them together.
        if owner.kind is EntryKind.PALETTE:
            self._mirror_palette(owner)
        self._refresh_view()  # also re-syncs the open editor

    def _set_pick_mode(self, on: bool) -> None:
        """Arm/disarm the eyedropper across every surface that can be sampled."""
        self._canvas.set_eyedropper(on)
        self._palette_panel.set_eyedropper(on)
        if on:
            self.statusBar().showMessage(
                "Eyedropper: click a pixel on the canvas or a palette swatch."
            )

    def _on_color_picked(self, argb: int) -> None:
        """A sampled color fills the editor and applies as an ordinary edit."""
        dialog = self._color_editor
        self._set_pick_mode(False)
        if dialog is None:
            return
        dialog.editor.set_pick_active(False)
        # set_color is deliberately signal-free, so the edit is pushed by hand.
        dialog.editor.set_color(argb)
        self._on_color_changed(argb)

    def _on_color_editor_closed(self) -> None:
        self._color_editor = None
        self._set_pick_mode(False)

    # -- clipboard --------------------------------------------------------
    def _active_subpalette(self) -> tuple[int, int]:
        """``(start, count)`` of the subpalette the view is indexing right now.

        The window into the palette a tile's indices actually reference — the
        subpalette row times the format's index space — the unit Copy/Paste
        Subpalette work on.
        """
        count = self._index_space()
        return self._subpalette.value() * count, count

    def _copy_palette_color(self) -> None:
        """Copy the selected swatch's color to the system clipboard.

        Goes out both as a lossless Celpix payload and as ``#RRGGBB``/
        ``#AARRGGBB`` text, so it pastes back verbatim here and into any other
        program that reads hex.
        """
        index = self._palette_panel.selected_index()
        if self._doc is None or index is None:
            return
        color = self._doc.palette.color(index)
        clipboard.put_colors([color])
        self.statusBar().showMessage(f"Copied color {clipboard.color_text(color)}.")

    def _copy_subpalette(self) -> None:
        """Copy the whole active subpalette's colors to the clipboard."""
        if self._doc is None:
            return
        start, count = self._active_subpalette()
        palette = self._doc.palette
        n = max(0, min(count, len(palette) - start))
        if n == 0:
            self.statusBar().showMessage("No subpalette colors to copy.")
            return
        clipboard.put_colors([palette.color(start + k) for k in range(n)])
        self.statusBar().showMessage(f"Copied subpalette ({n} colors).")

    def _paste_palette_color(self) -> None:
        """Write the clipboard's color onto the selected swatch, as one undo step.

        A run of clipboard colors pastes only its first here: the grid selects
        one entry at a time (Paste Subpalette fills a range).
        """
        if self._doc is None or self._workspace.current is None or self._applying_undo:
            return
        index = self._palette_panel.selected_index()
        if index is None:
            self.statusBar().showMessage("Select a swatch to paste a color onto.")
            return
        colors = clipboard.take_colors()
        if not colors:
            self.statusBar().showMessage("No color on the clipboard to paste.")
            return
        if self._paste_colors_at(index, colors, 1, "color"):
            self.statusBar().showMessage(
                f"Pasted color {clipboard.color_text(colors[0])}."
            )
        else:
            self.statusBar().showMessage("Clipboard color matches the selection.")

    def _paste_subpalette(self) -> None:
        """Fill the active subpalette with the clipboard's colors, as one undo step."""
        if self._doc is None or self._workspace.current is None or self._applying_undo:
            return
        colors = clipboard.take_colors()
        if not colors:
            self.statusBar().showMessage("No colors on the clipboard to paste.")
            return
        start, count = self._active_subpalette()
        if self._paste_colors_at(start, colors, count, "subpalette"):
            self.statusBar().showMessage("Pasted colors into the subpalette.")
        else:
            self.statusBar().showMessage("Clipboard colors match the subpalette.")

    def _paste_colors_at(
        self, start: int, colors: list[int], limit: int, label: str
    ) -> bool:
        """Write ``colors`` onto entries ``[start, start+limit)``; True if any changed.

        Clamped to both the clipboard run and the palette's end, and only the
        entries that actually differ are written, so an identical paste is a no-op.
        The edits take the same write-back as an editor edit — forking a read-only
        source to a Custom palette first — and, when more than one lands, are
        grouped in a macro so the whole paste undoes in a single step.
        """
        assert self._doc is not None
        palette = self._doc.palette
        span = max(0, min(len(colors), limit, len(palette) - start))
        changed = [
            (start + k, colors[k])
            for k in range(span)
            if palette.color(start + k) != colors[k]
        ]
        if not changed:
            return False
        grouped = len(changed) > 1
        if grouped:
            self._undo_stack.beginMacro(f"paste {label}")
        # Default/Emulator palettes can't hold an edit; forking to Custom first is
        # what makes it land (inside the macro, so undo peels it with the edits).
        if self._palette_mode in (PaletteMode.DEFAULT, PaletteMode.EMULATOR):
            self._fork_custom_palette()
        owner = self._palette_owner_entry()
        doc = self._palette_doc()
        if owner is not None and doc is not None:
            for index, argb in changed:
                before = doc.palette.color(index)
                if before != argb:
                    self._push_command(
                        ColorEditCommand(
                            self, owner, doc, index, before=before, after=argb
                        )
                    )
        if grouped:
            self._undo_stack.endMacro()
        return True

    def _show_palette_menu(self, pos) -> None:  # noqa: ANN001 — Qt supplies a QPoint
        """The palette grid's right-click menu: copy/paste a color or the subpalette.

        Built on demand (like the canvas menu) so Paste reflects the live
        clipboard. The shortcuts shown mirror what the grid handles itself.
        """
        if self._doc is None:
            return
        has_selection = self._palette_panel.selected_index() is not None
        can_paste = clipboard.has_colors()
        menu = QMenu(self)
        for label, slot, shortcut, enabled in (
            (
                "Copy Color",
                self._copy_palette_color,
                QKeySequence.StandardKey.Copy,
                has_selection,
            ),
            (
                "Paste Color",
                self._paste_palette_color,
                QKeySequence.StandardKey.Paste,
                has_selection and can_paste,
            ),
            (None, None, None, None),  # separator
            ("Copy Subpalette", self._copy_subpalette, "Ctrl+Shift+C", True),
            ("Paste Subpalette", self._paste_subpalette, "Ctrl+Shift+V", can_paste),
        ):
            if label is None:
                menu.addSeparator()
                continue
            action = menu.addAction(label)
            action.setShortcut(shortcut)
            action.setEnabled(enabled)
            action.triggered.connect(slot)
        menu.exec(self._palette_panel.mapToGlobal(pos))
