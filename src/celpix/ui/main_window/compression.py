"""The compression preview: decode-in-place, structure scan, promote to slice.

The main view always shows **raw** bytes. The toolbar's compression combo
instead drives a parallel, view-only pipeline run over the current window, whose
result appears in a floating overlay - so the current offset is a probe for
"does a compressed structure start here?", and scrubbing the raw view hunts for
structures.

"Fails to decompress" is therefore the *signal*, not an error: these formats can
only be decoded from a structure's first byte, so a failure means no structure
starts here and the overlay simply hides. A structure that decodes completely
can be promoted into a real slice entry, which is how a preview becomes
editable.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QApplication,
)

from celpix.core.arrangement import (
    BlockLayout,
)
from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.pipeline import pipeline
from celpix.plugins.base import NO_COMPRESSION
from celpix.plugins.builtins.lz16 import KEY_LZ16_ROWS
from celpix.project.workspace import (
    default_slice_name,
    slice_of,
)
from celpix.ui.decompress_overlay import Badge
from celpix.ui.undo_commands import (
    AddEntryCommand,
)


class CompressionMixin:
    """The decompression preview, structure scan, and promote-to-slice.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _populate_compression(self) -> None:
        """Fill the compression combo from the registry, in registration order
        (the built-ins group naturally: none first, then the LZ family)."""
        for plugin in self._registry.plugins(Stage.COMPRESSION):
            self._compression.addItem(plugin.info.name, plugin.info.id)
            if plugin.info.id == NO_COMPRESSION:
                self._compression.setCurrentIndex(self._compression.count() - 1)

    def _on_promote_structure(self) -> None:
        """One click: the structure in view becomes a slice entry.

        Its extent is whichever end the decode found — the structure's own
        terminator, or, for a scheme that has none, the point the view window
        ran out at.
        """
        src = self._slice_source()
        if src is None or self._structure_extent is None:
            return
        entry, _doc = src
        start, consumed = self._structure_extent
        abs_off = self._slice_prefill_offset(start)
        compression_id = self._compression_id()
        slice_entry = slice_of(
            entry,
            default_slice_name(abs_off, consumed, compression_id),
            abs_off,
            consumed,
            compression_id,
        )
        self._seed_slice_from_parent(slice_entry)
        self._push_command(
            AddEntryCommand(self, slice_entry, f'new slice "{slice_entry.name}"')
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
        active = compression_id != NO_COMPRESSION
        self._scan_button.setEnabled(active and not self._scanning)
        self._next_structure = None
        self._structure_extent = None
        try:
            self._present_overlay(compression_id if active else None)
        finally:
            self._refresh_structure_actions()

    def _refresh_structure_actions(self) -> None:
        """Arm Jump-to-Next / promote-to-slice from the overlay's scan state.

        Jump is armed only while a whole structure (known end) is in view.
        Promote arms on any extent worth recording — that structure, or a
        stream-based scheme's window end — but also needs the view's positions
        to be coordinates the new slice can be anchored at, so it stays off while
        the main view is itself showing decompressed bytes.

        Kept separate from :meth:`_refresh_overlay` because it is the single
        source of truth for these two buttons: a scan's blanket UI re-enable
        (:meth:`_set_scan_ui`) must restore their real state through here rather
        than leave them switched on.
        """
        self._jump_next.setEnabled(self._next_structure is not None)
        self._promote_button.setEnabled(
            self._structure_extent is not None and self._slice_source() is not None
        )

    def _present_overlay(self, compression_id: str | None) -> None:
        """The overlay body of :meth:`_refresh_overlay` (which owns the button
        state around every early exit here)."""
        assert self._doc is not None
        if compression_id is None:
            self._overlay.hide_overlay()
            return
        view = self._doc.view
        window = self._doc.window_bytes(
            view.tile_offset, view.columns * view.rows, view.byte_nudge
        )
        if not window:
            self._overlay.hide_overlay()
            return
        ctx = PipelineContext()
        ctx.set(KEY_DECOMPRESS_PARTIAL, True)
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
            # and it is what To Slice promotes: `consumed` rather than the raw
            # window size, so the slice ends on a whole packet the way the
            # decoder read it. Jump-to-Next stays off - "the byte after this
            # structure" means nothing when the structure had no end.
            if consumed:
                parts.append(f"slice {consumed:#x} B")
                self._structure_extent = (self._byte_position(), consumed)
            badge = Badge(
                "end of view window",
                "This scheme is a plain stream with no end marker, so it\n"
                "decodes exactly as far as the view window reaches and\n"
                "no further - the picture is a prefix, not a structure.\n"
                "To Slice bounds the slice at that same point; widen the\n"
                "window for more, or set the true length from whatever\n"
                "container recorded it.",
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

    def _on_jump_next(self) -> None:
        if self._doc is None or self._next_structure is None:
            return
        self._set_byte_position(self._next_structure)

    def _on_scan(self) -> None:
        """Scan forward for the next decodable structure (re-entered by Stop).

        Runs inline, pumping the event loop between batches so Stop stays
        clickable; everything else is frozen by :meth:`_set_scan_ui`. A hit is
        a *complete*, non-empty structure under a strict decode - a
        best-effort partial decode "succeeds" on almost any bytes, so it can't
        be the criterion (which also means non-self-delimiting schemes like
        LZ16 are effectively unscannable - there is nothing in the stream to
        recognise).
        """
        if self._scanning:
            self._scan_stop = True
            return
        if self._doc is None:
            return
        compression_id = self._compression_id()
        if compression_id == NO_COMPRESSION:
            return
        plugin = self._registry.plugin(Stage.COMPRESSION, compression_id)
        data = self._doc.pixel_data
        # Probe as many compressed bytes as one screenful decompresses to: a
        # structure bigger than the view can show is not worth confirming here.
        probe_bytes = max(
            1,
            self._columns.value() * self._rows.value() * self._doc.bytes_per_tile,
        )
        self._scanning = True
        self._scan_stop = False
        self._set_scan_ui(True)
        try:
            result = pipeline.find_next_structure(
                data,
                plugin,
                probe_bytes,
                self._byte_position() + 1,
                on_tick=self._scan_tick,
            )
        finally:
            self._scanning = False
            self._set_scan_ui(False)
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

    def _set_scan_ui(self, active: bool) -> None:
        """Swap Scan⇄Stop and freeze the rest of the UI while a scan runs."""
        self._scan_button.setText("Stop" if active else "Scan")
        for widget in (
            self.menuBar(),
            self.centralWidget(),
            self._palette_dock,
            self._files_dock,
            self._arrange_toolbar,
            self._view_toolbar,
            self._pixel_preset,
            self._pixel_filter,
            self._compression,
            self._jump_next,
            self._promote_button,
        ):
            widget.setEnabled(not active)
        # The blanket re-enable above must not resurrect controls that the
        # current entry keeps off (the block controls stay locked unless the
        # Pattern picker is on Custom, and Jump / promote are armed only by the
        # overlay's structure state, not by a scan ending).
        current = self._workspace.current
        if not active and current is not None:
            self._apply_pattern_lock()
            self._refresh_structure_actions()
