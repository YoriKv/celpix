"""Small reusable UI widgets and shared painting idioms.

Qt lives here (this is the ``ui`` layer); the model stays Qt-free.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import Enum
from typing import TypeVar

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPointF,
    QRect,
    QSettings,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

_EnumT = TypeVar("_EnumT", bound=Enum)

# The canvas editing shortcuts (Cut/Copy/Paste/Select All/Delete). The main
# window binds these window-wide (see ``SelectionMixin``), so they otherwise fire
# wherever focus is - acting on the *canvas* selection even when a side panel has
# focus. A panel that wants to own its keys claims them with
# :func:`take_editing_shortcut`. Matched by key sequence, so platform bindings
# track without hard-coded literals.
_EDITING_SHORTCUTS = (
    QKeySequence.StandardKey.Cut,
    QKeySequence.StandardKey.Copy,
    QKeySequence.StandardKey.Paste,
    QKeySequence.StandardKey.SelectAll,
    QKeySequence.StandardKey.Delete,
)


def take_editing_shortcut(event: QEvent) -> bool:
    """Claim a canvas editing shortcut for a focused panel; call from ``event()``.

    Returns True (having *accepted* ``event``) when it is a ``ShortcutOverride``
    for one of :data:`_EDITING_SHORTCUTS`. Accepting the override routes the key
    to the focused widget as a normal press instead of letting the canvas's
    window-wide shortcut consume (or, for Delete, ambiguously drop) it - so a
    panel that has its own key handling isn't shadowed by the editing surface
    behind it. The widget then handles the resulting key press however it likes
    (or ignores it, so the key simply does nothing there). Mirrors how the
    app-wide arrow-key filter yields to these same panels, and how text inputs
    already claim their editing keys natively.
    """
    if event.type() == QEvent.Type.ShortcutOverride and any(
        event.matches(key) for key in _EDITING_SHORTCUTS
    ):
        event.accept()
        return True
    return False


@contextmanager
def signals_blocked(*widgets: QObject) -> Iterator[None]:
    """Set widget state without the handlers firing back.

    The recurring need behind it: restoring a session, applying a preset, or
    correcting a clamped value pushes several widgets at once, and each one's
    ``valueChanged``/``toggled`` would otherwise trigger its own re-render — so
    what should be one coherent swap becomes a cascade of partial reloads (and,
    where a handler writes back, a re-entrant one). The caller re-renders once
    afterwards instead.

    Each widget's *previous* blocked state is restored rather than assumed
    ``False``, so nesting this inside an outer block doesn't unblock early.
    """
    previous = [widget.blockSignals(True) for widget in widgets]
    try:
        yield
    finally:
        for widget, was_blocked in zip(widgets, previous):
            widget.blockSignals(was_blocked)


def select_combo_data(combo: QComboBox, data: object) -> None:
    """Select the item carrying ``data``, signals blocked, no-op if absent.

    The one signal-safe combo snap used everywhere a selection is set
    programmatically — session restore, the undo apply-helpers, and every
    load-failed revert. Leaving the selection unchanged when nothing matches is
    deliberate: a plugin refresh can drop a preset out from under a stored id,
    and a bare ``setCurrentIndex(-1)`` would blank the box instead.
    """
    with signals_blocked(combo):
        index = combo.findData(data)
        if index >= 0:
            combo.setCurrentIndex(index)


def paint_selection_outline(painter: QPainter, rect: QRect) -> None:
    """The app's shared selection outline: a white ring over a black one.

    One outline language for every "this is the active thing" highlight (the
    canvas's tile selection, the palette panel's active subpalette). Two 1px
    layers rather than one line: whichever color the art under the edge happens
    to be, the other layer still shows, so the outline never disappears into it.
    Both are fixed colors — the highlight stays put whatever the theme is and
    wherever focus is, because the selection is the state, not the focus.

    The white layer sits flush on the selected area's boundary and the black one
    just inside it, so the whole 2px band lands *within* ``rect``: an aliased
    ``drawRect`` renders one pixel past its path, hence the -1 insets.
    """
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(255, 255, 255), 1))
    painter.drawRect(rect.adjusted(0, 0, -1, -1))
    painter.setPen(QPen(QColor(0, 0, 0), 1))
    painter.drawRect(rect.adjusted(1, 1, -2, -2))


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


def funnel_icon(color: QColor, size: int = 16, ratio: float = 1.0) -> QIcon:
    """A funnel/filter glyph filled with ``color`` — the app's "filter a list" mark.

    Painted rather than bundled so it inherits the current theme's text color and
    stays crisp at any device-pixel ratio; Qt derives the disabled (greyed) form
    from it automatically. The silhouette sits in a padded unit box: a wide mouth
    converging to a short stem.
    """
    px = max(1, round(size * ratio))
    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    unit = [
        (0.12, 0.18),
        (0.88, 0.18),
        (0.58, 0.52),
        (0.58, 0.86),
        (0.42, 0.86),
        (0.42, 0.52),
    ]
    painter.drawPolygon(QPolygonF([QPointF(x * px, y * px) for x, y in unit]))
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return QIcon(pixmap)


class ChecklistPopupButton(QToolButton):
    """A toolbar button that drops down a checkable list, with Select All / None.

    A compact multi-select filter: the owner supplies the current entries each
    time the popup opens (so a source list that changes — e.g. a plugin refresh
    adds a preset — stays in sync), and every change is handed to ``apply``,
    which does the real work and returns the set that ended up in force. The
    button then re-syncs its checkboxes to that set, so a request the owner had
    to clamp — you can never hide *everything* — visibly springs back. All of
    the filtering/selection logic lives with the owner and is unit-tested
    without driving this view.

    The popup is a top-level ``Qt.Popup``: clicks inside it (the checkboxes, the
    two buttons) leave it open, and a click anywhere else dismisses it — so the
    list stays up while several boxes are toggled.
    """

    def __init__(
        self,
        text: str,
        items: Callable[[], list[tuple[object, str, bool]]],
        apply: Callable[[set], set],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setText(text)
        self._items = items
        self._apply = apply
        self._boxes: dict[object, QCheckBox] = {}
        self._popup: QWidget | None = None
        self.clicked.connect(self._open)

    def _open(self) -> None:
        popup = QWidget(self, Qt.WindowType.Popup)
        outer = QVBoxLayout(popup)
        buttons = QHBoxLayout()
        select_all = QPushButton("Select All")
        select_none = QPushButton("Select None")
        select_all.clicked.connect(lambda: self._bulk(True))
        select_none.clicked.connect(lambda: self._bulk(False))
        buttons.addWidget(select_all)
        buttons.addWidget(select_none)
        outer.addLayout(buttons)

        # The source list can be long (dozens of codecs); scroll rather than grow
        # a popup taller than the screen.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(400)
        inner = QWidget()
        column = QVBoxLayout(inner)
        self._boxes = {}
        for key, label, checked in self._items():
            box = QCheckBox(label)
            box.setChecked(checked)
            box.toggled.connect(self._on_toggle)
            self._boxes[key] = box
            column.addWidget(box)
        column.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        popup.adjustSize()
        popup.move(self.mapToGlobal(self.rect().bottomLeft()))
        popup.show()
        self._popup = popup  # keep a reference so it isn't collected mid-show

    def _checked_keys(self) -> set:
        return {key for key, box in self._boxes.items() if box.isChecked()}

    def _on_toggle(self, *_args) -> None:
        self._sync(self._apply(self._checked_keys()))

    def _bulk(self, checked: bool) -> None:
        self._sync(self._apply(set(self._boxes) if checked else set()))

    def _sync(self, effective: set) -> None:
        """Reflect the owner's authoritative set back onto the checkboxes."""
        for key, box in self._boxes.items():
            with signals_blocked(box):
                box.setChecked(key in effective)


def load_enum_setting(key: str, default: _EnumT) -> _EnumT:
    """An app-wide appearance/interaction preference out of QSettings.

    The app-global preferences (grid style, selection shape) are stored by their
    enum's string ``value``, so the settings file stays readable and stable. A
    stored value this build has no member for — an older or newer Celpix wrote
    the settings — falls back to ``default`` rather than raising: a stale
    preference is not a reason to fail to start.
    """
    stored = QSettings().value(key, default.value)
    try:
        return type(default)(stored)
    except ValueError:
        return default
