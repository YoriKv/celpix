"""The compression preview: decode-in-place, structure scan, smart scan,
promote to slice.

The main view always shows **raw** bytes. The toolbar's compression combo
instead drives a parallel, view-only pipeline run over the current window, whose
result appears in a floating overlay - so the current offset is a probe for
"does a compressed structure start here?", and scrubbing the raw view hunts for
structures.

"Fails to decompress" is therefore the *signal*, not an error: these formats can
only be decoded from a structure's first byte, so a failure means no structure
starts here and the overlay simply hides. A structure that decodes completely
can be promoted into a real slice entry in one click (To Slice), or through the
slice dialog (File > New Slice from View), which is how a preview becomes
editable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QPushButton,
    QWidget,
)

from celpix.core.address import format_hex
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
from celpix.pipeline.scan import WINDOW_GROWTH
from celpix.plugins.base import NO_COMPRESSION, TilemapCodecPlugin, self_delimiting
from celpix.plugins.builtins.lz16 import KEY_LZ16_ROWS
from celpix.project.workspace import default_slice_name, slice_of
from celpix.ui.decompress_overlay import Badge
from celpix.ui.searchable_combo import fill_stage_combo

if TYPE_CHECKING:
    # For the checker only: a mixin is a slice of the window, so its
    # ``self`` is the window, widgets, state and all (see the package
    # docstring). At runtime it is a plain mixin.
    from celpix.ui.main_window.window import MainWindow as _Host
else:
    _Host = object


class CompressionMixin(_Host):
    """The decompression preview, the structure scans and promote-to-slice.

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

    def _on_promote_structure(self) -> None:
        """One click: the structure in view becomes a slice entry.

        Its extent is the structure's own end whenever the scheme has one: the
        terminator or declared size, chased past the view window when it lies
        there (:meth:`_present_pixel_overlay`). Only a scheme with no end marker
        is bounded by the view window, where the decode ran out. No dialog: the
        name is the generated one, and the slice reads its parent the way the
        parent is read now.
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
        self._add_slice(slice_entry)

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
        # holds is kept for the next tile format rather than run here. A map's
        # pixel picker is off the bar altogether, so what it holds says nothing.
        active = compression_id != NO_COMPRESSION and (
            self._doc.is_tilemap or not self._palette_view_active()
        )
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
        """Arm Jump-to-Next / promote-to-slice from the overlay's scan state.

        Jump is armed only while a whole structure (known end) is in view.
        Promote arms on any extent worth recording — that structure, or a
        stream-based scheme's window end — but also needs the view's positions
        to be coordinates the new slice can be anchored at
        (:meth:`_slice_source`).

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
        state around every early exit here): the pixel reading or the tilemap
        one, by what the document is."""
        assert self._doc is not None
        if compression_id is None:
            self._overlay.hide_overlay()
        elif self._doc.is_tilemap:
            self._present_tilemap_overlay(compression_id)
        else:
            self._present_pixel_overlay(compression_id)

    def _present_tilemap_overlay(self, compression_id: str) -> None:
        """The map's own cell bytes unpacked and drawn as cells over its bank.

        The tilemap twin of :meth:`_present_pixel_overlay`: the whole cell
        buffer rather than a view window, since a map is shown entire and has
        no window to cut, decoded under the map's own cell format and composed
        exactly as the canvas composes the map (``expand_cells`` over the bound
        tiles, the block being the cell). Nothing is armed for navigation —
        Jump and Scan read a window the map has not got — so the status line
        says how far the structure reached and no more.
        """
        doc = self._doc
        assert doc is not None
        cfg = doc.tilemap_config
        window = bytes(doc.tilemap_data)
        if cfg is None or not window:
            self._overlay.hide_overlay()
            return
        ctx = PipelineContext()
        ctx.set(KEY_DECOMPRESS_PARTIAL, True)
        inputs = self._preview_config_inputs(compression_id)
        if inputs is None:
            self._overlay.hide_overlay()
            return
        if inputs:
            ctx.set(KEY_INPUTS, inputs[Stage.COMPRESSION])
        try:
            plugin = self._registry.plugin(Stage.COMPRESSION, compression_id)
            raw = plugin.decompress(window, ctx)
            if not raw:
                self._overlay.hide_overlay()
                return
            engine, preset = self._registry.engine_for(
                cfg.interpret_preset_id, TilemapCodecPlugin
            )
            # A copy, so a decode that publishes what it found (a width, a
            # stamp size) does not write it into the document's own context.
            cells = engine.decode(raw, preset.params, doc.tilemap_ctx.copy())
            tiles, layout = pipeline.expand_cells(
                doc, self._registry, cells, self._tilemap_columns()
            )
            image = self._tilemap_grid_image(
                pipeline.compose_tiles(tiles, layout, None)
            )
        except Exception:  # noqa: BLE001 - any failure means "not a structure"
            self._overlay.hide_overlay()
            return
        parts = [
            f"{format_hex(len(raw), None)} B raw from "
            f"{format_hex(len(window), None)} B of cells",
            f"{len(cells)} cells",
        ]
        consumed = ctx.get(KEY_COMPRESSED_SIZE)
        badge = None
        if consumed and ctx.get(KEY_DECOMPRESS_COMPLETE):
            parts.append(f"structure {format_hex(consumed, None)} B")
        elif self_delimiting(plugin, inputs.get(Stage.COMPRESSION)):
            badge = Badge(
                "end not found",
                "The end marker is not in the map's bytes, so the\n"
                "preview stops where they do",
                warning=True,
            )
        self._overlay.show_result(
            image,
            self._pixel_tile_size(),
            doc.view,
            self._grid_settings(),
            f"Decompressed - {plugin.info.name}",
            ", ".join(parts),
            badge,
            block=(*doc.cell_tiles, "row"),
        )

    def _present_pixel_overlay(self, compression_id: str) -> None:
        """The view window's bytes unpacked and drawn as tiles — the overlay for
        a pixel document, laid out through the live arrangement path."""
        assert self._doc is not None
        view = self._doc.view
        window = self._doc.window_bytes(
            self._offset, view.columns * self._view_rows(), view.byte_nudge
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
                plugin=preset.id,
            )
        except Exception:  # noqa: BLE001 - any failure means "not a structure"
            self._overlay.hide_overlay()
            return

        parts = [
            f"{format_hex(len(raw), None)} B raw from "
            f"{format_hex(len(window), None)} B window"
        ]
        consumed = ctx.get(KEY_COMPRESSED_SIZE)
        complete = bool(consumed and ctx.get(KEY_DECOMPRESS_COMPLETE))
        has_end = self_delimiting(plugin, inputs.get(Stage.COMPRESSION))
        badge = None
        pos = self._byte_position()
        if not complete and has_end:
            # The scheme has an end marker and the window cut the structure
            # short of it. Its end is chased past the view by the scan's own
            # rule (``structure_at``), so Jump to Next and the slice gestures
            # take the whole structure rather than the window's piece of it -
            # a strict decode of that piece fails. The chase starts one growth
            # step past the window: the window itself was just decoded above.
            #
            # This runs on the GUI thread, on every refresh where the window
            # cut a structure short, and is bounded only by CHASE_LIMIT: on
            # padding the scheme decodes without erroring it reads the whole
            # limit before giving up. Hence the wait cursor. A window that
            # already reaches the end of the data leaves nothing to chase into.
            data = self._doc.pixel_data
            found = None
            if pos + len(window) < len(data):
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                try:
                    found = pipeline.structure_at(
                        data,
                        plugin,
                        pos,
                        len(window) * WINDOW_GROWTH,
                        inputs=inputs.get(Stage.COMPRESSION),
                        chase=has_end,
                    )
                finally:
                    QApplication.restoreOverrideCursor()
            if found is None:
                badge = Badge(
                    "end not found",
                    "The stream breaks, the data ends, or 1 MiB of it\n"
                    "passes before its end marker: probably not a\n"
                    "structure, or a damaged one",
                    warning=True,
                )
            else:
                consumed, complete = found[1], True
                badge = Badge(
                    "ends past view",
                    "The structure runs on past the view window\n"
                    "The preview shows the part in view\n"
                    "Jump to Next and To Slice take all of it",
                )
        if complete:
            # The structure's own end was found: report its true extent, and
            # arm Jump-to-Next at the byte right after it.
            parts.append(f"structure {format_hex(consumed, None)} B")
            self._structure_extent = (pos, consumed)
            after = pos + consumed
            if after < len(self._doc.pixel_data):
                self._next_structure = after
        elif not has_end:
            # A stream-based scheme has no end to find, in this window or any
            # other - so this is a statement of where the preview ends, not a
            # problem to fix. The window's end is then the only extent on offer,
            # and it is what To Slice and New Slice from View take: `consumed`
            # rather than the raw window size, so the slice ends on a whole
            # packet the way the decoder read it. Jump-to-Next stays off - "the
            # byte after this structure" means nothing when the structure had no
            # end.
            if consumed:
                parts.append(f"slice {format_hex(consumed, None)} B")
                self._structure_extent = (self._byte_position(), consumed)
            badge = Badge(
                "end of view window",
                "The scheme has no end marker, so decoding stops at\n"
                "the view window's end. To Slice bounds the slice\n"
                "there; widen the window or set a length",
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
        its own end (terminator or declared size) for a probe to ever complete.
        Asked with the entry's bound inputs, since for LZW the end code is one."""
        compression_id = self._compression_id()
        if compression_id == NO_COMPRESSION:
            return False
        inputs = self._preview_config_inputs(compression_id) or {}
        return self_delimiting(
            self._registry.plugin(Stage.COMPRESSION, compression_id),
            inputs.get(Stage.COMPRESSION),
        )

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
        # Probe as many compressed bytes as one screenful decompresses to; a
        # structure that runs past the probe is chased to its end, up to
        # CHASE_LIMIT compressed bytes (``structure_at``).
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
        """Progress callback for :func:`~celpix.pipeline.scan.find_next_structure`:
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
