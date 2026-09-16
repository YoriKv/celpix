"""The Inputs window: what one entry hands its plugins from outside its own bytes.

A stage plugin may declare **inputs** — a code table shared by forty streams, a
sprite mapping's parallel arrays, an output size kept in a length table
(:class:`~celpix.plugins.base.InputSpec`) — and this is where an entry binds
them (``docs/design/plugin-inputs.md`` §6). The form is **generated** from
the declarations: one section per stage of the entry whose plugin declares any,
one row per declared input, built from the spec's kind. A codec with five
region inputs and one with a table and a size get the same editor with no UI
code of their own.

A non-modal ``Qt.Tool`` window, like the font alphabet and subsprite windows,
and **pinned to the entry it was opened for** rather than following the current
one: the point of it being non-modal is that the user goes and selects the
table in the parent while it is open, and *Use selection* reads that selection
back. (The one exception is the Slice dialog's badge, which opens it blocking —
see :meth:`InputsWindow.show_for`.) Escape closes it either way: a ``QWidget``
inherits none of ``QDialog``'s key handling, so the gesture is spelled out. The
main window supplies what the rows cannot know — the entries a region may name,
what the selection on screen is, whether a binding resolves — through the
callbacks handed to :meth:`InputsWindow.show_for`; nothing here reads a file or
touches the workspace.

Offsets and lengths follow the app-wide address-box convention
(:func:`~celpix.core.address.parse_hex`): bare digits are hex, ``$`` and ``0x``
accepted. A row left blank is **unbound**, which for a required input is what
the status mark and the notice then say.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from celpix.core.address import format_hex, parse_hex
from celpix.core.errors import Stage
from celpix.plugins.base import InputKind, InputSpec
from celpix.project.inputs import (
    Bindings,
    InputBinding,
    IntegerFromBytes,
    RegionBinding,
    StageInputs,
)
from celpix.project.workspace import Entry
from celpix.ui.searchable_combo import SearchableComboBox
from celpix.ui.theme import WARNING_INK, set_ink
from celpix.ui.widgets import (
    PRESET_COMBO_WIDTH,
    SHORT_COMBO_WIDTH,
    CompactComboBox,
    signals_blocked,
)

__all__ = ["InputsWindow", "Section"]

# What each stage's section is captioned, in the words the toolbar uses.
_STAGE_CAPTIONS = {
    Stage.COMPRESSION: "Compression",
    Stage.INTERPRET_TILEMAP: "Tilemap",
    Stage.INTERPRET_PIXEL: "Pixel",
    Stage.INTERPRET_PALETTE: "Palette",
}

#: One section of the window: a stage's declarations, the plugin's display
#: name, and what the entry currently binds for it.
Section = tuple[StageInputs, str, Bindings]

#: ``(plugin id, key) -> why it does not resolve``; a key that resolves is absent.
Problems = dict[tuple[str, str], str]

_OK_MARK = "✓"  # ✓
_PROBLEM_MARK = "!"


def _count(length: int, spec: InputSpec) -> str:
    """``"750 B · 375 words"`` — a length in bytes and in the spec's own unit."""
    text = f"{length:,} B"
    if spec.unit and spec.stride > 1:
        text += f" · {length // spec.stride:,} {spec.unit}s"
    return text


class _Row(QWidget):
    """One declared input's editor. Subclasses lay out their kind's fields and
    answer :meth:`binding`; the window wires the status mark and the change
    signal, which are the same whatever the kind."""

    changed = Signal()

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
            "Where the bytes come from:\n"
            "This file - the file this entry's bytes are in, by\n"
            "absolute offset\n"
            "An entry - that entry's resolved bytes, from 0"
        )
        return combo

    def _select_source(self, combo: QComboBox, entry: Entry | None) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) is entry:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    def _hex_field(self, tip: str, width_chars: int = 10) -> QLineEdit:
        field = QLineEdit()
        field.setToolTip(tip)
        field.setFixedWidth(field.fontMetrics().horizontalAdvance("0" * width_chars))
        field.editingFinished.connect(self.changed.emit)
        return field


class RegionRow(_Row):
    """A **region** input: a source, an offset and a length."""

    use_selection_requested = Signal(object)  # this row
    go_to_requested = Signal(object)  # the RegionBinding shown

    def __init__(self, spec: InputSpec, sources: list[Entry]) -> None:
        super().__init__(spec, sources)
        self._source = self._source_combo()
        self._source.currentIndexChanged.connect(lambda _i: self.changed.emit())
        self._offset = self._hex_field("Offset (hex; $ and 0x accepted)")
        self._length = self._hex_field(
            f"Length in bytes (hex); a whole number of\n"
            f"{spec.stride}-byte {spec.unit or 'element'}s"
            if spec.stride > 1
            else "Length in bytes (hex)"
        )
        self._count = QLabel()
        self._use = QPushButton("Use selection")
        self._use.setToolTip(
            "Fill the source, offset and length from the\n"
            "byte or tile selection of the entry on screen"
        )
        self._use.clicked.connect(lambda: self.use_selection_requested.emit(self))
        self._go = QPushButton("Go to")
        self._go.setToolTip("Show the source with this region in view")
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
        offset = parse_hex(self._offset.text())
        length = parse_hex(self._length.text())
        if offset is None or length is None:
            return None
        return RegionBinding(
            entry=self._source.currentData(), offset=offset, length=length
        )

    def set_binding(self, binding: InputBinding | None) -> None:
        with signals_blocked(self._source, self._offset, self._length):
            if isinstance(binding, RegionBinding):
                self._select_source(self._source, binding.entry)
                self._offset.setText(format_hex(binding.offset))
                self._length.setText(format_hex(binding.length))
            else:
                self._source.setCurrentIndex(0)
                self._offset.clear()
                self._length.clear()
        self._refresh_count()

    def _refresh_count(self) -> None:
        length = parse_hex(self._length.text())
        self._count.setText(_count(length, self.spec) if length is not None else "")
        self._go.setEnabled(self.binding() is not None)

    def _emit_go_to(self) -> None:
        binding = self.binding()
        if binding is not None:
            self.go_to_requested.emit(binding)


class IntegerRow(_Row):
    """An **integer** input: a literal, or a number read out of bytes."""

    use_selection_requested = Signal(object)
    go_to_requested = Signal(object)

    def __init__(self, spec: InputSpec, sources: list[Entry]) -> None:
        super().__init__(spec, sources)
        self._form = CompactComboBox(SHORT_COMBO_WIDTH)
        self._form.addItem("Literal", "literal")
        self._form.addItem("From bytes", "bytes")
        self._form.setToolTip(
            "Literal - a number typed here\n"
            "From bytes - read the number out of the file at an\n"
            "offset, so an edit there is followed"
        )
        self._form.currentIndexChanged.connect(self._on_form_change)
        self._literal = self._hex_field(
            f"The value (hex; $ and 0x accepted),\n"
            f"{spec.minimum:#x} to {spec.maximum:#x}"
        )
        if spec.default is not None and not spec.required:
            # Left empty, the codec is handed the default: say which.
            self._literal.setPlaceholderText(f"{spec.default:#x}")
        self._source = self._source_combo()
        self._source.currentIndexChanged.connect(lambda _i: self.changed.emit())
        self._offset = self._hex_field("Offset of the number (hex)")
        self._width = QSpinBox()
        self._width.setRange(1, 8)
        self._width.setValue(2)
        self._width.setSuffix(" B")
        self._width.setToolTip("How many bytes wide the number is")
        self._width.valueChanged.connect(lambda _v: self.changed.emit())
        self._endian = CompactComboBox(SHORT_COMBO_WIDTH)
        self._endian.addItem("big-endian", False)
        self._endian.addItem("little-endian", True)
        self._endian.currentIndexChanged.connect(lambda _i: self.changed.emit())
        self._hint = QLabel()
        self._use = QPushButton("Use selection")
        self._use.setToolTip(
            "Read the number from the start of the selection of\n"
            "the entry on screen, as wide as the selection up to\n"
            "8 bytes"
        )
        self._use.clicked.connect(lambda: self.use_selection_requested.emit(self))
        self._go = QPushButton("Go to")
        self._go.setToolTip("Show the source with the number in view")
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
            offset = parse_hex(self._offset.text())
            if offset is None:
                return None
            return IntegerFromBytes(
                entry=self._source.currentData(),
                offset=offset,
                width=self._width.value(),
                little_endian=bool(self._endian.currentData()),
            )
        return parse_hex(self._literal.text())

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
                self._offset.setText(format_hex(binding.offset))
                self._width.setValue(binding.width)
                self._endian.setCurrentIndex(1 if binding.little_endian else 0)
            else:
                self._form.setCurrentIndex(0)
                self._literal.setText(
                    f"{binding:#x}"
                    if isinstance(binding, int) and not isinstance(binding, bool)
                    else ""
                )
        self._on_form_change()

    def set_read_value(self, value: int | None) -> None:
        """What a From-bytes binding currently reads, from the resolver."""
        self._read = value
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        value = getattr(self, "_read", None) if self._from_bytes() else self.binding()
        unit = f" {self.spec.unit}s" if self.spec.unit else ""
        if isinstance(value, int):
            self._hint.setText(f"= {value:,}{unit}")
        elif (
            not self._from_bytes()
            and not self._literal.text().strip()
            and not self.spec.required
            and self.spec.default is not None
        ):
            # Nothing typed hands the codec its default, so show that number.
            self._hint.setText(f"= {self.spec.default:,}{unit} (default)")
        else:
            self._hint.setText("")
        self._go.setEnabled(self._from_bytes() and self.binding() is not None)

    def _emit_go_to(self) -> None:
        binding = self.binding()
        if isinstance(binding, IntegerFromBytes):
            self.go_to_requested.emit(binding)


def _row_for(spec: InputSpec, sources: list[Entry]) -> _Row:
    if spec.kind is InputKind.INTEGER:
        return IntegerRow(spec, sources)
    return RegionRow(spec, sources)


class InputsWindow(QWidget):
    """Floating editor for one entry's input bindings."""

    #: ``(plugin id -> bindings, apply to the selection)``: the user pressed Apply.
    apply_requested = Signal(object, bool)
    #: A row asked for the selection on screen; the main window answers by
    #: calling :meth:`fill_from_selection` with what it found.
    use_selection_requested = Signal(object)
    #: A row asked to show its source — a RegionBinding or IntegerFromBytes.
    go_to_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle("Inputs")
        self._entry: Entry | None = None
        self._rows: dict[tuple[str, str], _Row] = {}
        self._validate: Callable[[dict[str, Bindings]], Problems] | None = None
        self._read_values: Callable[[dict[str, Bindings]], dict] | None = None

        outer = QVBoxLayout(self)
        self._sections = QVBoxLayout()
        outer.addLayout(self._sections)
        self._sections_widgets: list[QWidget] = []

        scope = QHBoxLayout()
        scope.addWidget(QLabel("Apply to:"))
        self._this = QRadioButton("this entry")
        self._this.setChecked(True)
        self._selected = QRadioButton("the selected entries")
        self._selected.setToolTip(
            "Every entry selected in the Files pane whose plugin\n"
            "declares the same inputs; a field left as it is here\n"
            "is left as it is on each of them"
        )
        scope.addWidget(self._this)
        scope.addWidget(self._selected)
        scope.addStretch(1)
        self._scope_row = QWidget()
        self._scope_row.setLayout(scope)
        outer.addWidget(self._scope_row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._apply = QPushButton("Apply")
        self._apply.setDefault(True)
        self._apply.clicked.connect(self._on_apply)
        close = QPushButton("Close")
        close.clicked.connect(self.hide)
        buttons.addWidget(self._apply)
        buttons.addWidget(close)
        outer.addLayout(buttons)

    # -- populating ------------------------------------------------------------
    @property
    def entry(self) -> Entry | None:
        """The entry this window is pinned to, or ``None`` while hidden."""
        return self._entry

    def show_for(
        self,
        entry: Entry,
        sections: list[Section],
        sources: list[Entry],
        *,
        selected_count: int,
        validate: Callable[[dict[str, Bindings]], Problems],
        read_values: Callable[[dict[str, Bindings]], dict] | None = None,
        blocking: bool = False,
    ) -> None:
        """Rebuild the form for ``entry`` and show it.

        ``sections`` is what the entry's stages declare and currently bind;
        ``sources`` the entries a region may name (already filtered by the
        one-hop rule); ``validate`` answers, for a candidate binding map, which
        keys do not resolve and why; ``read_values`` what each From-bytes
        integer currently reads, keyed like the problems.

        ``blocking`` is for the one caller that is itself a modal dialog — the
        Slice dialog's inputs badge. A window merely stacked above a modal one is
        drawn and then ignores every click, so the editor has to take the top of
        the modal stack itself; the price is that *Use selection* can only read
        the selection already on screen, since the main window is blocked too.
        """
        self._set_blocking(blocking)
        self._entry = entry
        self._validate = validate
        self._read_values = read_values
        self.setWindowTitle(f"Inputs - {entry.name}")
        for widget in self._sections_widgets:
            self._sections.removeWidget(widget)
            widget.deleteLater()
        self._sections_widgets = []
        self._rows = {}
        for declared, plugin_name, bindings in sections:
            box = QGroupBox(
                f"{_STAGE_CAPTIONS.get(declared.stage, declared.stage.value)}  "
                f"{plugin_name}"
            )
            grid = QGridLayout(box)
            for line, spec in enumerate(declared.specs):
                row = _row_for(spec, sources)
                row.set_binding(bindings.get(spec.key))
                row.changed.connect(self._revalidate)
                row.use_selection_requested.connect(self.use_selection_requested.emit)
                row.go_to_requested.connect(self.go_to_requested.emit)
                label = QLabel(spec.label)
                label.setToolTip(spec.tooltip or spec.label)
                grid.addWidget(label, line, 0)
                cells = QHBoxLayout()
                cells.setContentsMargins(0, 0, 0, 0)
                for widget in row.widgets():
                    cells.addWidget(widget)
                cells.addStretch(1)
                holder = QWidget()
                holder.setLayout(cells)
                grid.addWidget(holder, line, 1)
                grid.addWidget(row.status_widget(), line, 2)
                self._rows[(declared.plugin_id, spec.key)] = row
            self._sections.addWidget(box)
            self._sections_widgets.append(box)
        self._scope_row.setVisible(selected_count > 1)
        self._selected.setText(f"the {selected_count} selected entries")
        self._this.setChecked(True)
        self._revalidate()
        self.show()
        self.raise_()
        self.activateWindow()

    def _set_blocking(self, blocking: bool) -> None:
        """Take (or give back) the top of the modal stack — see :meth:`show_for`.

        Qt only reads a modality change on a window that is hidden, so a window
        already up for another entry is dropped first; it is rebuilt and shown
        again either way.
        """
        wanted = (
            Qt.WindowModality.ApplicationModal
            if blocking
            else Qt.WindowModality.NonModal
        )
        if self.windowModality() != wanted:
            self.hide()
            self.setWindowModality(wanted)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # Qt override
        """Escape closes it, the way it closes every other window in the app.

        A plain ``QWidget`` inherits none of ``QDialog``'s key handling, so the
        one gesture a floating editor has to answer had to be spelled out. It is
        the Close button's move rather than a Cancel: nothing typed here is live
        until Apply, so closing discards the unapplied rows either way — and it
        is the only way out of the window while it is blocking a Slice dialog
        that has taken the keyboard's other exits.
        """
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def hide_overlay(self) -> None:
        """Hide — the entry it was pinned to is gone."""
        self._entry = None
        if self.isVisible():
            self.hide()

    # -- what the form says ------------------------------------------------------
    def bindings(self) -> dict[str, Bindings]:
        """Every bound row, by plugin id then key; unbound rows are absent."""
        out: dict[str, Bindings] = {}
        for (plugin_id, key), row in self._rows.items():
            binding = row.binding()
            if binding is not None:
                out.setdefault(plugin_id, {})[key] = binding
        return out

    def fill_from_selection(self, row: _Row, binding: InputBinding | None) -> None:
        """Answer a *Use selection* request: ``None`` means nothing usable was
        selected, and the row is left alone."""
        if binding is None:
            return
        row.set_binding(binding)
        self._revalidate()

    def _revalidate(self) -> None:
        if self._validate is None:
            return
        candidate = self.bindings()
        problems = self._validate(candidate)
        values = self._read_values(candidate) if self._read_values is not None else {}
        for key, row in self._rows.items():
            row.set_problem(problems.get(key))
            if isinstance(row, IntegerRow):
                row.set_read_value(values.get(key))

    def _on_apply(self) -> None:
        self.apply_requested.emit(self.bindings(), self._selected.isChecked())
