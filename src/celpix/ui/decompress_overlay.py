"""The decompression preview overlay — a floating window over the raw view.

When a compression scheme is selected, the main canvas keeps showing the file's
*raw* bytes; this tool window answers "what would decompressing from the current
offset look like?". The main window feeds it a ready-rendered image (the product
of a parallel run of the pixel-interpret and palette paths over the
decompressed window bytes) or tells it to hide — it owns no model and makes no
decisions beyond presentation.

It is a `Qt.Tool` window: it floats above the main window, moves with the
session, and never takes a taskbar slot. The user can drag it wherever they
like; the first show places it beside the main window, after that its position
is left alone.

Below the preview sits a **status bar**, the one place the decode's own state
surfaces: the sizes on the left, and on the right a :class:`Badge` for the state
the picture itself can't show — that what is on screen is only as much as the
current view window fed the decompressor. The picture looks equally plausible
either way, which is exactly why it needs saying in words.

A badge is either a **warning** (amber) or plain information (standard text
colour), which is the difference between a problem and a fact: a scheme that has
an end marker and didn't reach one *was* cut short, while a stream-based scheme
was only ever going to decode as far as it was fed.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QLabel,
    QScrollArea,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from celpix.ui.canvas import Canvas

# Amber for the warning level. The QToolTip rule is not decoration: Qt applies a
# bare `color:` to the widget's *tooltip* as well, which would render the whole
# explanation amber — pinning it to palette(text) keeps the tooltip readable in
# either theme.
_WARNING_STYLE = "QLabel { color: #c08a30; } QToolTip { color: palette(text); }"


@dataclass(frozen=True)
class Badge:
    """The status bar's right-hand annotation.

    ``text`` is the few words shown; ``detail`` the tooltip's fuller
    explanation (hard-wrapped by the caller — see the tooltip rule in
    ``docs/py-qt-reference/pyside6-pitfalls.md``); ``warning`` picks amber over
    the standard text colour.
    """

    text: str
    detail: str
    warning: bool = False


class DecompressOverlay(QWidget):
    """Presentation-only floating preview of a decompressed view window."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle("Decompressed view")
        self._positioned = False

        self._canvas = Canvas()
        scroll = QScrollArea()
        scroll.setWidget(self._canvas)
        scroll.setWidgetResizable(False)

        self._status = QStatusBar()
        self._status.setSizeGripEnabled(False)
        # The badge rides in the permanent (right-hand) slot so its text never
        # pushes the sizes out of view; the tooltip carries the explanation.
        self._badge = QLabel()
        self._badge.hide()
        self._status.addPermanentWidget(self._badge)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 0)
        layout.addWidget(scroll, 1)
        layout.addWidget(self._status)
        self.resize(420, 420)

    def show_result(
        self,
        image: QImage,
        tile_size: tuple[int, int],
        zoom: int,
        show_grid: bool,
        title: str,
        status: str,
        badge: Badge | None = None,
    ) -> None:
        """Present a freshly rendered decompression (showing the window if hidden).

        ``status`` is the sizes line; ``badge`` annotates it, or None when the
        decode has nothing to add.
        """
        self.setWindowTitle(title)
        self._status.showMessage(status)
        self._badge.setText(badge.text if badge else "")
        self._badge.setToolTip(badge.detail if badge else "")
        self._badge.setStyleSheet(_WARNING_STYLE if badge and badge.warning else "")
        self._badge.setVisible(badge is not None)
        tw, th = tile_size
        self._canvas.set_tile_size(tw, th)
        self._canvas.set_zoom(zoom)
        self._canvas.set_grid(show_grid)
        self._canvas.set_image(image)
        if not self.isVisible():
            if not self._positioned and self.parentWidget() is not None:
                anchor = self.parentWidget().frameGeometry().topRight()
                self.move(anchor + QPoint(12, 0))
                self._positioned = True
            self.show()

    def hide_overlay(self) -> None:
        """Hide (compression off, or the current window doesn't decompress)."""
        if self.isVisible():
            self.hide()
