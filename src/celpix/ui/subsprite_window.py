"""The Subsprites window — every piece a sprite map is built from, as a sheet.

A sprite map's canvas shows its frames: the object assembled, which is a picture
of what the file *draws*. A frame is a heap of subsprites at signed pixel offsets
and the front ones cover the back ones, so it is not a picture of what the file
*holds*. This is that — one square per record, in frame order, repeats included
(``docs/design/sprite-map.md`` §5).

It is a floating tool window (:class:`~celpix.ui.tool_window.ToolWindow`), like
the animation player: floats above the main window, takes no taskbar slot,
placed beside it on the first show and left where the user drags it after, with
its layout kept across runs. The two are neighbours in the View menu
and are the two second readings a sprite map has — but where the player is
offered only on an object with a sequence to play, **every** sprite map has
subsprites, so this one is offered on all of them.

**Its zoom and its width are its own**, deliberately not the main view's. Cols
here lays out the sheet of records; Cols on the binding bar lays out the strip of
frames, and neither is a reading of the other. The panel reports Ctrl+wheel and
space-drag and owns no state, while this window holds the level in its Zoom spin
and the scrolling in its scroll area — the split the tile source dock makes with
its panel, here between this window and :mod:`celpix.ui.subsprite_panel`.

**Presentation only.** It is handed a composed sheet, the records it covers and
the one the canvas picked; it never reads the model
(:mod:`celpix.ui.main_window.subsprites`).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QSpinBox,
    QWidget,
)

from celpix.ui.panzoom import (
    PanZoomSurface,
    SpacePanFilter,
    mount_surface,
    zoom_spin,
)
from celpix.ui.subsprite_panel import Box, Record, SubspritePanel
from celpix.ui.tool_window import ToolWindow
from celpix.ui.widgets import (
    Badge,
    add_labelled,
)

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


class SubspriteWindow(ToolWindow):
    """Floating sheet of one sprite map's subsprite records."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, "Subsprites", "layout/subsprite-window", (420, 480))

        self._panel = SubspritePanel()
        self._columns = QSpinBox()
        self._columns.setRange(*COLUMN_RANGE)
        self._columns.setValue(DEFAULT_COLUMNS)
        self._columns.setKeyboardTracking(False)
        self._columns.valueChanged.connect(lambda _v: self.refresh_requested.emit())

        self._zoom = zoom_spin(
            ZOOM_RANGE,
            DEFAULT_ZOOM,
            self._panel.set_zoom,
            "Sheet magnification (Ctrl+Scroll over the sheet)\n"
            "Independent of the main view. Space+drag pans",
        )
        self._panel.set_zoom(DEFAULT_ZOOM)
        # The backing around the sheet zooms with it: a short object laid 8 across
        # leaves most of the window empty, and that is where the pointer sits.
        self._scroll = mount_surface(self._panel, spin=self._zoom)

        # Which of the two readings the sheet is (`pipeline.subsprite_sheet`).
        # On, the file's own listing — every record, in frame order. Off, the
        # inventory: one square per distinct piece, which is what says how much
        # art an object actually holds where the listing says the same few things
        # over and over.
        self._frames = QCheckBox("Frames")
        self._frames.setChecked(True)
        self._frames.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._frames.setToolTip(
            "Lay the sheet out by frame: one square per record\n"
            "Off shows each distinct subsprite once"
        )
        self._frames.toggled.connect(self._on_frames_toggled)

        self._numbers = QCheckBox("Numbers")
        self._numbers.setChecked(True)
        # Takes no focus, the animation player's rule for its transport buttons:
        # space is this window's pan gesture and is claimed window-wide
        # (:class:`~celpix.ui.widgets.SpacePanFilter`), so a focused checkbox
        # could not toggle itself with it anyway — it would just wear a focus
        # ring for nothing.
        self._numbers.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._numbers.setToolTip(
            "Label each square with frame:subsprite\n"
            "Needs Frames on and a zoom the text fits at"
        )
        self._numbers.toggled.connect(lambda _on: self._apply_captions())

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        add_labelled(
            header,
            "Cols",
            self._columns,
            "Subsprites per row of the sheet (Shift+Left/Right)\n"
            "Independent of the canvas",
        )
        add_labelled(header, "Zoom", self._zoom, self._zoom.toolTip())
        header.addWidget(self._frames)
        header.addWidget(self._numbers)
        header.addStretch(1)
        self.content.addLayout(header)
        self.content.addWidget(self._scroll, 1)

        # Space pans wherever focus sits in here; Shift+Left/Right lays the
        # sheet narrower or wider (:meth:`_columns_key`).
        self._space_pan = SpacePanFilter(self, self._panel, extra_key=self._columns_key)

    def _pixel_surface(self) -> PanZoomSurface:
        return self._panel

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
        self.set_status(status, badge)
        # The sheet takes the focus, so the column keys answer over the picture
        # rather than over the header's spins (:meth:`_columns_key`).
        self.present(focus=self._panel)

    def set_marked(self, record: Record | None) -> None:
        """Ring the record the canvas just picked. Cheap enough to call per press
        — it moves a ring, where :meth:`show_sheet` recomposes the picture."""
        self._panel.set_marked(record)

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
