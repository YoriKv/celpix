"""The compression preview: decode-in-place, structure scan, smart scan.

The main view always shows **raw** bytes. The toolbar's compression combo
instead drives a parallel, view-only pipeline run over the current window, whose
result appears in a floating overlay - so the current offset is a probe for
"does a compressed structure start here?", and scrubbing the raw view hunts for
structures.

"Fails to decompress" is therefore the *signal*, not an error: these formats can
only be decoded from a structure's first byte, so a failure means no structure
starts here and the overlay simply hides. A structure that decodes completely
can be made a real slice entry (File > New Slice from View), which is how a
preview becomes editable.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QPushButton,
    QWidget,
)

from celpix.core.arrangement import (
    BlockLayout,
)
from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    KEY_INPUTS,
    KEY_SURROUND,
    KEY_SURROUND_START,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.pipeline import pipeline
from celpix.plugins.base import NO_COMPRESSION
from celpix.plugins.builtins.lz16 import KEY_LZ16_ROWS
from celpix.ui.decompress_overlay import Badge
from celpix.ui.searchable_combo import fill_stage_combo


class CompressionMixin:
    """The decompression preview, the structure scan and the smart scan.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _populate_compression(self, selected: str = NO_COMPRESSION) -> None:
        """Fill the compression combo from the registry, grouped by category.

        The pass-through names no category, which is what keeps it at the top
        where a default belongs (:func:`~celpix.plugins.base.category_order`);
        the schemes below it stay in registration order inside each group, so
        each one still sits beside its improved encoder.
        """
        fill_stage_combo(
            self._compression,
            self._registry.plugins(Stage.COMPRESSION),
            selected or NO_COMPRESSION,
        )

    def _refresh_overlay(self) -> None:
        """Feed the floating decompression preview, or hide it.

        A parallel, view-only run of the pipeline over the current window's raw
        bytes: Decompress (best-effort - the window may cut a structure short)
        then the same pixel-interpret and palette paths the main view uses, so
        the overlay always reflects the active preset, palette row, and zoom.
        The main document is untouched; failure to decompress means "no
        structure starts at this offset" and simply hides the preview.
        """
        assert self._doc is not None
        compression_id = self._compression_id()
        # A color table has nothing to unpack: while the bytes are read as
        # palette swatches the picker is off the bar, and the scheme it still
        # holds is kept for the next tile format rather than run here.
        active = compression_id != NO_COMPRESSION and not self._palette_view_active()
        # A scan looks for a structure's *end* inside the probe, and a scheme
        # with no end marker never reports one — so on those the button would
        # only ever say "no match", and it stays off instead (``_on_scan``).
        scannable = active and self._scannable() and not self._scanning
        self._scan_button.setEnabled(scannable)
        self._smart_scan_button.setEnabled(scannable)
        self._next_structure = None
        self._structure_extent = None
        try:
            self._present_overlay(compression_id if active else None)
        finally:
            self._refresh_structure_actions()

    def _refresh_structure_actions(self) -> None:
        """Arm Jump-to-Next from the overlay's scan state.

        Armed only while a whole structure (known end) is in view. Kept
        separate from :meth:`_refresh_overlay` because it is the single source
        of truth for the button: a scan's blanket UI re-enable
        (:meth:`_set_scan_ui`) must restore its real state through here rather
        than leave it switched on. (``_structure_extent`` is what File > New
        Slice from View reads — the same state, no button of its own.)
        """
        self._jump_next.setEnabled(self._next_structure is not None)

    def _present_overlay(self, compression_id: str | None) -> None:
        """The overlay body of :meth:`_refresh_overlay` (which owns the button
        state around every early exit here)."""
        assert self._doc is not None
        if compression_id is None:
            self._overlay.hide_overlay()
            return
        view = self._doc.view
        window = self._doc.window_bytes(
            view.tile_offset, view.columns * self._view_rows(), view.byte_nudge
        )
        if not window:
            self._overlay.hide_overlay()
            return
        ctx = PipelineContext()
        ctx.set(KEY_DECOMPRESS_PARTIAL, True)
        # The window is cut from the entry's buffer, and a scheme that copies
        # from the bytes before its stream resolves them through that buffer.
        ctx.set(KEY_SURROUND, self._doc.pixel_data)
        ctx.set(KEY_SURROUND_START, self._byte_position())
        # A scheme that needs a table decodes with the file's binding for it,
        # or not at all: no preview is the honest answer to an unbound input,
        # and the badge beside the picker says which (``main_window/inputs.py``).
        inputs = self._preview_config_inputs(compression_id)
        if inputs is None:
            self._overlay.hide_overlay()
            return
        if inputs:
            ctx.set(KEY_INPUTS, inputs[Stage.COMPRESSION])
        engine, preset = self._registry.engine_for(self._pixel_preset_id())
        layout = BlockLayout(
            view.columns, view.block_columns, view.block_rows, view.block_order
        )
        try:
            plugin = self._registry.plugin(Stage.COMPRESSION, compression_id)
            raw = plugin.decompress(bytes(window), ctx)
            if not raw:  # nothing decompressed: not a structure
                self._overlay.hide_overlay()
                return
            # Same arrangement path as the live view (2D reflow / block layout),
            # but sized to the whole decompressed structure (max_rows=None).
            image, _ = self._render_arrangement(
                raw,
                engine,
                pipeline.tile_params(self._doc, engine, preset.params),
                layout,
                view.two_dimensional,
                max_rows=None,
            )
        except Exception:  # noqa: BLE001 - any failure means "not a structure"
            self._overlay.hide_overlay()
            return

        parts = [f"{len(raw):#x} B raw from {len(window):#x} B window"]
        consumed = ctx.get(KEY_COMPRESSED_SIZE)
        badge = None
        if consumed and ctx.get(KEY_DECOMPRESS_COMPLETE):
            # The structure's own end was inside the window: report its true
            # extent, and arm Jump-to-Next at the byte right after it.
            parts.append(f"structure {consumed:#x} B")
            self._structure_extent = (self._byte_position(), consumed)
            after = self._byte_position() + consumed
            if after < len(self._doc.pixel_data):
                self._next_structure = after
        elif plugin.info.self_delimiting:
            # The scheme has an end marker and this decode never reached it, so
            # the window really did cut a structure short: a warning, with a fix.
            badge = Badge(
                "end not in view",
                "The structure's end marker is not inside the view\n"
                "window, so this preview stops where the window does.\n"
                "Add rows or columns to decode more of it.",
                warning=True,
            )
        else:
            # A stream-based scheme has no end to find, in this window or any
            # other - so this is a statement of where the preview ends, not a
            # problem to fix. The window's end is then the only extent on offer,
            # and it is what New Slice from View takes: `consumed` rather than
            # the raw window size, so the slice ends on a whole packet the way
            # the decoder read it. Jump-to-Next stays off - "the byte after this
            # structure" means nothing when the structure had no end.
            if consumed:
                parts.append(f"slice {consumed:#x} B")
                self._structure_extent = (self._byte_position(), consumed)
            badge = Badge(
                "end of view window",
                "This scheme has no end marker, so it decodes as far\n"
                "as the view window reaches and no further.\n"
                "New Slice from View bounds the slice at that same\n"
                "point; widen the window for more, or set the true length.",
            )
        rows16 = ctx.get(KEY_LZ16_ROWS)
        if rows16 is not None:
            parts.append(f"{rows16} tile row(s)")
        self._overlay.show_result(
            image,
            engine.tile_size(preset.params),
            view,
            self._grid_settings(),
            f"Decompressed - {plugin.info.name}",
            ", ".join(parts),
            badge,
        )

    def _scannable(self) -> bool:
        """Whether the current scheme can be scanned for at all: it has to carry
        its own end (terminator or declared size) for a probe to ever complete."""
        compression_id = self._compression_id()
        if compression_id == NO_COMPRESSION:
            return False
        return self._registry.plugin(
            Stage.COMPRESSION, compression_id
        ).info.self_delimiting

    def _on_jump_next(self) -> None:
        if self._doc is None or self._next_structure is None:
            return
        self._set_byte_position(self._next_structure)

    def _on_scan(self) -> None:
        """Scan forward for the next decodable structure (re-entered by Stop).

        Runs inline, pumping the event loop between batches so Stop stays
        clickable; everything else is frozen by :meth:`_set_scan_ui`. A hit is
        a non-empty structure whose decode *reported complete* - its end was
        inside the probe. A scheme with no end marker (PackBits, RLE2, LZ16)
        never reports one, so there is nothing to scan for and the button is
        kept off (:meth:`_scannable`); a scheme that declares a start
        alignment is probed only at aligned offsets.
        """
        self._run_scan(self._scan_button, None)

    def _on_smart_scan(self) -> None:
        """Scan forward for the next structure that *looks like graphics*.

        The plain scan with :func:`~celpix.pipeline.pipeline.looks_like_graphics`
        as a further test on every complete structure, judged in the current
        pixel format's tiles: this is what makes the pixel preset part of the
        scan. It starts *after* the structure in view when the view sits on one
        - the user has seen that one - and at the next byte otherwise, like
        Scan. A codec-level false hit still passes now and then (1-3% of them
        did, measured); the overlay is what to judge it by.
        """
        if self._doc is None or self._scanning:
            self._run_scan(self._smart_scan_button, None)
            return
        bytes_per_tile = self._doc.bytes_per_tile
        self._run_scan(
            self._smart_scan_button,
            lambda out, consumed: pipeline.looks_like_graphics(
                out, consumed, bytes_per_tile
            ),
            skip_structure_in_view=True,
        )

    def _run_scan(
        self,
        button: QPushButton,
        accept: Callable[[bytes, int], bool] | None,
        *,
        skip_structure_in_view: bool = False,
    ) -> None:
        """The scan loop behind both buttons: ``button`` is the one that reads
        Stop while it runs, ``accept`` the smart scan's extra test."""
        if self._scanning:
            self._scan_stop = True
            return
        if self._doc is None:
            return
        compression_id = self._compression_id()
        if self._palette_view_active() or not self._scannable():
            return
        plugin = self._registry.plugin(Stage.COMPRESSION, compression_id)
        # "Find the next stream that decodes with *this* table": the scan runs
        # with the file's bindings, and without them a scheme that needs one
        # cannot be tried at all.
        inputs = self._preview_config_inputs(compression_id)
        if inputs is None:
            self.statusBar().showMessage(
                "Bind this codec's inputs before scanning (the badge beside it)."
            )
            return
        data = self._doc.pixel_data
        # Probe as many compressed bytes as one screenful decompresses to: a
        # structure bigger than the view can show is not worth confirming here.
        probe_bytes = max(
            1,
            self._columns.value() * self._view_rows() * self._doc.bytes_per_tile,
        )
        start = self._byte_position() + 1
        if skip_structure_in_view and self._next_structure is not None:
            # The view sits on a whole structure with a known end: the user
            # has seen it, so the search begins where it stops.
            start = self._next_structure
        self._scanning = True
        self._scan_stop = False
        self._set_scan_ui(True, button)
        try:
            result = pipeline.find_next_structure(
                data,
                plugin,
                probe_bytes,
                start,
                on_tick=self._scan_tick,
                inputs=inputs.get(Stage.COMPRESSION),
                alignment=plugin.info.alignment,
                accept=accept,
            )
        finally:
            self._scanning = False
            self._set_scan_ui(False, button)
        # Land where the scan ended - the hit, or wherever Stop/EOF left it.
        landing = result.found if result.found is not None else result.end
        self._set_byte_position(min(landing, len(data) - 1))
        if result.found is not None:
            self.statusBar().showMessage(
                f"Structure found at {self._format_offset(result.found)}."
            )
        elif result.stopped:
            self.statusBar().showMessage("Scan stopped.")
        else:
            self.statusBar().showMessage("Scan reached the end without a match.")

    def _scan_tick(self, pos: int) -> bool:
        """Progress callback for :func:`~celpix.pipeline.find_next_structure`:
        report the position, pump the event loop so Stop stays clickable, and
        return whether Stop was pressed (which aborts the scan)."""
        self.statusBar().showMessage(f"Scanning… {self._format_offset(pos)}")
        QApplication.processEvents()
        return self._scan_stop

    def _set_scan_ui(self, active: bool, button: QPushButton | None = None) -> None:
        """Swap the running button to Stop and freeze the rest of the UI while a
        scan runs; on thaw, give back exactly what was taken.

        The toolbars live *inside* the central widget, so freezing that
        wholesale would take the Stop button down with everything else and
        leave the scan uninterruptible. Instead the freeze walks up from the
        button to the central widget disabling the *siblings* along the way -
        the other controls in its group, the other groups on the codecs bar,
        the other bars and the canvas - and the docks and menu bar outside it.
        Only widgets that were enabled are touched and remembered, so the thaw
        restores them and nothing else: a control some owner keeps off (a gated
        group, a locked block picker) stays off through a scan, and one the
        overlay arms (Jump) is re-decided by :meth:`_refresh_structure_actions`
        afterwards rather than switched on by the thaw.
        """
        button = button or self._scan_button
        label = "Scan" if button is self._scan_button else "Smart Scan"
        button.setText("Stop" if active else label)
        if not active:
            for widget in self._scan_frozen:
                widget.setEnabled(True)
            self._scan_frozen = []
            self._apply_pattern_lock()
            self._refresh_structure_actions()
            return
        frozen: list[QWidget] = []

        def freeze(widget: QWidget) -> None:
            if widget.isEnabled():
                widget.setEnabled(False)
                frozen.append(widget)

        node: QWidget = button
        central = self.centralWidget()
        while node is not central and node.parentWidget() is not None:
            parent = node.parentWidget()
            for sibling in parent.findChildren(
                QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
            ):
                if sibling is not node:
                    freeze(sibling)
            node = parent
        for widget in (self.menuBar(), self._palette_dock, self._files_dock):
            freeze(widget)
        self._scan_frozen = frozen
