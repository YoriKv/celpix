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
surfaces: the sizes on the left, and on the right a
:class:`~celpix.ui.widgets.Badge` for the state the picture itself can't show —
that what is on screen is only as much as the current view window fed the
decompressor. The picture looks equally plausible either way, which is exactly
why it needs saying in words. The badge is shared with the animation player,
which has the same problem (:mod:`celpix.ui.animation_overlay`).
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QLabel,
    QScrollArea,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from celpix.core.document import GridMode, ViewOptions
from celpix.ui.canvas import Canvas, GridStyle
from celpix.ui.widgets import Badge, apply_badge
from celpix.ui.window_layout import WindowLayout

__all__ = ["Badge", "DecompressOverlay"]


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
        self._layout_memory = WindowLayout(self, "layout/decompress-preview")
        # A remembered position counts as already placed: the beside-the-main-
        # window move below is for a window nobody has put anywhere yet.
        self._positioned = self._layout_memory.restore()

    def show_result(
        self,
        image: QImage,
        tile_size: tuple[int, int],
        view: ViewOptions,
        grid: tuple[bool, GridMode, bool],
        title: str,
        status: str,
        badge: Badge | None = None,
    ) -> None:
        """Present a freshly rendered decompression (showing the window if hidden).

        ``view`` is the main view's options and ``grid`` the project's grid
        settings, since the preview is drawn through the same zoom, arrangement
        and lattice — the point of it is to look like the picture would if the
        bytes were already decompressed. ``status`` is the sizes line; ``badge``
        annotates it, or None when the decode has nothing to add.
        """
        self.setWindowTitle(title)
        self._status.showMessage(status)
        apply_badge(self._badge, badge)
        tw, th = tile_size
        self._canvas.set_tile_size(tw, th)
        self._canvas.set_zoom(view.zoom)
        self._canvas.set_arrangement(
            view.block_columns, view.block_rows, view.block_order
        )
        self._canvas.set_grid(*grid)
        self._canvas.set_image(image)
        if not self.isVisible():
            if not self._positioned and self.parentWidget() is not None:
                anchor = self.parentWidget().frameGeometry().topRight()
                self.move(anchor + QPoint(12, 0))
                self._positioned = True
            self.show()

    def set_grid_style(self, style: GridStyle) -> None:
        """Follow the app-wide grid style, which the main window owns."""
        self._canvas.set_grid_style(style)

    def hide_overlay(self) -> None:
        """Hide (compression off, or the current window doesn't decompress)."""
        if self.isVisible():
            self.hide()
