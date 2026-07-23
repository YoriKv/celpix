"""Small reusable UI widgets and shared painting idioms.

Qt lives here (this is the ``ui`` layer); the model stays Qt-free.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import QComboBox, QLineEdit, QWidget


def select_combo_data(combo: QComboBox, data: object) -> None:
    """Select the item carrying ``data``, signals blocked, no-op if absent.

    The one signal-safe combo snap used everywhere a selection is set
    programmatically — session restore, the undo apply-helpers, and every
    load-failed revert. Leaving the selection unchanged when nothing matches is
    deliberate: a plugin refresh can drop a preset out from under a stored id,
    and a bare ``setCurrentIndex(-1)`` would blank the box instead.
    """
    combo.blockSignals(True)
    index = combo.findData(data)
    if index >= 0:
        combo.setCurrentIndex(index)
    combo.blockSignals(False)


def paint_selection_outline(painter: QPainter, palette: QPalette, rect: QRect) -> None:
    """The app's shared selection outline: accent ring with a white inset.

    One outline language for every "this is the active thing" highlight (the
    canvas's tile selection, the palette panel's active subpalette), readable
    on dark and light content alike. The Active colour group is forced so the
    outline doesn't dim when focus is elsewhere — the selection itself is the
    state, not the focus.
    """
    painter.setBrush(Qt.BrushStyle.NoBrush)
    accent = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Highlight)
    painter.setPen(QPen(accent, 2))
    painter.drawRect(rect.adjusted(1, 1, -1, -1))
    painter.setPen(QPen(QColor(255, 255, 255, 220), 1))
    painter.drawRect(rect.adjusted(2, 2, -2, -2))


class CompactComboBox(QComboBox):
    """A combo box whose closed button is a fraction of its natural width.

    A stock combo reserves the full width of its longest item, which long entry
    names turn into a lot of dead toolbar space. Scaling the size *hints*
    (rather than fixing a pixel width) keeps the width proportional to the
    contents, including after a repopulation — ``AdjustToContents`` makes Qt
    re-query the hint whenever the model changes. The popup list is given back
    the full content width, so entries stay readable while choosing.
    """

    # Emitted when the box loses focus for real — i.e. the user moved on to
    # another widget, not merely opened this box's own popup (which also fires a
    # focus-out, with PopupFocusReason). Lets a screen hold scratch state alive
    # across consecutive selections and drop it the moment focus leaves.
    focus_lost = Signal()

    def __init__(self, scale: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scale = scale
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)

    def focusOutEvent(self, event) -> None:  # Qt override
        super().focusOutEvent(event)
        if event.reason() != Qt.FocusReason.PopupFocusReason:
            self.focus_lost.emit()

    def _scaled(self, hint: QSize) -> QSize:
        hint.setWidth(round(hint.width() * self._scale))
        return hint

    def sizeHint(self) -> QSize:  # Qt override
        return self._scaled(super().sizeHint())

    def minimumSizeHint(self) -> QSize:  # Qt override
        return self._scaled(super().minimumSizeHint())

    def showPopup(self) -> None:  # Qt override
        # The popup would inherit the narrowed button width; re-widen it to the
        # longest item (plus scrollbar room) so no entry is elided.
        view = self.view()
        scrollbar = view.verticalScrollBar()
        width = view.sizeHintForColumn(0) + scrollbar.sizeHint().width()
        view.setMinimumWidth(max(self.width(), width))
        super().showPopup()


class CommittingLineEdit(QLineEdit):
    """A free-text field that commits on edit-finish and self-normalises.

    Free-text fields that parse into a value (a hex offset, a dec/hex number, a
    palette index) all share one subtle correctness requirement, and one Qt
    gotcha that makes it easy to get wrong:

    - **Commit, don't stream.** The value should apply when the user finishes
      editing (Enter / focus-out), not on every keystroke — otherwise a
      half-typed value fires repeatedly.
    - **Always re-render on commit — even while focused.** An invalid entry must
      revert to the current value, and a valid one must show its *canonical* form
      (e.g. a tile-snapped, ``0x``-prefixed offset). The trap: ``editingFinished``
      fires on Enter *and* on focus-out, but Qt won't fire it again on a
      focus-out whose text is unchanged since the Enter. So if you skip the
      re-render while the field has focus (the usual guard against clobbering
      mid-typing), an invalid value committed with Enter lingers — the later
      focus-out never corrects it. Re-rendering unconditionally here closes that.

    Wiring it up: pass ``parse`` (text → value, or ``None`` when invalid) and
    ``current_text`` (a callable returning the canonical display string for the
    *current* committed state). On a valid commit the widget emits
    :attr:`committed` with the parsed value — the owner applies it (which may
    clamp/transform the underlying state) — and then the widget re-renders from
    ``current_text``, so the box always reflects the true post-commit state.
    """

    committed = Signal(object)  # the parsed value, on a valid commit

    def __init__(
        self,
        parse: Callable[[str], object | None],
        current_text: Callable[[], str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._parse = parse
        self._current_text = current_text
        self.editingFinished.connect(self.commit)

    def refresh(self) -> None:
        """Set the displayed text to the canonical current value."""
        self.setText(self._current_text())

    def commit(self) -> None:
        """Parse the text; emit :attr:`committed` if valid, then always re-render.

        Unconditional re-render is the point — see the class docstring: it reverts
        invalid input and normalises valid input regardless of focus.
        """
        value = self._parse(self.text())
        if value is not None:
            self.committed.emit(value)
        self.refresh()
