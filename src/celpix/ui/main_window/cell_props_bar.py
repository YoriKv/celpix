"""The per-cell property row: the attributes a format's cells carry, editable.

The row above the binding controls under a tilemap's canvas. Its controls are
**generated from the format's own field table**
(:meth:`~...tilemap_edit.TilemapEditMixin._cell_fields`): a format with mirror
bits grows two checkboxes, one with a priority bit grows a third, and a bare
index map grows nothing and the row hides — the bar's rule that a control
meaning nothing here is not a feature switched off. The index and the palette
row are deliberately not in it: each already has an editor with semantics of
its own (the Cell spin beside it; Palette Row and the pin gesture), and a second
control over the same field would fight the first.

**One row, two targets.** In tile mode it reads and writes the *selection*:
a uniform value shows plainly, differing values show as a third state — a
part-checked box, a spin showing ``—`` — and an edit sets every selected cell
in one undo step (:meth:`~...tilemap_edit.TilemapEditMixin._set_cell_property`).
With **Edit Tiles** armed it retargets to the *held stamp*, where what it
holds decides what an edit means:

- A tile picked in the **sheet** has no record behind it, so the row *is*
  the record: the stamp's **settings**
  (:attr:`~...stamp_tool.StampToolMixin._stamp_attrs`), concrete per-field
  values the landing lays whole (:meth:`~...stamp_tool.StampToolMixin.
  _stamp_cell`) — exactly what a canvas-swept brush lays, so the two picks
  cannot disagree about what a press writes. An untouched field holds the
  format's default; a pick that displaces a held record takes over that
  record's values (:meth:`_seed_stamp_attrs`), so setting up one tile and
  painting many carries the setup. A brush swept off the sheet is the same
  pick widened, so it lands the same way.
- An **eyedropped cell** is a whole record and lays down whole, so the row
  shows the record and an edit rewrites it in place.
- A **canvas-swept brush** is many records: differing values show the third
  state, and an edit sets them all — flipping each record, or mirroring the
  brush whole, is the transform bar's job instead
  (:meth:`~...stamp_tool.StampToolMixin._stamp_transform_tiles` and the Block
  pair's :meth:`~...stamp_tool.StampToolMixin._stamp_transform_block`), so
  every gesture stays reachable.

Stamp-mode edits are session state like Palette Row — no undo step; the stroke
that lands them is the step — and every one re-renders the stamp preview, so
what the ghost shows is what a press lays. Every declared field stays on the
row while the tool is armed: Edit Tiles is where a stamp layout's editing
happens at all, and its only fields are the drawn bit and the flags — hiding
them left the row empty exactly where it was needed. One of them reads
specially there. **Drawn** is a stamp-wide setting whatever is held, because
the stamp otherwise forces the bit: checked is that default, and unchecking
it turns the stamp into the **eraser** — every press lays undrawn cells, the
inverse Clear Cells performs on a selection. It is also the one field a
displaced record does not seed: the eraser is armed by this box alone, never
inherited from a cell that happened to be undrawn.

Gated in place rather than by the blanket pass, exactly as the Cell spin is
and for its reason: the pass runs later and its all-or-nothing visibility
would put the row back on a sprite object
(:mod:`~celpix.ui.main_window.capability_sync`). Synced from the refresh
cycle, the selection pass (a selection moves without a render) and every
pick (a pick changes what the row describes without either).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QSpinBox, QWidget

from celpix.core.capabilities import Capability
from celpix.core.tilemap import BLANK, Cell
from celpix.ui.widgets import add_labelled, hex_spin, signals_blocked, value_spin


@dataclass(frozen=True)
class _PropSpec:
    """One cell attribute the row can grow a control for.

    ``said`` is the attribute named for a status-bar sentence, lowercase.
    """

    field: str
    caption: str
    said: str
    tip: str


# In row order. A field the format does not declare is skipped at build time,
# which is the whole of how the row adapts — nothing here asks per format.
_PROPERTY_SPECS = (
    _PropSpec(
        "flip_h",
        "Flip H",
        "horizontal flip",
        "Mirror the selected cells' tiles left-right\n"
        "In Edit Tiles mode: how the next stamp lands",
    ),
    _PropSpec(
        "flip_v",
        "Flip V",
        "vertical flip",
        "Mirror the selected cells' tiles top-bottom\n"
        "In Edit Tiles mode: how the next stamp lands",
    ),
    _PropSpec(
        "priority",
        "Priority",
        "priority",
        "The cell's priority, carried for the console\n"
        "celPix draws no layers, so the picture does not change",
    ),
    _PropSpec(
        "visible",
        "Drawn",
        "drawn flag",
        "Whether this position is drawn at all\n"
        "Off, it paints the background - Clear Cells clears it too\n"
        "In Edit Tiles mode: off makes the stamp an eraser,\n"
        "laying undrawn cells",
    ),
    _PropSpec(
        "ends_line",
        "Line end",
        "line end",
        "The bit this text format sets on a line's last character\n"
        "View > Text shows the break it makes",
    ),
    _PropSpec(
        "flags",
        "Flags",
        "flags",
        "Bits this format has that celPix does not interpret\n"
        "Carried byte-exact; edit only if you know the format",
    ),
)
_SPEC_BY_FIELD = {spec.field: spec for spec in _PROPERTY_SPECS}


class CellPropsMixin:
    """The property row's build, sync and write slots.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object. See the module docstring for the two targets and for
    what an edit means against each.
    """

    # -- construction --------------------------------------------------------
    def _build_cell_props_row(self, layout: QHBoxLayout) -> None:
        """Seed the row's state; the controls come later, per format."""
        self._cell_props_layout = layout
        self._cell_props_widgets: dict[str, QWidget] = {}
        self._cell_props_labels: dict[str, QLabel] = {}
        # What the controls were built for: ((field, limit), ...) sorted, or ()
        # for no row. Rebuilding only when this moves is what keeps the sync
        # cheap enough to run from the selection pass.
        self._cell_props_signature: tuple = ()

    def _rebuild_cell_props(self, signature: tuple) -> None:
        """Tear the row down and grow the controls ``signature`` names.

        The :class:`~celpix.ui.container_dialog.ContainerDialog` rebuild shape:
        everything out, then the new set in row order, then the stretch. Wholesale
        rather than diffed — the row is at most six controls, and a diff would be
        more code than the widgets.
        """
        layout = self._cell_props_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._cell_props_widgets.clear()
        self._cell_props_labels.clear()
        self._cell_props_signature = signature
        fields = dict(signature)
        if not fields:
            return
        for spec in _PROPERTY_SPECS:
            limit = fields.get(spec.field)
            if limit is None:
                continue
            # Both spins' -1 floor is the ``—`` sentinel — display only, shown
            # while a mixed target parks on it. The sync owns the minimum
            # (:meth:`_show_cell_prop_spin`); the build leaves it 0.
            if spec.field == "flags":
                spin = hex_spin(0, limit, spec.tip)
                spin.setSpecialValueText("—")
                spin.valueChanged.connect(partial(self._on_cell_prop_spun, spec.field))
                self._cell_props_labels[spec.field] = add_labelled(
                    layout, f"{spec.caption} ", spin, spec.tip
                )
                self._cell_props_widgets[spec.field] = spin
            elif spec.field == "priority" and limit > 1:
                # One bit is a switch; more is a level. Every BG format in hand
                # is the switch, so the spin is the shape a sprite-word format
                # would grow into rather than one any preset builds today.
                spin = value_spin(
                    0, limit, 0, partial(self._on_cell_prop_spun, spec.field)
                )
                spin.setSpecialValueText("—")
                self._cell_props_labels[spec.field] = add_labelled(
                    layout, f"{spec.caption} ", spin, spec.tip
                )
                self._cell_props_widgets[spec.field] = spin
            else:
                box = QCheckBox(spec.caption)
                box.setToolTip(spec.tip)
                # `clicked` rather than `toggled`: only a user's click may write,
                # and the restores below then need no blocking to stay silent —
                # they still block, as every restore on this bar does, so a
                # future handler on the other signal cannot re-enter.
                box.clicked.connect(partial(self._on_cell_prop_clicked, spec.field))
                layout.addWidget(box)
                self._cell_props_widgets[spec.field] = box
        layout.addStretch(1)

    # -- what the row is pointed at ------------------------------------------
    def _cell_props_target(self) -> str | None:
        """Which store an edit would land in, or None for nothing to edit.

        ``"selection"`` in tile mode. With Edit Tiles armed: ``"brush"`` for a
        canvas-swept rectangle of records, ``"record"`` for a single eyedropped
        cell, ``"attrs"`` for a sheet pick — the one with no record behind it,
        where the row's settings are the record the landing lays. A sheet-swept
        brush is ``"attrs"`` too: its squares are bare indices that land with
        the same settings, so showing its blank records instead would claim a
        write the landing does not make.
        """
        if not self._stamping:
            return "selection"
        brush = self._stamp_brush
        if brush is not None and len(brush):
            return "brush" if self._source_cell is not None else "attrs"
        if self._held_tile_id() is None:
            return None
        return "record" if self._source_cell is not None else "attrs"

    def _cell_prop_values(self, target: str, field: str) -> set:
        """Every value ``field`` takes across ``target`` — one is uniform,
        more is the mixed display."""
        if target == "selection":
            doc = self._doc
            if doc is None or doc.cells is None:
                return set()
            return {getattr(doc.cells[at], field) for at in self._selected_cells()}
        if field == "visible":
            # The drawn bit is the stamp's whatever is held — forced on unless
            # the eraser setting says otherwise — so the record's own bit is
            # not what lands and must not be what shows.
            return {self._stamp_attr_value("visible")}
        if target == "brush":
            brush = self._stamp_brush
            return {
                getattr(brush.get(x, y), field)
                for y in range(brush.height)
                for x in range(brush.width)
            }
        if target == "record":
            return {getattr(self._source_cell, field)}
        return {self._stamp_attr_value(field)}

    # -- the sheet pick's settings ---------------------------------------------
    def _stamp_attr_value(self, field: str) -> bool | int:
        """What a sheet pick lays for ``field``: the row's setting, or the
        format's default for a field never touched (``BLANK`` states them)."""
        return self._stamp_attrs.get(field, getattr(BLANK, field))

    def _stamp_sheet_attrs(self) -> dict[str, bool | int]:
        """Every row-managed field's landing value, for the sheet pick's stamp.

        Concrete across the board — a sheet pick lays these *whole*, exactly as
        a canvas-swept brush lays its records, so the two picks agree about
        what a press writes. Filtered to what the format declares, on the
        row's own pruning grounds: an undeclared bit written here would show
        an attribute the save silently drops.
        """
        fields = self._cell_fields()
        return {
            field: self._stamp_attr_value(field)
            for field in fields
            if field in _SPEC_BY_FIELD
        }

    def _seed_stamp_attrs(self, record: Cell) -> None:
        """Take ``record``'s attributes over as the sheet pick's settings.

        Called where a sheet pick displaces a held record
        (:meth:`~...tile_source_dock.TileSourceDockMixin._set_source_tile` and
        the panel's own pick paths): the record was what the row showed and the
        user tuned, so the next pick starting from it — rather than from the
        defaults — is what keeps "set up one tile, paint many" one gesture per
        tile. Every declared field but **Drawn**: the eraser is armed by its
        box alone, never inherited from a record that happened to be undrawn.
        """
        for field in self._cell_fields():
            if field in _SPEC_BY_FIELD and field != "visible":
                self._stamp_attrs[field] = getattr(record, field)

    # -- sync ----------------------------------------------------------------
    def _sync_cell_props(self) -> None:
        """Converge the row with the format, the mode and the target.

        Rebuilds only when the format's field set moved; everything else is a
        signal-blocked restore. Also where a stamp setting whose field the
        format no longer declares is dropped — a stale one surviving a codec
        switch would land bits the new format cannot store, and the row would
        show a value the landing quietly ignores.
        """
        doc = self._doc
        fields = self._cell_fields() if doc is not None and doc.is_tilemap else {}
        shown = {
            field: limit for field, limit in fields.items() if field in _SPEC_BY_FIELD
        }
        usable = (
            doc is not None
            and doc.cells_editable
            and self._can(Capability.STAMP)
            and bool(shown)
        )
        signature = tuple(sorted(shown.items())) if usable else ()
        if signature != self._cell_props_signature:
            self._rebuild_cell_props(signature)
        for field in [name for name in self._stamp_attrs if name not in shown]:
            del self._stamp_attrs[field]
        if self._stamping:
            # The transform bar targets the same held stamp this row does, so
            # it converges wherever the row does: a pick changes what its
            # buttons act on without a render or a selection pass running
            # (:meth:`~...stamp_tool.StampToolMixin._sync_stamp_transform_actions`).
            self._sync_transform_actions()
        if not usable:
            return
        target = self._cell_props_target()
        armed = target is not None and (
            target != "selection" or bool(self._selected_cells())
        )
        for field, widget in self._cell_props_widgets.items():
            widget.setEnabled(armed)
            label = self._cell_props_labels.get(field)
            if label is not None:
                label.setEnabled(armed)
            values = self._cell_prop_values(target, field) if armed else set()
            with signals_blocked(widget):
                if isinstance(widget, QCheckBox):
                    self._show_cell_prop_check(widget, values)
                else:
                    self._show_cell_prop_spin(widget, values)

    @staticmethod
    def _show_cell_prop_check(box: QCheckBox, values: set) -> None:
        """One box's display: uniform, or the mixed third state.

        Three-state only while the third state is showing: it is a *display*
        of disagreement rather than a value — Qt's click cycle visits the
        partial state, and a click must resolve to a plain yes or no.
        """
        mixed = len(values) > 1
        box.setTristate(mixed)
        if mixed:
            box.setCheckState(Qt.CheckState.PartiallyChecked)
        else:
            box.setCheckState(
                Qt.CheckState.Checked
                if values and bool(next(iter(values)))
                else Qt.CheckState.Unchecked
            )

    @staticmethod
    def _show_cell_prop_spin(spin: QSpinBox, values: set) -> None:
        """One spin's display; ``—`` (the -1 floor) is the mixed reading.

        The floor opens only while the mixed display is parked on it — the
        minimum stays 0 everywhere else, because -1 is a sentinel the write
        slot ignores, and a value a user can enter but that never lands reads
        as a broken field.
        """
        uniform = len(values) == 1
        # Minimum before value: raising it clamps a parked -1 up first.
        spin.setMinimum(0 if uniform else -1)
        spin.setValue(int(next(iter(values))) if uniform else -1)

    # -- the writes ----------------------------------------------------------
    def _on_cell_prop_clicked(self, field: str, _checked: bool = False) -> None:
        """A checkbox click, landed wherever the row is pointed.

        The state read back off the box rather than taken from the signal,
        because a mixed box is tristate and the click's *destination* is the
        meaning — a click from the part-checked display must resolve to a
        plain yes or no.
        """
        if self._applying_undo:
            return
        target = self._cell_props_target()
        if target is None:
            return
        spec = _SPEC_BY_FIELD[field]
        state = self._cell_props_widgets[field].checkState()
        if field == "visible" and target != "selection":
            # The stamp forces the bit on, so the box is that default and the
            # eraser — a stamp-wide setting whatever is held, since a record's
            # own bit is overwritten either way (:meth:`_stamp_cell`).
            if state == Qt.CheckState.Checked:
                self._stamp_attrs.pop("visible", None)
                self._after_stamp_prop_edit("Stamps draw again.")
            else:
                self._stamp_attrs["visible"] = False
                self._after_stamp_prop_edit(
                    "Stamping now clears - each press lays undrawn cells."
                )
            return
        on = state == Qt.CheckState.Checked
        value: bool | int = int(on) if field == "priority" else on
        said = "on" if on else "off"
        if target == "selection":
            self._set_cell_property(field, value)
        elif target == "attrs":
            self._stamp_attrs[field] = value
            self._after_stamp_prop_edit(f"Next stamp lays {spec.said} {said}.")
        elif target == "record":
            self._source_cell = replace(self._source_cell, **{field: value})
            self._after_stamp_prop_edit(f"Held stamp's {spec.said} {said}.")
        else:
            self._set_brush_property(field, value)
            brush = self._stamp_brush
            self._after_stamp_prop_edit(
                f"Turned {spec.said} {said} across the held "
                f"{brush.width}x{brush.height} brush."
            )

    def _on_cell_prop_spun(self, field: str, value: int) -> None:
        """A spin edit, landed wherever the row is pointed.

        Negative is the ``—`` floor — the mixed display's parking spot, never
        a value: the sync keeps the minimum at 0 wherever a write could land
        (:meth:`_show_cell_prop_spin`), so the guard is a backstop, not a path.
        """
        if self._applying_undo:
            return
        target = self._cell_props_target()
        if target is None:
            return
        spec = _SPEC_BY_FIELD[field]
        if value < 0:
            return
        if target == "selection":
            self._set_cell_property(field, value)
        elif target == "attrs":
            self._stamp_attrs[field] = value
            self._after_stamp_prop_edit(f"Next stamp lays {spec.said} {value}.")
        elif target == "record":
            self._source_cell = replace(self._source_cell, **{field: value})
            self._after_stamp_prop_edit(f"Held stamp's {spec.said} set to {value}.")
        else:
            self._set_brush_property(field, value)
            brush = self._stamp_brush
            self._after_stamp_prop_edit(
                f"Set {spec.said} to {value} across the held "
                f"{brush.width}x{brush.height} brush."
            )

    def _set_brush_property(self, field: str, value: bool | int) -> None:
        """Write ``field`` into every record of the held brush — the set-all.

        A property edit, not a transform: making a mixed brush uniform is the
        one coherent reading of a checkbox over it. The geometric mirror —
        reverse the order *and* toggle each — is the Block flip's (Shift+H/V,
        :meth:`~...stamp_tool.StampToolMixin._stamp_transform_block`).
        """
        brush = self._stamp_brush
        if brush is None:
            return
        for y in range(brush.height):
            for x in range(brush.width):
                brush.set(x, y, replace(brush.get(x, y), **{field: value}))

    def _after_stamp_prop_edit(self, message: str) -> None:
        """A stamp-mode edit's tail: session state moved, so nothing is pushed —
        the stroke that lands it is the undo step — but the row, the ghost and
        the status line all describe it and all have to move together."""
        self._sync_cell_props()
        self._sync_stamp_preview()
        self.statusBar().showMessage(message)
