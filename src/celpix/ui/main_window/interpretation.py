"""How the bytes on screen are read: codec, header skip, and arrangement.

The decode axes (the pixel preset, the header skip that decides *which* bytes
the entry is) together with the display axes on the toolbars - block grouping,
fill order, 2D - and the plugin registry they all resolve through.

The load rule that shapes this module: switching the **preset** re-reads nothing,
because it only changes how the same buffer is interpreted, and re-running the
pathway there would pull the file's bytes back over unsaved edits. Changing the
header (or anything else feeding Read/Decompress) genuinely changes which bytes
the entry is, and must load. :func:`_same_bytes` is the test.
"""

from __future__ import annotations

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QToolBar,
    QWidget,
)

from celpix.core.arrangement import (
    ARRANGEMENT_PRESETS,
    ArrangementPreset,
    arrangement_preset_for,
)
from celpix.core.errors import PipelineError, Stage
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import NO_DECOMPRESS
from celpix.project.workspace import (
    Entry,
    EntryKind,
    pixel_config_for,
)
from celpix.ui.undo_commands import (
    PaletteState,
    PixelConfigCommand,
)
from celpix.ui.widgets import (
    ChecklistPopupButton,
    CompactComboBox,
    add_labelled,
    funnel_icon,
    select_combo_data,
    signals_blocked,
)


def _same_bytes(a: PathwayConfig, b: PathwayConfig) -> bool:
    """Would both configs' Read + Decompress produce the same bytes?

    Everything downstream of Decompress - the Interpret preset - only decides how
    those bytes are *read*, so when this holds the loaded buffer is still valid
    and must not be fetched again (see :meth:`InterpretationMixin._pixel_data_for`).
    """
    return (a.source, a.read_id, a.decompress_id) == (
        b.source,
        b.read_id,
        b.decompress_id,
    )


class InterpretationMixin:
    """The codec, header skip and arrangement the bytes are read through.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _pixel_config(self, entry: Entry, preset_id: str, header: int) -> PathwayConfig:
        """``entry``'s pixel pathway config, in this workspace.

        The workspace is what lets a slice of a parent with unsaved edits read
        those edits instead of the stale file (:func:`pixel_config_for`), so every
        config the window builds goes through here rather than calling the factory
        directly and silently losing that.
        """
        return pixel_config_for(
            entry, preset_id, header, self._registry, self._workspace
        )

    def _build_toolbar(self) -> None:
        # Three stacked rows: the codec selects (what the bytes *are*) on top, the
        # tile arrangement (how those tiles are grouped/addressed) directly below
        # it, and the view settings (how they're shown) at the bottom.
        #
        # Placed in the canvas column (above the transform bar) rather than with
        # ``addToolBar``: QMainWindow's toolbar area spans the whole window width,
        # which would cut across the top of the Files/Palette column. Inserting
        # them here keeps every bar over the canvas it describes and leaves the
        # docks the window's full height. Immovable for the same reason the
        # transform bar is — there is no toolbar area to drag them to.
        codecs = QToolBar("Codecs")
        self._codecs_toolbar = codecs  # greyed out wholesale for a missing entry
        arrange = QToolBar("Arrangement")
        self._arrange_toolbar = arrange  # frozen wholesale during a scan
        view = QToolBar("View")
        self._view_toolbar = view  # frozen wholesale during a scan
        for index, bar in enumerate((codecs, arrange, view)):
            bar.setMovable(False)
            bar.layout().setSpacing(10)
            self._canvas_column.insertWidget(index, bar)

        # Which pixel presets the dropdown lists lives on the workspace, so the
        # project file persists it (self._workspace.hidden_pixel_presets); empty
        # means all. It's view-only — pruning the codec picker to the formats the
        # user cares about, without touching how any file is read.
        self._pixel_preset = self._preset_combo(Stage.INTERPRET_PIXEL, "snes-4bpp")
        self._pixel_preset.currentIndexChanged.connect(self._on_pixel_preset_change)
        # End a format-cycling run when focus leaves the dropdown: the next switch
        # then re-anchors on the live position rather than the stale target.
        self._pixel_preset.focus_lost.connect(self._end_pixel_switch_run)
        self._pixel_preset.setToolTip("Tile graphics format")
        pixel_label = QLabel("Pixel:")
        pixel_label.setToolTip(self._pixel_preset.toolTip())
        pixel_label.setBuddy(self._pixel_preset)
        codecs.addWidget(pixel_label)
        # The combo and its filter button read as one control: grouped in a tight
        # container (no toolbar gap between them), same height, the button a plain
        # funnel icon that picks up the theme's button-text color.
        self._pixel_filter = ChecklistPopupButton(
            "Filter", self._pixel_filter_items, self._apply_pixel_filter
        )
        self._pixel_filter.setIcon(
            funnel_icon(
                self.palette().color(QPalette.ColorRole.ButtonText),
                ratio=self.devicePixelRatioF(),
            )
        )
        self._pixel_filter.setToolTip("Which formats appear in the dropdown")
        self._pixel_filter.setFixedHeight(self._pixel_preset.sizeHint().height())
        pixel_group = QWidget()
        group = QHBoxLayout(pixel_group)
        group.setContentsMargins(0, 0, 0, 0)
        group.setSpacing(2)
        group.addWidget(self._pixel_preset)
        group.addWidget(self._pixel_filter)
        codecs.addWidget(pixel_group)
        # The palette format combo lives in the palette dock's header, next to
        # the mode it qualifies (_build_palette_dock).

        # Compression preview: the main view stays raw; the chosen Decompress
        # plugin runs over the current window and shows in the floating overlay.
        self._compression = CompactComboBox(0.60)
        self._populate_compression()
        self._compression.currentIndexChanged.connect(self._on_view_change)
        add_labelled(
            codecs,
            "Compression:",
            self._compression,
            "Preview the window decompressed with this codec",
        )

        # Structure navigation for contiguously packed compressed data: hop
        # past the structure in view, or walk forward looking for the next one.
        self._jump_next = QPushButton("Jump to Next")
        self._jump_next.setToolTip("Jump past the structure in view")
        self._jump_next.setEnabled(False)
        self._jump_next.clicked.connect(self._on_jump_next)
        codecs.addWidget(self._jump_next)
        self._scan_button = QPushButton("Scan")
        self._scan_button.setToolTip(
            "Scan for the next compressed structure; click again to stop"
        )
        self._scan_button.setEnabled(False)
        self._scan_button.clicked.connect(self._on_scan)
        codecs.addWidget(self._scan_button)
        # One click promotes the complete structure in view into a decompressed
        # slice entry in the files list - the overlay preview made editable.
        self._promote_button = QPushButton("To Slice")
        self._promote_button.setToolTip(
            "Add the structure in view as a decompressed slice"
        )
        self._promote_button.setEnabled(False)
        self._promote_button.clicked.connect(self._on_promote_structure)
        codecs.addWidget(self._promote_button)

        # Manual header skip for headered ROMs: when checked, the first N file
        # bytes are ignored - the view and every offset start after the header
        # (so bank-address formats line up with the ROM proper), and saves
        # splice back after it. 512 B default = copier headers; iNES is 16 B.
        self._headered = QCheckBox("Header")
        self._headered.setToolTip("Skip a file header; offsets start after it")
        self._headered.toggled.connect(self._on_header_change)
        view.addWidget(self._headered)
        self._header_len = self._spin(0, 0x10000, 512, self._on_header_change)
        self._header_len.setToolTip("Header size (512 = copier, 16 = iNES)")
        self._header_len.setSuffix(" B")
        # The hint is sized for the 5-digit maximum, but real headers are at
        # most 3 digits - trim the box so the view row stays compact.
        self._header_len.setFixedWidth(int(self._header_len.sizeHint().width() * 0.84))
        view.addWidget(self._header_len)

        self._columns = self._spin(1, 64, 16, self._on_view_change)
        add_labelled(view, "Cols:", self._columns, "Tiles per row")

        # How many tile-rows the window shows - the "render N rows" view setting.
        self._rows = self._spin(1, 256, 16, self._on_view_change)
        add_labelled(view, "Rows:", self._rows, "Tile rows shown")
        # Cols maxes at 2 digits, rows at 3, so their hints differ - pin both
        # to the rows hint so the pair reads as a matched set.
        rows_width = self._rows.sizeHint().width()
        self._columns.setFixedWidth(rows_width)
        self._rows.setFixedWidth(rows_width)

        self._zoom = self._spin(1, 16, 4, self._on_view_change)
        add_labelled(
            view,
            "Zoom:",
            self._zoom,
            "Screen pixels per image pixel",
        )

        # Range 255: enough rows for a 512-entry palette under a 2-color (1bpp)
        # index space; the view refresh clamps to the loaded palette anyway.
        self._subpalette = self._spin(0, 255, 0, self._on_view_change)
        add_labelled(
            view,
            "Subpal:",
            self._subpalette,
            "Which block of palette entries tiles index into",
        )

        # The Selection Shape picker (what a canvas drag selects) lives on the
        # canvas transform toolbar - see :mod:`celpix.ui.main_window.transform` -
        # because it gates that bar's block transforms.

        # Arrangement (display-only placement/addressing, so these re-render like
        # zoom/grid - not undoable). Block W×H groups tiles into blocks; Order sets
        # how each block fills; 2D reads the source as one wide bitmap Cols across.
        # These share the codecs bar's second row (see _build_toolbar) rather than
        # the view row.
        #
        # Pattern names documented block/order/2D combinations and, like the Offset
        # format picker, fills + locks the individual controls when a preset is
        # chosen; "Custom" unlocks them so they can be hand-edited.
        self._pattern = CompactComboBox(0.60)
        for preset in ARRANGEMENT_PRESETS:
            self._pattern.addItem(preset.name, preset)
        self._pattern.addItem("Custom", "custom")
        self._pattern.setToolTip(
            "Arrangement preset; pick Custom to edit these yourself"
        )
        self._pattern.currentIndexChanged.connect(self._on_pattern_change)
        add_labelled(arrange, "Pattern:", self._pattern, self._pattern.toolTip())

        self._block_cols = self._spin(1, 64, 1, self._on_view_change)
        self._block_rows = self._spin(1, 256, 1, self._on_view_change)
        self._block_cols.setFixedWidth(rows_width)
        self._block_rows.setFixedWidth(rows_width)
        self._block_rows.setToolTip("Tiles per block, down")
        add_labelled(arrange, "Block:", self._block_cols, "Tiles per block, across")
        # The "x" between the pair belongs to both, so it carries the whole
        # control's sense rather than either side's half.
        times = QLabel("\u00d7")
        times.setToolTip("Block size, in tiles")
        arrange.addWidget(times)
        arrange.addWidget(self._block_rows)
        self._block_order = QComboBox()
        self._block_order.setToolTip(
            "How each block fills:\n"
            "• Row - left to right, then down\n"
            "• Column - top to bottom, then right\n"
            "• Row-interleave - a tile-row across every block"
        )
        for label, data in (
            ("Row", "row"),
            ("Column", "column"),
            ("Row-interleave", "row-interleave"),
        ):
            self._block_order.addItem(label, data)
        self._block_order.currentIndexChanged.connect(self._on_view_change)
        add_labelled(arrange, "Order:", self._block_order, self._block_order.toolTip())
        self._two_d = QCheckBox("2D")
        self._two_d.setToolTip("Read as one wide bitmap, not back-to-back tiles")
        self._two_d.toggled.connect(self._on_view_change)
        arrange.addWidget(self._two_d)
        # The default view is Linear (the first preset), so start with the block
        # controls locked until Custom is picked.
        self._apply_pattern_lock()

    @property
    def _arrangement_controls(self) -> tuple[QWidget, ...]:
        """The individual block/order/2D widgets a Pattern preset drives."""
        return (self._block_cols, self._block_rows, self._block_order, self._two_d)

    def _apply_pattern_lock(self) -> None:
        """Enable the individual arrangement controls only under Custom; a named
        preset owns them, so they're read-only while one is selected."""
        custom = self._pattern.currentData() == "custom"
        for widget in self._arrangement_controls:
            widget.setEnabled(custom)

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
        starting point). Either way, re-render."""
        data = self._pattern.currentData()
        if isinstance(data, ArrangementPreset):
            self._set_arrangement(
                data.block_columns,
                data.block_rows,
                data.block_order,
                data.two_dimensional,
            )
        self._apply_pattern_lock()
        self._on_view_change()

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
        self._apply_pattern_lock()

    def _header_offset(self) -> int:
        """File bytes to skip before data begins (0 while 'Header' is unchecked)."""
        return self._header_len.value() if self._headered.isChecked() else 0

    def _on_header_change(self, *_args) -> None:
        entry = self._workspace.current
        if self._doc is None or entry is None or self._applying_undo:
            return
        if entry.kind is not EntryKind.FILE:
            return  # header skip is FILE state; the widgets are disabled anyway
        # The effective skip folds the checkbox and the length spin into one
        # number, so "toggle" and "length edit" are the same command; an edit
        # while unchecked (or an uncheck with no skip applied) changes nothing.
        old_header = self._doc.pixel_config.source.offset
        header = self._header_offset()
        if header == old_header:
            return
        preset_id = self._pixel_preset_id()
        before = (preset_id, old_header, self._byte_position())
        cfg = self._pixel_config(entry, preset_id, header)
        try:
            px = self._pixel_data_for(cfg)  # a moved header really does re-read
        except PipelineError as exc:
            self._report(exc)
            # The doc never changed - snap the widgets back onto its config.
            self._sync_header_widgets(old_header)
            return
        self._push_command(
            PixelConfigCommand(
                self,
                entry,
                "change header",
                before=before,
                after=(preset_id, header, self._byte_position()),
                preloaded=px,
            )
        )

    def _sync_header_widgets(self, header_offset: int) -> None:
        """Snap the header checkbox + length spin to ``header_offset``, signals
        blocked - seeds them on a config apply and reverts them when a header
        change fails to load. A zero offset just unticks the box and leaves the
        spin's last value, so re-ticking restores the previous skip length."""
        with signals_blocked(self._headered, self._header_len):
            self._headered.setChecked(header_offset > 0)
            if header_offset:
                self._header_len.setValue(header_offset)

    def _preset_combo(self, stage: Stage, default_suffix: str) -> QComboBox:
        # Compact: preset names are long and the combo shares a row with other
        # controls, so the closed button takes 3/4 of its natural width; the
        # popup stays full.
        combo = CompactComboBox(0.60)
        for preset in sorted(self._registry.presets(stage), key=lambda p: p.name):
            combo.addItem(preset.name, preset.id)
            if preset.id.endswith(default_suffix):
                combo.setCurrentIndex(combo.count() - 1)
        return combo

    # -- pixel-format filter ----------------------------------------------
    def _all_pixel_presets(self) -> list:
        """Every pixel preset the registry offers, name-sorted (the dropdown's
        natural order and the filter list's order)."""
        return sorted(
            self._registry.presets(Stage.INTERPRET_PIXEL), key=lambda p: p.name
        )

    def _fill_pixel_combo(self, select_id: str) -> None:
        """Repopulate the pixel dropdown with the un-hidden presets, always
        keeping ``select_id`` present and selected.

        The selected format is force-included even when the filter hides it: you
        can't hide the format actually interpreting the file, and every apply
        path (a switch, an undo, a session restore) lands here so that invariant
        holds. Signals stay blocked — the caller owns any reinterpretation.
        """
        hidden = self._workspace.hidden_pixel_presets
        visible = [
            preset
            for preset in self._all_pixel_presets()
            if preset.id not in hidden or preset.id == select_id
        ]
        with signals_blocked(self._pixel_preset):
            self._pixel_preset.clear()
            for preset in visible:
                self._pixel_preset.addItem(preset.name, preset.id)
            index = self._pixel_preset.findData(select_id)
            self._pixel_preset.setCurrentIndex(index if index >= 0 else 0)

    def _pixel_filter_items(self) -> list[tuple[str, str, bool]]:
        """``(id, name, checked)`` for every pixel preset — the filter popup's
        model. The format in force always reads as checked (it can't be hidden)."""
        current = self._pixel_preset_id()
        hidden = self._workspace.hidden_pixel_presets
        return [
            (
                preset.id,
                preset.name,
                preset.id not in hidden or preset.id == current,
            )
            for preset in self._all_pixel_presets()
        ]

    def _apply_pixel_filter(self, desired: set[str]) -> set[str]:
        """Set which pixel presets the dropdown lists; return the set in force.

        ``desired`` is the ids left checked. The list can never be emptied, so an
        empty request keeps the current format; unchecking the *current* format
        switches the view to the first remaining one. When that switch can't take
        (no document, or the bytes don't fit the new codec) nothing changes. The
        returned set is what actually ended up checked, so the popup can spring a
        clamped request back.
        """
        all_ids = {preset.id for preset in self._all_pixel_presets()}
        current = self._pixel_preset_id()
        desired = (set(desired) & all_ids) or {current}
        if current not in desired:
            target = next(
                (p.id for p in self._all_pixel_presets() if p.id in desired), None
            )
            if target is None or not self._try_switch_pixel(target):
                return all_ids - self._workspace.hidden_pixel_presets  # unchanged
        self._workspace.hidden_pixel_presets = all_ids - desired
        self._fill_pixel_combo(self._pixel_preset_id())
        # The filter is project state now, so changing it can dirty the project.
        self._refresh_project_modified()
        return desired

    def _try_switch_pixel(self, target: str) -> bool:
        """Move the dropdown to ``target`` and reinterpret through it, as an
        ordinary undoable switch. With no document open there is nothing to read,
        so it just moves the default selection and reports success."""
        self._fill_pixel_combo(target)
        if self._doc is None:
            return True
        return self._on_pixel_preset_change()

    @staticmethod
    def _spin(low: int, high: int, value: int, on_change) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setValue(value)
        # Commit on Enter / focus-out / stepping, not on every keystroke, so typing
        # a multi-digit value doesn't re-render (and re-clamp) for each character.
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(on_change)
        return spin

    # -- current selections ------------------------------------------------
    def _pixel_preset_id(self) -> str:
        return self._pixel_preset.currentData()

    def _palette_preset_id(self) -> str:
        return self._palette_preset.currentData()

    def _compression_id(self) -> str:
        """The compression-preview combo's plugin id, pass-through by default.

        The fallback matters before the combo is populated (session seeding runs
        during construction) and after a plugin refresh drops the selected
        scheme, both of which leave ``currentData()`` empty.
        """
        return self._compression.currentData() or NO_DECOMPRESS

    def _pixel_bpp(self) -> int:
        return pipeline.pixel_bpp(self._pixel_preset_id(), self._registry)

    def _index_space(self, preset_id: str | None = None) -> int:
        """The pixel format's color count - the subpalette row size.

        Capped at 256: a direct-color preset's bpp can be up to 32, and both
        the palette maths and the fallback palette top out at 256 entries. The
        bpp comes from the resolved codec's geometry (:func:`pipeline.pixel_bpp`),
        so a preset with no ``bpp`` param - a wide/odd-tile codec, a code format -
        is sized correctly rather than crashing on a missing key.

        Defaults to the currently selected preset; pass ``preset_id`` to size
        another format's index space (e.g. _apply_pixel_config's outgoing preset).
        A stale id (preset removed by a plugin refresh) falls back to the
        current preset rather than failing the reload.
        """
        if preset_id is not None:
            try:
                bpp = pipeline.pixel_bpp(preset_id, self._registry)
                return min(256, 1 << bpp)
            except (KeyError, PipelineError):
                pass
        return min(256, 1 << self._pixel_bpp())

    def _pixel_tile_size(self) -> tuple[int, int]:
        # The atomic tile size is the codec's (recorded on the document at load) - not
        # a preset field (geometry is the engine's fixed unit; display grouping into
        # larger tiles is a separate view option, not yet implemented).
        if self._doc is not None:
            return self._doc.tile_width, self._doc.tile_height
        return 8, 8

    def _store_pixel_data(self, px: pipeline.PixelData, cfg: PathwayConfig) -> None:
        """Update the open document's pixel bytes + geometry from a fresh load."""
        assert self._doc is not None
        self._doc.pixel_data = px.data
        self._doc.bytes_per_tile = px.bytes_per_tile
        self._doc.tile_width = px.tile_width
        self._doc.tile_height = px.tile_height
        self._doc.pixel_config = cfg
        self._doc.pixel_ctx = px.ctx
        if not self._palette_mode.is_real:
            self._doc.palette = self._fallback_palette()

    def _on_pixel_preset_change(self) -> bool:
        """The pixel combo changed: validate the new interpretation, then push
        one undoable command whose first redo applies the pre-validated load.

        Anchor on the target from the first switch of this run, if one is live,
        so a series of switches all measure from the same intended position
        instead of from wherever the previous format's clamping happened to
        land. The first switch has none yet, so it seeds it from the live view.

        Returns whether the switch went through — False on an early bail (no
        document) or a load failure (already reported, combo reverted), which
        the filter uses to leave its own state untouched when a switch it drove
        can't take.
        """
        entry = self._workspace.current
        if self._doc is None or entry is None or self._applying_undo:
            return False
        if self._pixel_switch_target is None:
            self._pixel_switch_target = self._byte_position()
        # The doc still holds the outgoing interpretation here (only the combo
        # has moved), so the undo state reads straight off it.
        old_preset = self._doc.pixel_config.interpret_preset_id
        old_header = (
            self._doc.pixel_config.source.offset
            if entry.kind is EntryKind.FILE
            else self._header_offset()  # ignored for slices
        )
        before = (old_preset, old_header, self._byte_position())
        preset_id = self._pixel_preset_id()
        header = self._header_offset()
        # Rebuild from the entry, not the old config: a slice keeps its bounds
        # and codec ids, and a file re-derives the header skip.
        cfg = self._pixel_config(entry, preset_id, header)
        try:
            px = self._pixel_data_for(cfg)
        except PipelineError as exc:
            self._report(exc)
            # The doc never switched - snap the combo back onto its preset.
            select_combo_data(self._pixel_preset, old_preset)
            return False
        self._push_command(
            PixelConfigCommand(
                self,
                entry,
                f"switch pixel format to {self._pixel_preset.currentText()}",
                before=before,
                after=(preset_id, header, self._pixel_switch_target),
                preloaded=px,
            )
        )
        note = self._partial_tile_note()
        if note:
            self.statusBar().showMessage(f"Preset changed - {note}")
        return True

    def _pixel_data_for(
        self, cfg: PathwayConfig, *, reload: bool = False
    ) -> pipeline.PixelData:
        """``cfg``'s pixel bytes + geometry, going to disk only when it must.

        A pixel-format switch changes how the same bytes are *read as* tiles, not
        which bytes they are - so the live buffer is reinterpreted in place.
        Re-running the pathway there would pull the file's own bytes back over
        unsaved edits, silently undoing them. A header change (or any other
        change to the source, Read or Decompress ids) genuinely moves which bytes
        the entry is, and has to load; ``reload`` forces that for a plugin
        refresh, whose whole point is to re-run the reloaded plugins.
        """
        live = self._doc
        if not reload and live is not None and _same_bytes(live.pixel_config, cfg):
            return pipeline.reinterpret_pixel_data(
                live.pixel_data, live.pixel_ctx, cfg, self._registry
            )
        return pipeline.load_pixel_data(cfg, self._registry)

    def _apply_pixel_config(
        self,
        preset_id: str,
        header_offset: int,
        byte_position: int,
        preloaded: pipeline.PixelData | None = None,
        *,
        reload: bool = False,
    ) -> bool:
        """Re-interpret the current entry's bytes and land on ``byte_position``.

        The one application path for preset switches, header changes, plugin
        refreshes and their undos: syncs the codec widgets (signals blocked,
        the _restore_session pattern) and never pushes a command. ``preloaded``
        carries a push site's already-validated result; without it the pathway
        re-runs here (through :meth:`_pixel_data_for`, so a mere reinterpretation
        keeps unsaved edits), and a failure (reported) leaves the view untouched.

        The view offset is a tile index, so it maps to a different *byte*
        position under a new bytes-per-tile - ``byte_position`` re-lands the
        view exactly, with the sub-tile remainder becoming the byte nudge. The
        subpalette row is likewise re-anchored: the same row index means a
        different palette base under the new color count, so it is recomputed
        from the selected color (or the old base) to keep pointing at the
        same palette entries.
        """
        entry = self._workspace.current
        if self._doc is None or entry is None:
            return False
        old_group = self._index_space(self._doc.pixel_config.interpret_preset_id)
        cfg = self._pixel_config(entry, preset_id, header_offset)
        if preloaded is not None:
            px = preloaded
        else:
            try:
                px = self._pixel_data_for(cfg, reload=reload)
            except PipelineError as exc:
                self._report(exc)
                return False
        # Rebuild rather than a plain select: the applied format may be one the
        # filter hides, and you can never hide the format actually in force.
        self._fill_pixel_combo(preset_id)
        if entry.kind is EntryKind.FILE:
            self._sync_header_widgets(header_offset)
        self._store_pixel_data(px, cfg)
        # _refresh_view clamps the offset; the nudge stays < the new tile size.
        self._offset, self._nudge = divmod(byte_position, px.bytes_per_tile)
        anchor = self._palette_panel.selected_index()
        if anchor is None:
            anchor = self._subpalette.value() * old_group
        # Signals blocked: _refresh_view below re-renders (and re-clamps) once.
        with signals_blocked(self._subpalette):
            self._subpalette.setValue(anchor // self._index_space())
        self._clear_selection()  # the same tile index covers different bytes now
        self._refresh_view()
        return True

    def _end_pixel_switch_run(self) -> None:
        """Drop the scratch target when the pixel dropdown loses focus.

        The target only spans one uninterrupted bout of format-cycling; once the
        user moves on, the current view *is* the position, so the next switch
        should re-anchor there rather than resurrect a stale byte offset.
        """
        self._pixel_switch_target = None

    def _refresh_plugins(self) -> None:
        """Developer aid: reload plugins from disk and re-run on the open file.

        Rebuilds the registry (picking up added/changed/removed presets and code
        plugins - a changed code plugin passes the trust gate; one you approved this
        run reloads without a prompt), refreshes the preset menus, and re-decodes the
        currently open pixel/palette through the reloaded plugins.

        The pixel re-run goes back to disk so a reloaded Read/Decompress plugin
        is exercised too - except on an entry with unsaved edits, which live only
        in the loaded bytes and a re-read would throw away. There the refresh
        reinterprets what is in memory, so a changed *codec* still takes effect
        and the edits survive; a re-read happens on the entry's next load.
        """
        if self._reload_plugins is None:
            return
        entry = self._workspace.current
        self._registry, self._plugin_issues = self._reload_plugins()
        self._repopulate_presets()
        if self._doc is not None:
            # Re-decode the open file's sources through the new registry - via
            # the application paths, never commands: a plugin refresh isn't an
            # edit and must not pollute the undo history.
            self._apply_pixel_config(
                self._pixel_preset_id(),
                self._header_offset(),
                self._byte_position(),
                reload=entry is None or not entry.pixel_dirty,
            )
            # Only a palette with an external source can be re-decoded; a
            # generated default or a project-stored custom palette has no bytes
            # to re-read (its config points at an empty path).
            if self._palette_mode.has_source:
                result = self._reinterpret_palette()
                if result is not None:
                    loaded, cfg = result
                    self._apply_palette_state(
                        PaletteState(
                            cfg.interpret_preset_id,
                            self._palette_mode,
                            loaded.palette,
                            cfg,
                            loaded.ctx,
                            data=loaded.data,
                        )
                    )

        parts = ["Plugins refreshed"]
        if self._doc is not None:
            parts.append("re-ran on current file")
        self.statusBar().showMessage("; ".join(parts) + ".")
        # Any plugin that failed the reload is a warning, surfaced modally.
        self._alert_plugin_issues()

    def _repopulate_presets(self) -> None:
        """Rebuild the preset combos from the (reloaded) registry, keeping the
        current selection when it still exists."""
        # The pixel combo goes through the filter (a refresh keeps hidden formats
        # hidden and lets newly added ones through); the selection is preserved.
        self._fill_pixel_combo(self._pixel_preset.currentData())
        current = self._palette_preset.currentData()
        # Block signals so repopulating doesn't fire a reload per item; the
        # refresh does one explicit reload afterwards.
        with signals_blocked(self._palette_preset):
            self._palette_preset.clear()
            for preset in sorted(
                self._registry.presets(Stage.INTERPRET_PALETTE), key=lambda p: p.name
            ):
                self._palette_preset.addItem(preset.name, preset.id)
            index = self._palette_preset.findData(current)
            self._palette_preset.setCurrentIndex(index if index >= 0 else 0)
        # The compression combo lists Decompress *plugins*, not presets, but
        # refreshes the same way (keep the selection when it survives the reload).
        current = self._compression.currentData()
        with signals_blocked(self._compression):
            self._compression.clear()
            self._populate_compression()
            index = self._compression.findData(current)
            if index >= 0:
                self._compression.setCurrentIndex(index)

    def _partial_tile_note(self) -> str:
        """Status-bar warning when the data ends mid-tile, or ``""`` when aligned.

        Not an error: the trailing partial tile renders zero-padded, so the file
        stays viewable - the note just explains the padded tail.
        """
        assert self._doc is not None
        short = -len(self._doc.pixel_data) % self._doc.bytes_per_tile
        if not short:
            return ""
        return (
            f"data ends {short} byte(s) short of a whole "
            f"{self._doc.bytes_per_tile}-byte tile; the last tile is zero-padded"
        )
