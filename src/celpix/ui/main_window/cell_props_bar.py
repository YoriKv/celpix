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
them would leave the row empty exactly where it is needed. One of them reads
specially there. **Drawn** is a stamp-wide setting whatever is held, because
the stamp otherwise forces the bit: checked is that default, and unchecking
it turns the stamp into the **eraser** — every press lays undrawn cells, the
inverse Clear Cells performs on a selection. It is also the one field a
displaced record does not seed: the eraser is armed by this box alone, never
inherited from a cell that happened to be undrawn.

**A sprite map grows record fields** after the cell ones
(``docs/design/sprite-map.md`` §10.3): X and Y, Size, and on a slot format
Drawn — generated from the codec's
:meth:`~celpix.plugins.base.TilemapCodecPlugin.subsprite_limits` rather than
from the field table, because a :class:`Cell` has no attribute for any of them
and only the codec knows where they live
(:meth:`~...tilemap_edit.TilemapEditMixin._place_subsprites`). There the
selection target is the picked pieces, and the tool both picks and places,
so the two targets are told apart by **what was pointed at last**: a piece
picked on the canvas or in the Subsprites window is the target, and a tile
taken for placing — a sheet click or sweep, the eyedropper — drops the pick
and makes the held piece the target
(:meth:`~...sprite_edit.SpriteEditMixin._drop_pick_for_held`). Under the
held piece X and Y stay disabled — its offsets are wherever the click lands
it — Drawn is hidden, and **Size** is the one record field it has: a sheet
pick's setting (``_stamp_attrs["size"]``, written into the template through
the codec), or the held record's own bits rewritten in place
(:meth:`_set_held_size`); either way the Size shown is what the add would
resolve (:meth:`~...sprite_edit.SpriteEditMixin._held_size_choice`).

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
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QWidget,
)

from celpix.core.capabilities import Capability
from celpix.core.sprite import SizeChoice, SubspriteLimits
from celpix.core.tilemap import BLANK, Cell
from celpix.ui.main_window.tilemap_edit import fit_cell
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

# A sprite map's record fields, after the cell ones. Not in the field table
# above: a Cell has no attribute for any of them — the offsets, the size and
# the drawn bit live in ``flags`` on the format's own terms — so they are
# written through the codec (:meth:`~...tilemap_edit.TilemapEditMixin.
# _place_subsprites`) and generated from its limits, not from ``cell_fields``.
_RECORD_TIPS = {
    "x": "The picked subsprites' offset right of the object's origin\n"
    "In pixels; negative is left of it",
    "y": "The picked subsprites' offset below the object's origin\n"
    "In pixels; negative is above it",
    "size": "The picked subsprites' size, in tiles\n"
    "A size bit picks one of the entry's two sizes\n"
    "In Edit Subsprites mode: the size the next piece lands at",
    "drawn": "Whether the picked slots are drawn\n"
    "Off clears the slot, as Clear does; on puts it back",
}


if TYPE_CHECKING:
    # For the checker only: a mixin is a slice of the window, so its
    # ``self`` is the window, widgets, state and all (see the package
    # docstring). At runtime it is a plain mixin.
    from celpix.ui.main_window.window import MainWindow as _Host
else:
    _Host = object


class CellPropsMixin(_Host):
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
        # A sprite map's record controls (X, Y, Size, Drawn), apart from the
        # cell ones because they read and write pieces rather than Cell fields.
        self._record_props_widgets: dict[str, QWidget] = {}
        self._record_props_labels: dict[str, QLabel] = {}
        # What the controls were built for: ``(((field, limit), ...) sorted,
        # record)`` — ``record`` the sprite limits and size pair the record
        # controls were generated from, or None — or () for no row. Rebuilding
        # only when this moves is what keeps the sync cheap enough to run from
        # the selection pass; the pair is in it because the Size captions say
        # what each choice spells under it.
        self._cell_props_signature: tuple = ()
        # The field table the held record and brush were last fitted to — what
        # tells the sync that an entry switch has put them on another format.
        self._held_fields: dict[str, int] = {}

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
        self._record_props_widgets.clear()
        self._record_props_labels.clear()
        self._cell_props_signature = signature
        if not signature:
            return
        fields = dict(signature[0])
        for spec in _PROPERTY_SPECS:
            limit = fields.get(spec.field)
            if limit is None:
                continue
            # Both spins' -1 floor is the ``—`` sentinel — display only, shown
            # while a mixed target parks on it. The sync owns the minimum and
            # the dash (:meth:`_show_cell_prop_spin`); the build leaves it 0.
            if spec.field == "flags":
                spin = hex_spin(0, limit, spec.tip)
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
        if signature[1] is not None:
            self._build_record_props(layout, signature[1][0])
        layout.addStretch(1)

    def _build_record_props(self, layout: QHBoxLayout, limits: SubspriteLimits) -> None:
        """Grow a sprite map's record controls from what its codec can write.

        X and Y span the offset fields' own signed ranges, each one below the
        range being the ``—`` sentinel a mixed pick parks on, as the cell spins'
        -1 is. Size offers every choice the record can spell, captioned with
        the shape it draws, and is absent where the preset fixes the size.
        Drawn is a slot format's alone: elsewhere a record that is present is
        drawn, and removing one is Clear's.

        A ``fixed`` layout grows none of them: the drawing routine places and
        sizes every piece, and its records hold no field X, Y, Size or Drawn
        could write. The row keeps the cell fields — tile and flips — and the
        sync and handlers below walk an empty record set.
        """
        if limits.structure == "fixed":
            return
        widgets, labels = self._record_props_widgets, self._record_props_labels
        for field, bounds in (("x", limits.x_range), ("y", limits.y_range)):
            spin = value_spin(
                bounds.start,
                bounds.stop - 1,
                0,
                partial(self._on_record_prop_spun, field),
            )
            labels[field] = add_labelled(
                layout, f"{field.upper()} ", spin, _RECORD_TIPS[field]
            )
            widgets[field] = spin
        if limits.size:
            combo = QComboBox()
            combo.setPlaceholderText("—")
            for choice in limits.size:
                combo.addItem(self._size_caption(choice))
            # `activated`, not `currentIndexChanged`: only a user's pick writes,
            # so the sync's restores need no guard of their own.
            combo.activated.connect(self._on_record_size_activated)
            labels["size"] = add_labelled(layout, "Size ", combo, _RECORD_TIPS["size"])
            widgets["size"] = combo
        if limits.structure == "slots":
            box = QCheckBox("Drawn")
            box.setToolTip(_RECORD_TIPS["drawn"])
            box.clicked.connect(self._on_record_drawn_clicked)
            layout.addWidget(box)
            widgets["drawn"] = box

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

        On a **sprite map** a pick is the selection inside Edit Subsprites as
        well as outside it — arming clears the tile selection, not the pick —
        and it wins over the held stamp while there is one: it is what the
        tool's drags and keys are acting on, and a click-to-add leaves the
        added pieces picked. What keeps that from pinning the row on the pick
        is that taking a tile for placing drops it
        (:meth:`~...sprite_edit.SpriteEditMixin._drop_pick_for_held`), so the
        row follows whichever the user pointed at last. Under a ``fixed``
        layout a click lays nothing, so the held piece is no target there:
        with nothing picked the row has nothing to edit.
        """
        if not self._stamping:
            return "selection"
        doc = self._doc
        if doc is not None and doc.is_sprite:
            if self._selected_cells():
                return "selection"
            if self._fixed_layout():
                return None
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
        for field, limit in self._cell_fields().items():
            if field in _SPEC_BY_FIELD and field != "visible":
                value = getattr(record, field)
                # Clamped: a record can come off a map whose format holds more.
                self._stamp_attrs[field] = (
                    value if isinstance(value, bool) else min(value, limit)
                )
        # A sprite map's record carries its size in ``flags``, so the size
        # travels too — read through the probe while the record is still the
        # one held, since only the frames say what shape its bits spell.
        limits = self._subsprite_limits()
        if limits is not None and limits.size and record is self._source_cell:
            choice = self._held_size_choice()
            if choice is not None:
                self._stamp_attrs["size"] = choice

    # -- sync ----------------------------------------------------------------
    def _sync_cell_props(self) -> None:
        """Converge the row with the format, the mode and the target.

        Rebuilds only when the format's field set — or, on a sprite map, its
        record limits — moved; everything else is a signal-blocked restore. The
        gate takes a sprite map whose codec answers the limits, which is what
        makes its pieces editable at all. Also where a stamp setting whose field the
        format no longer declares is dropped — a stale one surviving a codec
        switch would land bits the new format cannot store, and the row would
        show a value the landing quietly ignores.
        """
        doc = self._doc
        fields = self._cell_fields() if doc is not None and doc.is_tilemap else {}
        shown = {
            field: limit for field, limit in fields.items() if field in _SPEC_BY_FIELD
        }
        # A sprite map grows its record fields from the codec's limits, so one
        # whose codec answers them has a row even with no Cell field to show.
        limits = self._subsprite_limits()
        record = (limits, self._subsprite_pair()) if limits is not None else None
        usable = (
            doc is not None
            and doc.cells_editable
            and self._can(Capability.STAMP)
            and (bool(shown) or record is not None)
        )
        signature = (tuple(sorted(shown.items())), record) if usable else ()
        if signature != self._cell_props_signature:
            self._rebuild_cell_props(signature)
        for field in [name for name in self._stamp_attrs if name not in shown]:
            # The size setting is a sprite map's, kept while its codec still
            # spells that choice; a pair of sides no other format offers, or a
            # bit on a format with a nibble, would land a size nobody picked.
            if (
                field == "size"
                and limits is not None
                and self._stamp_attrs[field] in (limits.size or ())
            ):
                continue
            del self._stamp_attrs[field]
        if fields and fields != self._held_fields:
            self._fit_held_stamp(fields)
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
        if limits is not None:
            self._sync_record_props(limits, target, armed)

    def _sync_record_props(
        self, limits: SubspriteLimits, target: str | None, armed: bool
    ) -> None:
        """Show the record fields for ``target``, or park them disabled.

        X and Y are the **selection's** alone: the held piece's offsets are
        wherever the click lands it, so under it they stay on the row
        disabled, and Drawn, which on the held piece would be the eraser it
        already is, is hidden. **Size** has a reading under both: the picked
        pieces' shapes, or the size the held piece lands at
        (:meth:`~...sprite_edit.SpriteEditMixin._held_size_choice`) — except
        under a swept brush, whose pieces take the sizes the split gives them,
        so it is disabled there rather than showing a setting the landing
        ignores.

        Selection values come from the drawn pieces (:meth:`~...sprite_select.
        SpriteSelectMixin._picked_subsprites`): a picked record that is not
        drawn has no offset or size to show, and is the one thing Drawn reads
        as off.
        """
        on_pick = armed and target == "selection"
        on_held = (
            armed
            and target in ("attrs", "record", "brush")
            and not self._held_lays_brush()
        )
        pieces = self._picked_subsprites() if on_pick else []
        for field, widget in self._record_props_widgets.items():
            enabled = on_pick or (field == "size" and on_held)
            widget.setEnabled(enabled)
            label = self._record_props_labels.get(field)
            if label is not None:
                label.setEnabled(enabled)
            with signals_blocked(widget):
                if field == "drawn":
                    widget.setVisible(target == "selection")
                    drawn = {record for record, _sub in pieces}
                    values = (
                        {at in drawn for at in self._selected_cells()}
                        if on_pick
                        else set()
                    )
                    self._show_cell_prop_check(widget, values)
                elif field == "size":
                    if on_pick:
                        values = {self._size_choice_of(sub) for _r, sub in pieces}
                    elif on_held:
                        choice = self._held_size_choice()
                        values = set() if choice is None else {choice}
                    else:
                        values = set()
                    self._show_record_size(widget, limits, values)
                else:
                    bounds = limits.x_range if field == "x" else limits.y_range
                    self._show_record_spin(
                        widget, bounds, {getattr(sub, field) for _r, sub in pieces}
                    )

    @staticmethod
    def _show_record_spin(spin: QSpinBox, bounds: range, values: set) -> None:
        """An offset spin's display; ``—`` (one below the range) is mixed.

        :meth:`_show_cell_prop_spin` with a signed floor: the sentinel opens
        only while a mixed or empty pick parks on it, so no value a user can
        enter is one the write ignores.
        """
        uniform = len(values) == 1
        _park_spin(spin, bounds.start, uniform)
        spin.setValue(int(next(iter(values))) if uniform else bounds.start - 1)

    @staticmethod
    def _show_record_size(
        combo: QComboBox, limits: SubspriteLimits, values: set
    ) -> None:
        """The Size box's display: the one choice, or the ``—`` placeholder
        for a mixed pick and a shape none of the choices spells."""
        choices = list(limits.size or ())
        value = next(iter(values)) if len(values) == 1 else None
        combo.setCurrentIndex(choices.index(value) if value in choices else -1)

    def _fit_held_stamp(self, fields: dict[str, int]) -> None:
        """Fit the eyedropped record and the canvas brush to ``fields``.

        Both outlive an entry switch, so they can hold a flip or a row the
        format now on screen has no field for. The landing is fitted again in
        :meth:`~...tilemap_edit.TilemapEditMixin._apply_cells`; this is so the
        ghost and the row show what a press will lay rather than what was lifted.
        """
        self._held_fields = dict(fields)
        blank = Cell()
        if self._source_cell is not None:
            self._source_cell = fit_cell(self._source_cell, fields, blank)
        brush = self._stamp_brush
        if brush is not None:
            for y in range(brush.height):
                for x in range(brush.width):
                    record = brush.get(x, y)
                    if record is not None:
                        brush.set(x, y, fit_cell(record, fields, blank))

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
        _park_spin(spin, 0, uniform)
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

    def _on_record_prop_spun(self, field: str, value: int) -> None:
        """An X or Y edit, written into every picked piece — one step.

        Below the range is the ``—`` floor, never a value
        (:meth:`_show_record_spin`). A refused write puts the row back on what
        the pieces hold, since nothing landed to re-sync it.
        """
        limits = self._subsprite_limits()
        if self._applying_undo or limits is None:
            return
        if self._cell_props_target() != "selection":
            return
        bounds = limits.x_range if field == "x" else limits.y_range
        if value < bounds.start:
            return
        if not self._place_subsprites({field: value}, "move subsprites"):
            self._sync_cell_props()

    def _on_record_size_activated(self, index: int) -> None:
        """A Size pick: into every picked piece in one step, or into the held
        piece as the size the next click lays (:meth:`_set_held_size`)."""
        limits = self._subsprite_limits()
        if self._applying_undo or limits is None or not limits.size:
            return
        target = self._cell_props_target()
        if target is None or not 0 <= index < len(limits.size):
            return
        choice = limits.size[index]
        if target == "selection":
            if not self._place_subsprites({"size": choice}, "resize subsprites"):
                self._sync_cell_props()
        elif not self._held_lays_brush():  # the box is disabled over a brush
            self._set_held_size(choice)

    def _set_held_size(self, choice: SizeChoice) -> None:
        """Size against the held piece, by the row's rule for its holdings.

        A **sheet pick** has no record behind it, so the row's setting is the
        size — laid into the template through the codec on every landing
        (:meth:`~...sprite_edit.SpriteEditMixin._held_template`). An
        **eyedropped record** is a whole record and lays down whole, so its
        own size bits are rewritten in place, through the codec as a picked
        piece's are. Session state either way: no step is pushed, the add that
        lands it is the step. A refusal puts the box back on what the piece
        holds, since nothing moved to re-sync it.
        """
        caption = self._size_caption(choice)
        if self._source_cell is None:
            self._stamp_attrs["size"] = choice
            message = f"Next subsprite lands {caption}."
        else:
            try:
                self._source_cell = self._place_subsprite(
                    self._source_cell, size=choice
                )
            except ValueError as exc:
                reason = str(exc)
                self.statusBar().showMessage(
                    f"{reason[:1].upper()}{reason[1:]} - nothing changed."
                )
                self._sync_cell_props()
                return
            message = f"Held subsprite's size set to {caption}."
        self._after_stamp_prop_edit(message)

    def _on_record_drawn_clicked(self, _checked: bool = False) -> None:
        """The Drawn box: off clears the picked slots, on puts them back.

        Off is Clear's reading on a slot format — the drawn bit cleared and the
        rest of the slot kept — which is what lets on restore the same piece:
        the pick holds an undrawn record for exactly this
        (:meth:`~...sprite_select.SpriteSelectMixin._init_sprite_select`). The
        state is read off the box, as :meth:`_on_cell_prop_clicked` does, so a
        click from the mixed display resolves to a plain yes or no.
        """
        if self._applying_undo or self._cell_props_target() != "selection":
            return
        box = self._record_props_widgets.get("drawn")
        if box is None:
            return
        on = box.checkState() == Qt.CheckState.Checked
        text = "draw subsprites" if on else "hide subsprites"
        if not self._place_subsprites({"drawn": on}, text):
            self._sync_cell_props()

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
        # A sprite map's tile source ring marks the held piece's footprint,
        # which its size setting moves.
        self._sync_tile_source_marker()
        self.statusBar().showMessage(message)


def _park_spin(spin: QSpinBox, floor: int, uniform: bool) -> None:
    """Fence the ``—`` sentinel off or open it: the minimum and the dash together.

    Qt shows the special value text whenever the value *is* the minimum, for
    any reason, so the dash cannot stay set while the minimum is the field's
    real floor — a piece at offset 0 on an unsigned format, or at -128 on a
    signed one, would read as a mixed pick. The text is set only while the
    display is parked one below the floor. Minimum before value: raising it
    clamps a parked sentinel up first.
    """
    spin.setSpecialValueText("" if uniform else "—")
    spin.setMinimum(floor if uniform else floor - 1)
