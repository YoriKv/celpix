"""The floating tool windows' shared shell.

The decompression preview, the animation player, the subsprite sheet, the text
window and the font alphabet are all the same kind of window around different
content: a ``Qt.Tool`` that floats above the main window and takes no taskbar
slot, a status bar with a :class:`~celpix.ui.widgets.Badge` in its permanent
slot for what the picture cannot show, a size and position remembered across
runs (:mod:`celpix.ui.window_layout`), and a first show that places it beside
the main window. This is that shell once, so a sixth window inherits the rules
rather than copying them.

**A remembered position counts as already placed.** The beside-the-main-window
move is for a window nobody has put anywhere yet; running it over a restored
position would throw the user's placement away every launch.

**Refreshes are debounced** for the windows that recompose on the entry
underneath (:meth:`ToolWindow.request_refresh`): the main window refreshes on
things that arrive per pixel of a stroke, and one recompose per burst is the
difference between a window that is open and one that is in the way.

``hide_overlay`` is celPix putting a window away (the entry stopped being one
it describes); closing it from its own frame is the user saying they do not
want it, which is what :attr:`ToolWindow.dismissed` reports.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import QLabel, QStatusBar, QVBoxLayout, QWidget

from celpix.ui.panzoom import PanZoomSurface
from celpix.ui.widgets import (
    Badge,
    apply_badge,
)
from celpix.ui.window_layout import WindowLayout

__all__ = ["REFRESH_DEBOUNCE_MS", "ToolWindow"]

# How long a burst of view refreshes coalesces into one recompose. Long enough to
# swallow a stroke's worth, short enough that an edit appears to land in the
# window at the same time it lands on the canvas.
REFRESH_DEBOUNCE_MS = 120


class ToolWindow(QWidget):
    """A floating window's shell: status bar and badge, placement, memory.

    A subclass builds its content into :attr:`content` (a vertical layout above
    the status bar), shows itself through :meth:`present`, and names the
    magnifying surface it holds, if any, through :meth:`_pixel_surface` — which
    is what :meth:`set_pixel_aspect` forwards to and what closing disarms.

    ``offset`` is where the first show puts the window's top-left against the
    main window's top-right corner — or, with ``from_bottom``, against its
    bottom-right, less the window's own height, for a window that sits beside
    the lower half of the main one.
    """

    #: Ask for the content to be recomposed — the entry underneath changed.
    #: A signal rather than a direct call because the composing half lives on
    #: the main window; :meth:`request_refresh` debounces it.
    refresh_requested = Signal()
    #: The user shut the window from its own frame. Distinct from being hidden
    #: because the entry stopped being one this window describes, which is
    #: celPix's decision and says nothing about whether they want it back.
    dismissed = Signal()

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        layout_key: str,
        size: tuple[int, int],
        *,
        offset: QPoint | None = None,
        from_bottom: bool = False,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle(title)
        self._offset = offset if offset is not None else QPoint(12, 0)
        self._from_bottom = from_bottom

        self._status = QStatusBar()
        self._status.setSizeGripEnabled(False)
        # The badge rides in the permanent (right-hand) slot so its text never
        # pushes the message out of view; its tooltip carries the explanation.
        self._badge = QLabel()
        self._badge.hide()
        self._status.addPermanentWidget(self._badge)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 0)
        #: Where the subclass lays out its content, above the status bar.
        self.content = QVBoxLayout()
        self.content.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self.content, 1)
        outer.addWidget(self._status)

        self.resize(*size)
        # A size the user set is worth keeping: the one that fits what is being
        # read is theirs to find, and finding it again every time the window
        # opens is the tax this exists to stop. One key per window kind — a
        # window is remembered by what it is for, not by which instance it is.
        self._layout_memory = WindowLayout(self, layout_key)
        self._positioned = self._layout_memory.restore()

        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.setInterval(REFRESH_DEBOUNCE_MS)
        self._pending.timeout.connect(self.refresh_requested)

    # -- the shell -----------------------------------------------------------
    def present(self, focus: QWidget | None = None) -> None:
        """Show the window if it is hidden, placing it on its first show.

        ``focus`` takes the keyboard once shown — the surface rather than the
        first widget of the layout, where a window's keys are meant to land.
        """
        if self.isVisible():
            return
        parent = self.parentWidget()
        if not self._positioned and parent is not None:
            frame = parent.frameGeometry()
            anchor = (
                frame.bottomRight() - QPoint(0, self.height())
                if self._from_bottom
                else frame.topRight()
            )
            self.move(anchor + self._offset)
            self._positioned = True
        self.show()
        if focus is not None:
            focus.setFocus()

    def set_status(self, status: str, badge: Badge | None = None) -> None:
        """The status line and its badge — ``None`` takes the badge down."""
        self._status.showMessage(status)
        apply_badge(self._badge, badge)

    def request_refresh(self) -> None:
        """Ask for the content to be recomposed shortly, a burst into one."""
        if self.isVisible():
            self._pending.start()

    def hide_overlay(self) -> None:
        """Hide — the entry on screen is not one this window describes.

        The pan goes down with it, for the reason :meth:`closeEvent` gives.
        """
        self._pending.stop()
        self._disarm_pan()
        if self.isVisible():
            self.hide()

    def set_pixel_aspect(self, aspect) -> None:  # noqa: ANN001 — a PixelAspect
        """Draw at ``aspect`` — forwarded to the surface the window holds.

        One name on every holder of a pixel surface, so the main window applies
        the project's setting with a loop rather than by reaching through each of
        them (:meth:`~celpix.ui.main_window.view_menu.ViewMenuMixin.
        _sync_pixel_aspect`).
        """
        surface = self._pixel_surface()
        if surface is not None:
            surface.set_pixel_aspect(aspect)

    def _pixel_surface(self) -> PanZoomSurface | None:
        """The magnifying surface this window holds, or ``None`` for none."""
        return None

    def _disarm_pan(self) -> None:
        surface = self._pixel_surface()
        if surface is not None:
            surface.set_pan_mode(False)

    def closeEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Closed from its own frame: say so, and drop the pan mode with it.

        A space release landing anywhere else is a release this window never
        sees, which would leave the surface holding an open hand and eating the
        next press when it reopens.
        """
        self._pending.stop()
        self._disarm_pan()
        self.dismissed.emit()
        super().closeEvent(event)
