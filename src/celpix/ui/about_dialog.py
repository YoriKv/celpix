"""Help ▸ About: what this is, which version, who wrote it, and the license."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from celpix import APP_TAGLINE, __version__
from celpix.ui.icon_font import app_pixmap
from celpix.ui.widgets import dialog_buttons

__all__ = ["AUTHOR", "HOMEPAGE", "AboutDialog"]

AUTHOR = "Epi"
HOMEPAGE = "https://github.com/YoriKv/celpix"


class AboutDialog(QDialog):
    """Help ▸ About: what this is, which version, who wrote it, and the license."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About celPix")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        icon = QLabel()
        icon.setPixmap(
            app_pixmap().scaled(
                64,
                64,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)

        # One rich-text label rather than a stack of them: it keeps the links
        # clickable and the whole thing selectable for a bug report.
        text = QLabel(
            f"<h2 style='margin-bottom:2px'>celPix {__version__}</h2>"
            f"<p style='margin-top:0'>{APP_TAGLINE}</p>"
            f"<p>By <b>{AUTHOR}</b><br>"
            f"<a href='{HOMEPAGE}'>{HOMEPAGE}</a></p>"
            "<p>Released under the MIT license. Built on Python and Qt via"
            " PySide6, which is licensed under the LGPLv3.</p>"
            # Apache 2.0 asks that the notice travel with the work, so it is in
            # the app and not only in the license file shipped beside the font.
            "<p>Icons from <a href='https://fonts.google.com/icons'>Material"
            " Symbols</a>, licensed Apache 2.0.</p>"
        )
        text.setWordWrap(True)
        text.setOpenExternalLinks(True)
        text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )

        top = QHBoxLayout()
        top.setSpacing(14)
        top.addWidget(icon)
        top.addWidget(text, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        dialog_buttons(self, layout, close_only=True)
        self.setMinimumWidth(420)
