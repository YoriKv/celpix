"""The Pixel Aspect popup: what shape one pixel is drawn at.

A short list, because the answer is a property of the machine a file was drawn
for and celPix reads a handful of machines between them
(:data:`~celpix.core.aspect.PRESETS`). What the popup is really for is making the
question *askable*: a 640x200 screen looks merely a bit squat at 1:1, which reads
as the art being a bit squat, so a user who does not know the setting exists has
no reason to go looking for it. Each row therefore says where its ratio is met
rather than only naming a number.

The choice is the **project's** and not this window's (``docs/design/pixel-
aspect.md``), which is why it lands here rather than beside the theme: it says
something about the data, and the same project opened on another machine wants
the same answer. A container that knows which hardware it is reading can seed it
(:data:`~celpix.core.context.KEY_PIXEL_ASPECT`), and this is where that seeding is
overruled.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from celpix.core.aspect import PRESETS, SQUARE, PixelAspect

__all__ = ["PixelAspectDialog"]


class PixelAspectDialog(QDialog):
    """Pick one of :data:`~celpix.core.aspect.PRESETS`, or keep the current one."""

    def __init__(self, current: PixelAspect, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("celPix - pixel aspect")
        layout = QVBoxLayout(self)
        intro = QLabel(
            "How wide and tall one image pixel is drawn. This is a display\n"
            "setting for the whole project - it changes nothing in the data,\n"
            "and nothing an export writes."
        )
        intro.setToolTip(
            "A machine's pixel is not always square: a 640x200 screen\n"
            "draws one twice as tall as it is wide, so its art is\n"
            "squashed at 1:1. The wider side is stretched rather than\n"
            "the narrower one shrunk, so nothing is ever lost."
        )
        layout.addWidget(intro)
        self._group = QButtonGroup(self)
        # Held beside the group because a QButtonGroup id is an int and the value
        # here is a pair; the index into PRESETS is the one thing both can carry.
        self._choices: list[PixelAspect] = []
        for at, (aspect, label, detail) in enumerate(PRESETS):
            button = QRadioButton(label)
            button.setToolTip(detail)
            self._group.addButton(button, at)
            self._choices.append(aspect)
            layout.addWidget(button)
            if aspect == current:
                button.setChecked(True)
        if self._group.checkedButton() is None:
            # A ratio no preset carries — an older or newer project, or a file
            # edited by hand. It still draws correctly, so the popup opens with
            # nothing selected rather than silently re-answering the question:
            # closing it must not quietly change what is on screen.
            self._unlisted = current
        else:
            self._unlisted = None
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def chosen(self) -> PixelAspect:
        """The picked ratio — the one the popup opened on if nothing was picked."""
        at = self._group.checkedId()
        if at < 0:
            return self._unlisted or SQUARE
        return self._choices[at]

    @staticmethod
    def ask(parent: QWidget | None, current: PixelAspect) -> PixelAspect | None:
        """Run the popup modally; the chosen ratio, or ``None`` if cancelled."""
        dialog = PixelAspectDialog(current, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.chosen()
