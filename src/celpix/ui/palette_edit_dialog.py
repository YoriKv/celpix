"""The Edit Palette window: one colour operation over a set of entries.

A modal input form and nothing else: it knows no document. It holds a copy
of the dock's colours only so that the *Subset* scope can show them for the
user to point at entries; what those indices mean is the host's. Every input
change emits :attr:`PaletteEditDialog.changed`, and the host reads
:meth:`op`, :meth:`scope` and :meth:`subset` back to preview the result live — on the
canvas and on the swatch grid — and to commit it on OK. Cancel is the host's
to honour: it put the preview up, so it takes it down
(``docs/design/palette-editing.md`` §1).

Modal on purpose. The preview is a palette swapped in under the view, and
anything that moves the view meanwhile — a tile click in Offset mode reloads
the palette from the bytes under it — would land a committed edit on colours
other than the ones previewed. Locking the window out is what makes "Cancel
puts everything back" a promise.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import Enum

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from celpix.core.color_ops import BlendMode, OpKind, PaletteOp
from celpix.core.palette import format_argb, parse_argb
from celpix.ui.color_editor import ColorSwatch
from celpix.ui.swatch_subset_picker import SwatchSubsetPicker
from celpix.ui.widgets import (
    CommittingLineEdit,
    dialog_buttons,
    signals_blocked,
)


class ClickableSwatch(ColorSwatch):
    """A colour chip that is also the way into the colour picker.

    The chip already shows the colour the field holds; making it the control
    that changes it keeps the row to the value and its preview, with no
    third button to read. The pointing hand is what says it can be clicked.
    """

    clicked = Signal()

    def __init__(self, size: QSize, parent: QWidget | None = None) -> None:
        super().__init__(size, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to choose the color in a color picker")

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class EditScope(Enum):
    """Which entries an operation reaches; ``value`` is the radio's caption.

    The first four are the palette dock's, over the dock's palette; the last
    two are the swatch view's, over the colour words on the canvas
    (``on_view``), and a window offers one set or the other.
    """

    ROW = "Palette row"
    ALL = "Whole palette"
    SELECTED = "Selected color"
    SUBSET = "Subset"
    ENTIRE_VIEW = "Entire view"
    SELECTION = "Selection"


_DOCK_SCOPES = (EditScope.ROW, EditScope.ALL, EditScope.SELECTED, EditScope.SUBSET)
_VIEW_SCOPES = (EditScope.ENTIRE_VIEW, EditScope.SELECTION)


# Which inputs each operation reads; the rest are hidden rather than greyed,
# so the form is only ever as tall as the operation needs.
_USES_COLOR = {OpKind.COLORIZE, OpKind.BLEND, OpKind.SET}
_USES_AMOUNT = {OpKind.GRAYSCALE, OpKind.COLORIZE, OpKind.BLEND}
_USES_HSV = {OpKind.HSV_SHIFT}


class PaletteEditDialog(QDialog):
    """Pick an operation, its inputs, and the entries to apply it to."""

    # Any input moved; the host re-previews from :meth:`op` / :meth:`scope`.
    changed = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        selected_color: int | None = None,
        rows: bool = True,
        on_view: bool = False,
        has_selection: bool = False,
        colors: Sequence[int] = (),
        selected_index: int | None = None,
    ) -> None:
        """``selected_color`` is the swatch grid's selection, or ``None`` when
        nothing is selected — then *Selected color* is not offered and the
        colour input starts white instead of on it. ``rows`` is whether the
        pixel format has palette rows at all: a direct-colour view indexes
        nothing, so *Palette row* is not offered there and the whole palette
        is the default instead.

        ``colors`` is the dock's palette as it stands when the window opens,
        shown as a swatch grid under the scope row while *Subset* is checked
        so the user can pick the entries — the originals, not the preview —
        and *Subset* is offered only with some. ``selected_index`` is the
        swatch grid's selected entry, which the subset starts on.

        ``on_view`` is the toolbar's window instead of the dock's: the
        operation reaches the swatches on the canvas rather than the dock's
        palette, so the scopes offered are *Entire view* and *Selection* —
        the latter only with a canvas selection (``has_selection``), and the
        default when there is one, since a selection made before opening the
        window is what the user means to recolour. Which swatches either
        names is the host's to work out — this form knows no document. The
        window never offers *Subset*: its swatches are the canvas's, picked
        with the canvas selection."""
        super().__init__(parent)
        self.setWindowTitle("Edit Palette")
        self._selected_color = selected_color
        self._color = (
            0xFFFFFFFF if selected_color is None else selected_color | 0xFF000000
        )

        self._op = QComboBox()
        for kind in OpKind:
            self._op.addItem(kind.value, kind)
        self._op.setToolTip(
            "Invert: every channel flipped\n"
            "Grayscale: each color to its own brightness\n"
            "Colorize: the color's hue on each entry's brightness\n"
            "HSV Shift: turn every hue, and add to saturation and value\n"
            "Blend: the color laid over each entry in a blend mode\n"
            "Set to Color: every entry becomes the color"
        )
        self._op.currentIndexChanged.connect(self._on_op_change)

        self._mode = QComboBox()
        for mode in BlendMode:
            self._mode.addItem(mode.value, mode)
        self._mode.setToolTip("How the color combines with each entry")
        self._mode.currentIndexChanged.connect(self._announce)

        # The colour as six hex digits: the operations keep each entry's alpha,
        # so an alpha typed here would be ignored and is not offered.
        self._hex = CommittingLineEdit(parse_argb, self._hex_text)
        self._hex.setFixedWidth(80)
        self._hex.setToolTip("#RRGGBB")
        self._hex.committed.connect(self._on_hex)
        self._swatch = ClickableSwatch(QSize(36, 22))
        self._swatch.set_color(self._color)
        self._swatch.clicked.connect(self._on_pick)
        self._use_selected = QPushButton("Use Selected")
        self._use_selected.setToolTip("Take the swatch grid's selected color")
        self._use_selected.setEnabled(selected_color is not None)
        self._use_selected.clicked.connect(self._on_use_selected)

        amount_tip = "How far each entry moves toward the result"
        self._amount, self._amount_spin, amount_row = self._slider_row(
            0, 100, 100, "%", amount_tip
        )
        hue_tip = "Degrees to turn every entry's hue, around the wheel"
        self._hue, self._hue_spin, hue_row = self._slider_row(
            -180, 180, 0, "°", hue_tip
        )
        saturation_tip = "Added to every entry's saturation, then clamped"
        self._saturation, self._saturation_spin, saturation_row = self._slider_row(
            -100, 100, 0, "%", saturation_tip
        )
        value_tip = "Added to every entry's value (brightness), then clamped"
        self._value, self._value_spin, value_row = self._slider_row(
            -100, 100, 0, "%", value_tip
        )

        # The subset is picked against the colours the window opened on; the
        # preview recolours the dock's grid, not this one, so the entries stay
        # recognisable while the operation changes them.
        self._picker = SwatchSubsetPicker()
        self._picker.set_colors(colors)
        if selected_index is not None and 0 <= selected_index < len(colors):
            self._picker.set_selection([selected_index])
        self._picker.selection_changed.connect(self._announce)
        subset_tip = (
            "The entries the operation changes:\n"
            "click one, Shift+click a range, Ctrl+click to toggle,\n"
            "or drag a rectangle. The colors are the palette\n"
            "as it was when the window opened."
        )

        # Before the radios: checking one re-syncs the rows, which must find
        # none yet while the form is being built.
        self._rows: dict[str, tuple[QLabel, QWidget]] = {}
        self._scopes: dict[EditScope, QRadioButton] = {}
        scope_row = QHBoxLayout()
        for scope in _VIEW_SCOPES if on_view else _DOCK_SCOPES:
            radio = QRadioButton(scope.value)
            radio.toggled.connect(self._on_scope_toggle)
            self._scopes[scope] = radio
            scope_row.addWidget(radio)
        if on_view:
            self._scopes[EditScope.SELECTION].setEnabled(has_selection)
            self._scopes[EditScope.SELECTION].setToolTip(
                "The swatches selected on the canvas"
            )
            self._scopes[
                EditScope.SELECTION if has_selection else EditScope.ENTIRE_VIEW
            ].setChecked(True)
        else:
            self._scopes[EditScope.ROW if rows else EditScope.ALL].setChecked(True)
            self._scopes[EditScope.ROW].setEnabled(rows)
            self._scopes[EditScope.SELECTED].setEnabled(selected_color is not None)
            self._scopes[EditScope.SUBSET].setEnabled(bool(colors))
            self._scopes[EditScope.SUBSET].setToolTip(
                "The entries picked in the grid below"
            )
        scope_row.addStretch(1)

        subset_row = QHBoxLayout()
        subset_row.addWidget(self._picker)
        subset_row.addStretch(1)

        color_row = QHBoxLayout()
        color_row.addWidget(self._hex)
        color_row.addWidget(self._swatch)
        color_row.addWidget(self._use_selected)
        color_row.addStretch(1)

        # A grid of caption + input rows, so hiding a row leaves no gap and the
        # captions stay one column however many rows are showing.
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        rows = [
            ("op", "Operation:", self._op, self._op.toolTip()),
            ("mode", "Blend mode:", self._mode, self._mode.toolTip()),
            ("color", "Color:", color_row, self._hex.toolTip()),
            ("amount", "Amount:", amount_row, amount_tip),
            ("hue", "Hue:", hue_row, hue_tip),
            ("saturation", "Saturation:", saturation_row, saturation_tip),
            ("value", "Value:", value_row, value_tip),
            (
                "scope",
                "Apply to:",
                scope_row,
                "Which swatches the operation changes"
                if on_view
                else "Which entries the operation changes",
            ),
        ]
        if not on_view:
            rows.append(("subset", "Subset:", subset_row, subset_tip))
        for at, (name, caption, widget_or_row, tip) in enumerate(rows):
            if isinstance(widget_or_row, QWidget):
                holder = widget_or_row
            else:
                holder = QWidget()
                widget_or_row.setContentsMargins(0, 0, 0, 0)
                holder.setLayout(widget_or_row)
            label = QLabel(caption)
            label.setToolTip(tip)
            holder.setToolTip(holder.toolTip() or tip)
            grid.addWidget(label, at, 0)
            grid.addWidget(holder, at, 1)
            self._rows[name] = (label, holder)

        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        dialog_buttons(self, layout)
        self._sync_rows()

    def _slider_row(
        self, low: int, high: int, start: int, suffix: str, tip: str
    ) -> tuple[QSlider, QSpinBox, QHBoxLayout]:
        """A slider and a spin on one value, announcing each move once."""
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(low, high)
        slider.setValue(start)
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setValue(start)
        spin.setSuffix(suffix)
        for widget in (slider, spin):
            widget.setToolTip(tip)
        slider.valueChanged.connect(lambda v: self._on_slider(slider, spin, v))
        spin.valueChanged.connect(lambda v: self._on_slider(slider, spin, v))
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(spin)
        return slider, spin, row

    # -- what the host reads ----------------------------------------------
    def op(self) -> PaletteOp:
        return PaletteOp(
            self._op.currentData(),
            color=self._color,
            amount=self._amount.value() / 100,
            mode=self._mode.currentData(),
            hue=float(self._hue.value()),
            saturation=self._saturation.value() / 100,
            value=self._value.value() / 100,
        )

    def scope(self) -> EditScope:
        for scope, radio in self._scopes.items():
            if radio.isChecked():
                return scope
        return next(iter(self._scopes))

    def subset(self) -> list[int]:
        """The entries picked for *Subset*, sorted; read whatever the scope."""
        return self._picker.selection()

    # -- programmatic setters, for the host and for tests -----------------
    def set_op(self, kind: OpKind) -> None:
        self._op.setCurrentIndex(self._op.findData(kind))

    def set_mode(self, mode: BlendMode) -> None:
        self._mode.setCurrentIndex(self._mode.findData(mode))

    def set_color(self, argb: int) -> None:
        self._apply_color(argb)

    def set_amount(self, percent: int) -> None:
        self._amount.setValue(percent)

    def set_hsv_shift(self, hue: int, saturation: int, value: int) -> None:
        """Degrees, and the two as percentages."""
        self._hue.setValue(hue)
        self._saturation.setValue(saturation)
        self._value.setValue(value)

    def set_scope(self, scope: EditScope) -> None:
        self._scopes[scope].setChecked(True)

    def set_subset(self, indices: Iterable[int]) -> None:
        self._picker.set_selection(indices)

    # -- input handling ----------------------------------------------------
    def _announce(self, *_args: object) -> None:
        self.changed.emit()

    def _on_op_change(self, _index: int) -> None:
        self._sync_rows()
        self.changed.emit()

    def _on_scope_toggle(self, checked: bool) -> None:
        # Radios announce twice per change (the one going off, the one coming
        # on); only the arrival is a new answer. Empty while the form is
        # still being built, before there are rows to show or hide.
        if checked:
            if self._rows:
                self._sync_rows()
            self.changed.emit()

    def _on_slider(self, slider: QSlider, spin: QSpinBox, value: int) -> None:
        with signals_blocked(slider, spin):
            slider.setValue(value)
            spin.setValue(value)
        self.changed.emit()

    def _on_pick(self) -> None:
        """The platform's colour picker, opening on the current colour.

        Its answer is invalid when it was cancelled, which leaves the colour
        alone; its alpha is dropped like any other, since no operation reads
        one.
        """
        picked = QColorDialog.getColor(
            QColor.fromRgba(self._color), self, "Edit Palette - Color"
        )
        if picked.isValid():
            self._apply_color(picked.rgb())

    def _on_hex(self, argb: object) -> None:
        self._apply_color(int(argb))

    def _on_use_selected(self) -> None:
        if self._selected_color is not None:
            self._apply_color(self._selected_color)

    def _apply_color(self, argb: int) -> None:
        argb = (argb & 0xFFFFFFFF) | 0xFF000000  # opaque: alpha is not an input
        self._hex.refresh()  # a typed spelling normalises even when unchanged
        if argb == self._color:
            return
        self._color = argb
        self._hex.refresh()
        self._swatch.set_color(argb)
        self.changed.emit()

    def _hex_text(self) -> str:
        return format_argb(self._color, alpha=False)

    def _sync_rows(self) -> None:
        """Show only the inputs the chosen operation reads, and the subset's
        grid only while *Subset* is the scope."""
        kind = self._op.currentData()
        for name, shown in (
            ("mode", kind is OpKind.BLEND),
            ("color", kind in _USES_COLOR),
            ("amount", kind in _USES_AMOUNT),
            ("hue", kind in _USES_HSV),
            ("saturation", kind in _USES_HSV),
            ("value", kind in _USES_HSV),
            ("subset", self.scope() is EditScope.SUBSET),
        ):
            for widget in self._rows.get(name, ()):
                widget.setVisible(shown)
        self.adjustSize()
