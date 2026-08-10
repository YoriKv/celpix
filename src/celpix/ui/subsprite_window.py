"""The Subsprites window — every piece a sprite map is built from, as a sheet.

A sprite map's canvas shows its frames: the object assembled, which is a picture
of what the file *draws*. A frame is a heap of subsprites at signed pixel offsets
and the front ones cover the back ones, so it is not a picture of what the file
*holds*. This is that — one square per record, in frame order, repeats included
(``docs/design/tilemap-entry.md`` §6).

It is a `Qt.Tool` window on the animation player's pattern
(:mod:`celpix.ui.animation_overlay`): floats above the main window, takes no
taskbar slot, placed beside it on the first show and left where the user drags it
after, with its layout kept across runs. The two are neighbours in the View menu
and are the two second readings a sprite map has — but where the player is
offered only on an object with a sequence to play, **every** sprite map has
subsprites, so this one is offered on all of them.

**Its zoom and its width are its own**, deliberately not the main view's. Cols
here lays out the sheet of records; Cols on the binding bar lays out the strip of
frames, and neither is a reading of the other. The panel reports Ctrl+wheel and
space-drag and owns no state, while this window holds the level in its Zoom spin
and the scrolling in its scroll area — the tile source panel's split
(:mod:`celpix.ui.subsprite_panel`).

**Presentation only.** It is handed a composed sheet, the records it covers and
the one the canvas picked; it never reads the model
(:mod:`celpix.ui.main_window.subsprites`).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSpinBox,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from celpix.ui.subsprite_panel import Box, Record, SubspritePanel
from celpix.ui.widgets import (
    Badge,
    apply_badge,
    pan_scroll_area,
    zoom_anchored,
)
from celpix.ui.window_layout import WindowLayout

# Whole magnifications, like the animation player's and for its reason: there is
# nothing to reduce — a square is one subsprite, not a file too big for the
# window — and the window's own spin is what steps it.
ZOOM_RANGE = (1, 16)
DEFAULT_ZOOM = 3

# How many records across. Its own setting rather than the binding bar's Cols,
# which lays out *frames*: 8 fits a row of pieces beside the object they came
# from without the window having to be as wide as the main one.
COLUMN_RANGE = (1, 64)
DEFAULT_COLUMNS = 8

# How long a burst of view refreshes coalesces into one recompose of the sheet.
# The animation player's debounce, for its reason: the window refreshes on things
# that arrive per pixel of a stroke, and one recompose per burst is the
# difference between a window that is open and one that is in the way.
REFRESH_DEBOUNCE_MS = 120


class SubspriteWindow(QWidget):
    """Floating sheet of one sprite map's subsprite records."""

    #: Ask for the sheet to be recomposed — the entry underneath changed, or the
    #: layout controls here moved. A signal rather than a direct call because the
    #: composing half lives on the main window.
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle("Subsprites")
        self._positioned = False

        self._panel = SubspritePanel()
        self._panel.zoom_requested.connect(self._on_wheel_zoom)
        self._panel.pan_requested.connect(self._pan)
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._panel)
        self._scroll.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self._scroll.setWidgetResizable(False)
        # The backing around the sheet zooms with it: a short object laid 8 across
        # leaves most of the window empty, and that is where the pointer sits.
        self._panel.claim_background(self._scroll)

        self._columns = QSpinBox()
        self._columns.setRange(*COLUMN_RANGE)
        self._columns.setValue(DEFAULT_COLUMNS)
        self._columns.setKeyboardTracking(False)
        self._columns.setToolTip(
            "How many subsprites the sheet is laid out across\n"
            "This window's own width, separate from the canvas\n"
            "Shift+Left/Right steps it"
        )
        self._columns.valueChanged.connect(lambda _v: self.refresh_requested.emit())

        self._zoom = QSpinBox()
        self._zoom.setRange(*ZOOM_RANGE)
        self._zoom.setValue(DEFAULT_ZOOM)
        self._zoom.setKeyboardTracking(False)
        self._zoom.setSuffix("x")
        self._zoom.setToolTip(
            "How big each subsprite is drawn here\n"
            "This window's own zoom, separate from the main view\n"
            "Ctrl+wheel over the sheet steps it; space-drag pans"
        )
        self._zoom.valueChanged.connect(self._panel.set_zoom)
        self._panel.set_zoom(DEFAULT_ZOOM)

        # Which of the two readings the sheet is (`pipeline.subsprite_sheet`).
        # On, the file's own listing — every record, in frame order. Off, the
        # inventory: one square per distinct piece, which is what says how much
        # art an object actually holds where the listing says the same few things
        # over and over.
        self._frames = QCheckBox("Frames")
        self._frames.setChecked(True)
        self._frames.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._frames.setToolTip(
            "Split the sheet across the object's frames\n"
            "On, a square is one record; off, a square is one\n"
            "distinct piece, with the repetition taken out"
        )
        self._frames.toggled.connect(self._on_frames_toggled)

        self._numbers = QCheckBox("Numbers")
        self._numbers.setChecked(True)
        # Takes no focus, the animation player's rule for its transport buttons:
        # space is this window's pan gesture and is claimed window-wide
        # (:meth:`eventFilter`), so a focused checkbox could not toggle itself
        # with it anyway — it would just wear a focus ring for nothing.
        self._numbers.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._numbers.setToolTip(
            "Caption each square with frame:subsprite\n"
            "Dropped when the squares are too small to hold the\n"
            "text, and unavailable with Frames off"
        )
        self._numbers.toggled.connect(lambda _on: self._apply_captions())

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(QLabel("Cols"))
        header.addWidget(self._columns)
        header.addWidget(QLabel("Zoom"))
        header.addWidget(self._zoom)
        header.addWidget(self._frames)
        header.addWidget(self._numbers)
        header.addStretch(1)

        self._status = QStatusBar()
        self._status.setSizeGripEnabled(False)
        self._badge = QLabel()
        self._badge.hide()
        self._status.addPermanentWidget(self._badge)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 0)
        layout.addLayout(header)
        layout.addWidget(self._scroll, 1)
        layout.addWidget(self._status)
        self.resize(420, 480)
        # Kept across runs like the player's: the size that fits the object being
        # read is the user's to find, and finding it again every time the window
        # opens is the tax celpix.ui.window_layout exists to stop.
        self._layout_memory = WindowLayout(self, "layout/subsprite-window")
        # A remembered position counts as already placed (the tool windows'
        # shared rule, :mod:`celpix.ui.decompress_overlay`).
        self._positioned = self._layout_memory.restore()

        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.setInterval(REFRESH_DEBOUNCE_MS)
        self._pending.timeout.connect(self.refresh_requested)

        # The pan gesture's space key is taken off an application filter rather
        # than a key event of this window, so it answers wherever focus sits in
        # here — see eventFilter.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def columns(self) -> int:
        """How many squares across to compose the next sheet."""
        return self._columns.value()

    def by_frame(self) -> bool:
        """Whether the next sheet is the file's listing or its inventory."""
        return self._frames.isChecked()

    def records(self) -> list[Record]:
        """The records the sheet on show holds, one per square.

        Read by the window side to place the ring: under the inventory reading a
        square stands for several records, so which square a pick belongs in is a
        question about the *art*, and only the composing half can answer it
        (:mod:`celpix.ui.main_window.subsprites`).
        """
        return list(self._panel.records())

    def _on_frames_toggled(self, on: bool) -> None:
        """Recompose under the other reading, and settle the caption with it.

        Not debounced, unlike a refresh riding on the entry: this is a control
        the user just moved, and a sheet that re-laid itself a tenth of a second
        later would read as the click not having landed.
        """
        # Greyed rather than left live and inert: with Frames off a square is
        # several records and has no one frame to caption, so the box has nothing
        # to turn on — and a tick that does nothing is worse than one that is
        # visibly not this mode's.
        self._numbers.setEnabled(on)
        self._apply_captions()
        self.refresh_requested.emit()

    def _apply_captions(self) -> None:
        """Captions are on only where both switches allow them (see above)."""
        self._panel.set_captions(self._frames.isChecked() and self._numbers.isChecked())

    # -- presenting ----------------------------------------------------------
    def show_sheet(
        self,
        sheet: QImage,
        records: list[Record],
        boxes: list[Box],
        cell_px: tuple[int, int],
        title: str,
        *,
        marked: Record | None = None,
        status: str = "",
        badge: Badge | None = None,
    ) -> None:
        """Present an already-composed ``sheet`` of ``records``, showing the window.

        ``boxes`` says where each record's art landed in the sheet, which is what
        the ring goes round — the square is the largest piece of the object and
        not the record (:mod:`celpix.ui.subsprite_panel`).

        Called again on every refresh of the entry underneath, so nothing here
        may reset what the user set: the layout controls are read, never written,
        and the scroll position is the scroll area's own.
        """
        self.setWindowTitle(title)
        self._panel.set_sheet(sheet, records, boxes, cell_px, self.columns())
        self._panel.set_marked(marked)
        self._status.showMessage(status)
        apply_badge(self._badge, badge)
        if not self.isVisible():
            if not self._positioned and self.parentWidget() is not None:
                anchor = self.parentWidget().frameGeometry().topRight()
                self.move(anchor + QPoint(12, 0))
                self._positioned = True
            self.show()
            self._panel.setFocus()

    def set_marked(self, record: Record | None) -> None:
        """Ring the record the canvas just picked. Cheap enough to call per press
        — it moves a ring, where :meth:`show_sheet` recomposes the picture."""
        self._panel.set_marked(record)

    def set_status(self, status: str, badge: Badge | None = None) -> None:
        self._status.showMessage(status)
        apply_badge(self._badge, badge)

    def request_refresh(self) -> None:
        """Ask for the sheet to be recomposed shortly, coalescing a burst into one."""
        if self.isVisible():
            self._pending.start()

    def hide_overlay(self) -> None:
        """Hide — the entry on screen is not a sprite map, or was closed."""
        self._pending.stop()
        if self.isVisible():
            self.hide()

    def closeEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Closed from its own frame: drop the pan mode with it.

        A space release landing anywhere else is a release this window never
        sees, which would leave the panel holding an open hand and eating the
        next press when it reopens.
        """
        self._pending.stop()
        self._panel.set_pan_mode(False)
        super().closeEvent(event)

    # -- zoom and pan --------------------------------------------------------
    def _on_wheel_zoom(self, steps: int, pos) -> None:  # noqa: ANN001 — QPointF
        """Ctrl+wheel over the sheet, anchored on the pixel under the cursor."""
        spin = self._zoom
        new = min(max(spin.value() + steps, spin.minimum()), spin.maximum())
        zoom_anchored(self._scroll, spin, new, pos)

    def _pan(self, dx: int, dy: int) -> None:
        """Shift the scroll view by a space-drag delta (device pixels)."""
        pan_scroll_area(self._scroll, dx, dy)

    # -- the sheet's own width -----------------------------------------------
    #: Shift+arrow, the main window's Cols keys, in this window's terms.
    _COLUMN_KEYS = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1}

    def _columns_key(self, event) -> bool:  # noqa: ANN001 — QKeyEvent
        """Shift+Left/Right lay the sheet narrower or wider; True when consumed.

        The same keys the main window binds application-wide for the *view's*
        Cols (:meth:`~celpix.ui.main_window.navigation.NavigationMixin.
        _adjust_columns`), aimed at this window's width while this is the window
        being typed into. The two never fire together — that filter is gated on
        its own window being the active one — so this is the key finding the
        sheet the user is actually looking at rather than a second binding of it.

        Yields to a focused spin box, where Shift+arrow selects the digits of the
        number being typed. That is why the sheet takes the focus when the window
        opens: the keys answer over the picture, not over the header.
        """
        if event.modifiers() != Qt.KeyboardModifier.ShiftModifier:
            return False
        delta = self._COLUMN_KEYS.get(event.key())
        if delta is None or isinstance(QApplication.focusWidget(), QAbstractSpinBox):
            return False
        self._columns.setValue(self._columns.value() + delta)
        return True

    # -- space arms the pan --------------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001 — Qt override
        """Claim the space bar for the pan wherever focus sits in this window.

        Filtered on the application rather than handled in ``keyPressEvent``,
        because a key press goes to the focused widget alone: with focus on the
        Zoom spin — where magnifying the sheet leaves it, and having magnified it
        is the usual reason to want to pan — the press reached a widget that does
        nothing with it. The animation player's rule, and the main window's
        (:meth:`~celpix.ui.main_window.navigation.NavigationMixin.
        _handle_space_pan`).

        Any widget of *this* window and nothing outside it: only one window can
        be the one being typed into.
        """
        et = event.type()
        if et == QEvent.Type.WindowDeactivate and obj is self:
            # A hold that outlives the window's activation: the release lands in
            # whatever was raised over it and is never seen here.
            self._panel.set_pan_mode(False)
        elif (
            et in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
            and event.key() == Qt.Key.Key_Space
            and isinstance(obj, QWidget)
            and obj.window() is self
        ):
            # Auto-repeat is swallowed rather than acted on: holding space fires
            # press after press, and each would re-arm a mode already on.
            if not event.isAutoRepeat():
                self._panel.set_pan_mode(et == QEvent.Type.KeyPress)
            return True
        elif (
            et == QEvent.Type.KeyPress
            and isinstance(obj, QWidget)
            and obj.window() is self
            and self._columns_key(event)
        ):
            return True
        return super().eventFilter(obj, event)
