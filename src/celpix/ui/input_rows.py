"""The Inputs window's rows: one editor per kind of declared input.

A stage plugin declares what it needs from outside its bytes
(:class:`~celpix.plugins.base.InputSpec`), and each kind — a region, an
integer, a flag, a choice — gets the one editor here that states it. The window
builds a row per declaration with :func:`row_for` and never asks which kind it
got: every row answers :meth:`InputRow.binding`, shows a binding without
echoing it (:meth:`InputRow.set_binding`), and wears the same status mark
(:mod:`celpix.ui.inputs_window`).

Offsets and lengths are free-text address boxes
(:func:`~celpix.ui.widgets.hex_field`): bare digits are hex, ``$`` and ``0x``
accepted, and a row left blank is **unbound**.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from celpix.core.address import format_hex, hex_digits
from celpix.plugins.base import InputKind, InputSpec
from celpix.project.inputs import InputBinding, IntegerFromBytes, RegionBinding
from celpix.project.workspace import Entry
from celpix.ui.searchable_combo import SearchableComboBox
from celpix.ui.theme import WARNING_INK, set_ink
from celpix.ui.widgets import (
    PRESET_COMBO_WIDTH,
    SHORT_COMBO_WIDTH,
    CompactComboBox,
    counted,
    hex_field,
    hex_value,
    signals_blocked,
)

__all__ = ["ChoiceRow", "FlagRow", "InputRow", "IntegerRow", "RegionRow", "row_for"]

_OK_MARK = "✓"
_PROBLEM_MARK = "!"


def _count(length: int, spec: InputSpec) -> str:
    """``"750 bytes · 375 words"`` — a length in bytes and in the spec's own unit."""
    text = counted(length, "byte")
    if spec.unit and spec.stride > 1:
        text += f" · {counted(length // spec.stride, spec.unit)}"
    return text


def _unit_value(value: int, spec: InputSpec) -> str:
    """``value`` counted in the spec's unit where it has one — ``"375 words"``."""
    return counted(value, spec.unit) if spec.unit else f"{value:,}"


class InputRow(QWidget):
    """One declared input's editor. Subclasses lay out their kind's fields and
    answer :meth:`binding`; the window wires the status mark and the three
    signals, which are the same whatever the kind."""

    changed = Signal()
    #: *Use selection* was pressed — this row. A kind with nothing to select
    #: (a flag, a choice) never emits it, but the window connects every row the
    #: same way, so every row has it.
    use_selection_requested = Signal(object)
    #: *Go to* was pressed — the binding to show. Likewise never emitted by a
    #: kind that points at nothing.
    go_to_requested = Signal(object)

    def __init__(self, spec: InputSpec, sources: list[Entry]) -> None:
        super().__init__()
        self.spec = spec
        self._sources = sources
        self._status = QLabel(_OK_MARK)
        self._status.setFixedWidth(self._status.fontMetrics().horizontalAdvance("!!"))
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def binding(self) -> InputBinding | None:
        """What the row says, or ``None`` for unbound."""
        raise NotImplementedError

    def set_binding(self, binding: InputBinding | None) -> None:
        """Show ``binding`` without firing :attr:`changed`."""
        raise NotImplementedError

    def set_problem(self, detail: str | None) -> None:
        """The status mark: a tick, or a warning whose tooltip says why."""
        if detail is None:
            self._status.setText(_OK_MARK)
            self._status.setToolTip("Resolves")
            set_ink(self._status, None)
        else:
            self._status.setText(_PROBLEM_MARK)
            self._status.setToolTip(detail)
            set_ink(self._status, WARNING_INK)

    def status_widget(self) -> QLabel:
        return self._status

    def _source_combo(self) -> QComboBox:
        """*This file*, then every entry a region may name — the one-hop rule
        having already been applied by whoever built the list.

        The format pickers' own widget, at the format pickers' own width: a
        project's entry names are as long and as many as a registry's preset
        names, so this list wants the same search field and the same refusal to
        let one long name decide how wide the control is (the popup is widened
        back to the content while choosing).
        """
        combo = SearchableComboBox(PRESET_COMBO_WIDTH)
        combo.addItem("This file", None)
        for entry in self._sources:
            combo.addItem(entry.name, entry)
        combo.setToolTip(
            "Source of the bytes:\n"
            "• This file - the entry's own file, by absolute offset\n"
            "• An entry - that entry's decoded bytes, from 0"
        )
        return combo

    def _select_source(self, combo: QComboBox, entry: Entry | None) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) is entry:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    def _hex_field(self, tip: str, width_chars: int = 10) -> QLineEdit:
        field = hex_field(tip)
        field.setFixedWidth(field.fontMetrics().horizontalAdvance("0" * width_chars))
        field.editingFinished.connect(self.changed.emit)
        return field


class RegionRow(InputRow):
    """A **region** input: a source, an offset and a length."""

    def __init__(self, spec: InputSpec, sources: list[Entry]) -> None:
        super().__init__(spec, sources)
        self._source = self._source_combo()
        self._source.currentIndexChanged.connect(lambda _i: self.changed.emit())
        self._offset = self._hex_field("Offset")
        self._length = self._hex_field(
            f"Length in bytes\n"
            f"A whole number of {spec.stride}-byte {spec.unit or 'element'}s"
            if spec.stride > 1
            else "Length in bytes"
        )
        self._count = QLabel()
        self._use = QPushButton("Use selection")
        self._use.setToolTip(
            "Fill source, offset and length from the\ncurrent entry's selection"
        )
        self._use.clicked.connect(lambda: self.use_selection_requested.emit(self))
        self._go = QPushButton("Go to")
        self._go.setToolTip("Show the region in its source")
        self._go.clicked.connect(self._emit_go_to)
        self.changed.connect(self._refresh_count)

    def widgets(self) -> list[QWidget]:
        return [
            self._source,
            self._offset,
            self._length,
            self._count,
            self._use,
            self._go,
        ]

    def binding(self) -> RegionBinding | None:
        offset = hex_value(self._offset)
        length = hex_value(self._length)
        if offset is None or length is None:
            return None
        return RegionBinding(
            entry=self._source.currentData(), offset=offset, length=length
        )

    def set_binding(self, binding: InputBinding | None) -> None:
        with signals_blocked(self._source, self._offset, self._length):
            if isinstance(binding, RegionBinding):
                self._select_source(self._source, binding.entry)
                self._offset.setText(format_hex(binding.offset, prefix=False))
                self._length.setText(format_hex(binding.length, prefix=False))
            else:
                self._source.setCurrentIndex(0)
                self._offset.clear()
                self._length.clear()
        self._refresh_count()

    def _refresh_count(self) -> None:
        length = hex_value(self._length)
        self._count.setText(_count(length, self.spec) if length is not None else "")
        self._go.setEnabled(self.binding() is not None)

    def _emit_go_to(self) -> None:
        binding = self.binding()
        if binding is not None:
            self.go_to_requested.emit(binding)


class IntegerRow(InputRow):
    """An **integer** input: a literal, or a number read out of bytes."""

    def __init__(self, spec: InputSpec, sources: list[Entry]) -> None:
        super().__init__(spec, sources)
        # What a From-bytes binding currently reads, as the resolver last said
        # (:meth:`set_read_value`); ``None`` until it has.
        self._read: int | None = None
        # Every literal is shown at the width of the largest the spec allows,
        # so the box, its placeholder and the range in its tooltip line up.
        self._digits = hex_digits(spec.maximum)
        self._form = CompactComboBox(SHORT_COMBO_WIDTH)
        self._form.addItem("Literal", "literal")
        self._form.addItem("From bytes", "bytes")
        self._form.setToolTip(
            "• Literal - a number typed here\n"
            "• From bytes - a number read from the file at an offset,\n"
            "so it follows edits there"
        )
        self._form.currentIndexChanged.connect(self._on_form_change)
        width = self._digits
        self._literal = self._hex_field(
            f"Value, {format_hex(spec.minimum, width)} to "
            f"{format_hex(spec.maximum, width)}"
        )
        if spec.default is not None and not spec.required:
            # Left empty, the codec is handed the default: say which.
            self._literal.setPlaceholderText(
                format_hex(spec.default, width, prefix=False)
            )
        self._source = self._source_combo()
        self._source.currentIndexChanged.connect(lambda _i: self.changed.emit())
        self._offset = self._hex_field("Offset of the number")
        self._width = QSpinBox()
        self._width.setRange(1, 8)
        self._width.setValue(2)
        self._width.setSuffix(" B")
        self._width.setToolTip("Width of the number in bytes")
        self._width.valueChanged.connect(lambda _v: self.changed.emit())
        self._endian = CompactComboBox(SHORT_COMBO_WIDTH)
        self._endian.addItem("big-endian", False)
        self._endian.addItem("little-endian", True)
        self._endian.currentIndexChanged.connect(lambda _i: self.changed.emit())
        self._hint = QLabel()
        self._use = QPushButton("Use selection")
        self._use.setToolTip(
            "Read the number from the start of the current\n"
            "entry's selection, up to 8 bytes wide"
        )
        self._use.clicked.connect(lambda: self.use_selection_requested.emit(self))
        self._go = QPushButton("Go to")
        self._go.setToolTip("Show the number in its source")
        self._go.clicked.connect(self._emit_go_to)
        self.changed.connect(self._refresh_hint)
        self._on_form_change()

    def widgets(self) -> list[QWidget]:
        return [
            self._form,
            self._literal,
            self._source,
            self._offset,
            self._width,
            self._endian,
            self._hint,
            self._use,
            self._go,
        ]

    def _from_bytes(self) -> bool:
        return self._form.currentData() == "bytes"

    def _on_form_change(self) -> None:
        from_bytes = self._from_bytes()
        self._literal.setVisible(not from_bytes)
        for widget in (
            self._source,
            self._offset,
            self._width,
            self._endian,
            self._use,
            self._go,
        ):
            widget.setVisible(from_bytes)
        self.changed.emit()

    def binding(self) -> InputBinding | None:
        if self._from_bytes():
            offset = hex_value(self._offset)
            if offset is None:
                return None
            return IntegerFromBytes(
                entry=self._source.currentData(),
                offset=offset,
                width=self._width.value(),
                little_endian=bool(self._endian.currentData()),
            )
        return hex_value(self._literal, allow_negative=self.spec.minimum < 0)

    def set_binding(self, binding: InputBinding | None) -> None:
        with signals_blocked(
            self._form,
            self._literal,
            self._source,
            self._offset,
            self._width,
            self._endian,
        ):
            if isinstance(binding, IntegerFromBytes):
                self._form.setCurrentIndex(1)
                self._select_source(self._source, binding.entry)
                self._offset.setText(format_hex(binding.offset, prefix=False))
                self._width.setValue(binding.width)
                self._endian.setCurrentIndex(1 if binding.little_endian else 0)
            else:
                self._form.setCurrentIndex(0)
                self._literal.setText(
                    format_hex(binding, self._digits, prefix=False)
                    if isinstance(binding, int) and not isinstance(binding, bool)
                    else ""
                )
        self._on_form_change()

    def set_read_value(self, value: int | None) -> None:
        """What a From-bytes binding currently reads, from the resolver."""
        self._read = value
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        value = self._read if self._from_bytes() else self.binding()
        if isinstance(value, int):
            self._hint.setText(f"= {_unit_value(value, self.spec)}")
        elif (
            not self._from_bytes()
            and not self._literal.text().strip()
            and not self.spec.required
            and self.spec.default is not None
        ):
            # Nothing typed hands the codec its default, so show that number.
            self._hint.setText(
                f"= {_unit_value(self.spec.default, self.spec)} (default)"
            )
        else:
            self._hint.setText("")
        self._go.setEnabled(self._from_bytes() and self.binding() is not None)

    def _emit_go_to(self) -> None:
        binding = self.binding()
        if isinstance(binding, IntegerFromBytes):
            self.go_to_requested.emit(binding)


class FlagRow(InputRow):
    """A **flag** input: one checkbox.

    There is no unbound state to offer, because an unbound flag reads as its
    default and a box showing the default *is* that reading. So the row binds
    only the other value: a box left at the default writes nothing, and a
    project file carries a flag only where it was switched.
    """

    def __init__(self, spec: InputSpec, sources: list[Entry]) -> None:
        super().__init__(spec, sources)
        self._box = QCheckBox()
        self._box.setToolTip(spec.tooltip or spec.label)
        self._box.toggled.connect(lambda _on: self.changed.emit())

    def widgets(self) -> list[QWidget]:
        return [self._box]

    def binding(self) -> bool | None:
        on = self._box.isChecked()
        return None if on == bool(self.spec.default) else on

    def set_binding(self, binding: InputBinding | None) -> None:
        with signals_blocked(self._box):
            self._box.setChecked(
                binding if isinstance(binding, bool) else bool(self.spec.default)
            )


class ChoiceRow(InputRow):
    """A **choice** input: a drop-down of the spec's labels, holding its keys.

    The flag's rule, for the flag's reason: unbound reads as the default, so
    the default showing binds nothing and only another option is written. A
    stored key the plugin does not list is kept as an extra item, marked, so
    the status mark can say why it is refused and Apply does not quietly
    replace it before the user has picked something else.
    """

    def __init__(self, spec: InputSpec, sources: list[Entry]) -> None:
        super().__init__(spec, sources)
        # The format pickers' width, not the short one: the labels are the
        # plugin's, so nothing here bounds how long they run.
        self._combo = CompactComboBox(PRESET_COMBO_WIDTH)
        for key, label in spec.options:
            self._combo.addItem(label, key)
        self._combo.setToolTip(spec.tooltip or spec.label)
        self._combo.currentIndexChanged.connect(lambda _i: self.changed.emit())

    def widgets(self) -> list[QWidget]:
        return [self._combo]

    def binding(self) -> str | None:
        key = self._combo.currentData()
        return None if key == self.spec.choice_default else key

    def set_binding(self, binding: InputBinding | None) -> None:
        with signals_blocked(self._combo):
            # Drop any unknown key a previous binding added: the list is the
            # spec's options plus, at most, the one being shown.
            while self._combo.count() > len(self.spec.options):
                self._combo.removeItem(self._combo.count() - 1)
            key = binding if isinstance(binding, str) else self.spec.choice_default
            at = self._combo.findData(key)
            if at < 0:
                self._combo.addItem(f"{key} (unknown)", key)
                at = self._combo.count() - 1
            self._combo.setCurrentIndex(at)


def row_for(spec: InputSpec, sources: list[Entry]) -> InputRow:
    """The editor for ``spec``'s kind — a region where nothing more is said."""
    if spec.kind is InputKind.INTEGER:
        return IntegerRow(spec, sources)
    if spec.kind is InputKind.FLAG:
        return FlagRow(spec, sources)
    if spec.kind is InputKind.CHOICE:
        return ChoiceRow(spec, sources)
    return RegionRow(spec, sources)
