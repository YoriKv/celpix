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

from celpix.core.errors import PipelineError
from celpix.pipeline import pipeline
from celpix.project.workspace import (
    PaletteMode,
)
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
        """The editor moved a color - fork if needed, then push the edit."""
        entry = self._workspace.current
        if self._doc is None or entry is None or self._applying_undo:
            return
        index = self._palette_panel.selected_index()
        if index is None or index >= len(self._doc.palette):
            return
        # Default and Emulator palettes can't hold an edit; forking to Custom
        # first is what makes the edit land somewhere (and is its own undo step).
        if self._palette_mode in (PaletteMode.DEFAULT, PaletteMode.EMULATOR):
            self._fork_custom_palette()
        before = self._doc.palette.color(index)
        if before == argb:
            return
        self._push_command(
            ColorEditCommand(self, entry, index, before=before, after=argb)
        )

    def _apply_color_edit(self, index: int, argb: int, revision: int) -> None:
        """Land one color on the document - :class:`ColorEditCommand`'s apply.

        Never mutates in place: undo snapshots hold the palette by reference, so
        the edit swaps in a new one (:meth:`Palette.with_color`).
        """
        if self._doc is None:
            return
        self._doc.palette = self._doc.palette.with_color(index, argb)
        # Mark the entry so Write splices just this one back, leaving every
        # other entry's bytes exactly as they were read (a color codec doesn't
        # round-trip bytes - see Document.palette_bytes). The mark survives an
        # undo: re-encoding an unchanged color is harmless, and the entry is
        # clean again anyway once its revision walks back to the saved one.
        self._doc.palette_edits.add(index)
        entry = self._workspace.current
        # A file-backed palette now differs from its bytes on disk - stamped on
        # the *palette* pathway, so Write doesn't also rewrite the graphic. A
        # custom palette is saved with the project, so it dirties nothing.
        if entry is not None and self._doc.palette_config.write_enabled:
            self._workspace.set_palette_revision(entry, revision)
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
