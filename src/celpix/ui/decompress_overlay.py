"""The decompression preview — a floating window beside the raw view.

(:class:`DecompressOverlay` is a tool window beside the view, not drawn over it;
the class name and the ``layout/decompress-preview`` settings key are what the
rest of the code and a user's stored layout know it by.)

When a compression scheme is selected, the main canvas keeps showing the file's
*raw* bytes; this tool window answers "what would decompressing from the current
offset look like?". The main window feeds it a ready-rendered image (the product
of a parallel run of the pixel-interpret and palette paths over the
decompressed window bytes) or tells it to hide — it owns no model and makes no
decisions beyond presentation.

It is a floating tool window (:class:`~celpix.ui.tool_window.ToolWindow`): it
floats above the main window, moves with the session, and never takes a taskbar
slot. The first show places it beside the main window; after that its position
is the user's.

Below the preview sits the tool windows' **status bar**, the one place the
decode's own state surfaces: the sizes on the left, and on the right a
:class:`~celpix.ui.widgets.Badge` for the state the picture itself can't show —
that what is on screen is only as much as the current view window fed the
decompressor. The picture looks equally plausible either way, which is exactly
why it needs saying in words.
"""

from __future__ import annotations

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QScrollArea, QWidget

from celpix.core.document import GridMode, ViewOptions
from celpix.ui.canvas import Canvas, GridStyle
from celpix.ui.panzoom import PanZoomSurface
from celpix.ui.tool_window import ToolWindow
from celpix.ui.widgets import Badge

# ``Badge`` is re-exported for the callers that build this window's badges
# without otherwise needing the widgets module.
__all__ = ["Badge", "DecompressOverlay"]


class DecompressOverlay(ToolWindow):
    """Presentation-only floating preview of a decompressed view window."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            parent, "Decompressed", "layout/decompress-preview", (420, 420)
        )
        self._canvas = Canvas()
        scroll = QScrollArea()
        scroll.setWidget(self._canvas)
        scroll.setWidgetResizable(False)
        self.content.addWidget(scroll, 1)

    def _pixel_surface(self) -> PanZoomSurface:
        return self._canvas

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
        self.set_status(status, badge)
        tw, th = tile_size
        self._canvas.set_tile_size(tw, th)
        self._canvas.set_zoom(view.zoom)
        self._canvas.set_arrangement(
            view.block_columns, view.block_rows, view.block_order
        )
        self._canvas.set_grid(*grid)
        self._canvas.set_image(image)
        self.present()

    def set_grid_style(self, style: GridStyle) -> None:
        """Follow the app-wide grid style, which the main window owns."""
        self._canvas.set_grid_style(style)
