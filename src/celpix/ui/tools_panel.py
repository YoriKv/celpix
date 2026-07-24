"""The drawing-tools panel: an exclusive grid of tool buttons.

A dumb view, like :class:`~celpix.ui.palette_panel.PalettePanel`: it shows one
checkable button per :data:`~celpix.ui.tools.TOOL_SPECS` entry and reports the
picked tool via :attr:`tool_selected`. It owns no editing logic and no mode
state — the pixel-edit controller decides what a tool *does* and drives
:meth:`set_tool` back when the tool changes by a number-key shortcut, so the
buttons always mirror the active tool.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QGridLayout,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from celpix.ui.tools import TOOL_SPECS, Tool

# Two columns keeps the panel narrow enough to sit under the palette dock without
# widening it, while the nine tools stay a short scan (five rows).
_COLUMNS = 2


class ToolsPanel(QWidget):
    tool_selected = Signal(object)  # the picked Tool

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._buttons: dict[Tool, QToolButton] = {}
        # Exclusive group: exactly one tool is active, and clicking another
        # unchecks the last without any bookkeeping here.
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        grid = QGridLayout(self)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setSpacing(2)
        for i, spec in enumerate(TOOL_SPECS):
            button = QToolButton()
            button.setText(spec.label)
            button.setToolTip(f"{spec.tooltip}  ({spec.key})")
            button.setCheckable(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            # Bind the spec's tool per-iteration so every button reports its own.
            button.clicked.connect(lambda _=False, tool=spec.tool: self._pick(tool))
            self._group.addButton(button)
            grid.addWidget(button, i // _COLUMNS, i % _COLUMNS)
            self._buttons[spec.tool] = button
        # Start on the pen (the first, most-used tool) without announcing it —
        # the controller seeds its own tool state from the same default.
        self._buttons[Tool.PENCIL].setChecked(True)

    def _pick(self, tool: Tool) -> None:
        self.tool_selected.emit(tool)

    def set_tool(self, tool: Tool) -> None:
        """Check ``tool``'s button without emitting — reflect an external change."""
        button = self._buttons.get(tool)
        if button is not None and not button.isChecked():
            button.setChecked(True)
