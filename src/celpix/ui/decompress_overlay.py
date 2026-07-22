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
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from celpix.ui.canvas import Canvas


class DecompressOverlay(QWidget):
    """Presentation-only floating preview of a decompressed view window."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle("Decompressed view")
        self._positioned = False

        self._note = QLabel()
        self._note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._canvas = Canvas()
        scroll = QScrollArea()
        scroll.setWidget(self._canvas)
        scroll.setWidgetResizable(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self._note)
        layout.addWidget(scroll, 1)
        self.resize(420, 420)

    def show_result(
        self,
        image: QImage,
        tile_size: tuple[int, int],
        zoom: int,
        show_grid: bool,
        title: str,
        note: str,
    ) -> None:
        """Present a freshly rendered decompression (showing the window if hidden)."""
        self.setWindowTitle(title)
        self._note.setText(note)
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
