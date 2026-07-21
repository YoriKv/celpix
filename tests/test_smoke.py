"""Foundation smoke tests: the package imports and the main window constructs."""

from __future__ import annotations

import celpix
from celpix.ui.main_window import MainWindow


def test_version_present() -> None:
    assert celpix.__version__


def test_main_window_constructs(qtbot) -> None:
    # qtbot (pytest-qt) provides a QApplication and cleans up the widget.
    window = MainWindow()
    qtbot.addWidget(window)
    assert window.windowTitle() == "Celpix"
