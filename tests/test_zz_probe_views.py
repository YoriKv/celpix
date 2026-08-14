"""Throwaway probe: what a *small* repaint costs in each tile view."""

from __future__ import annotations

import cProfile
import pstats
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QImage, QRegion

from celpix.plugins.discovery import load_user_plugins, project_plugin_dir
from celpix.plugins.registry import default_registry
from celpix.ui.main_window import window as window_module

ROOT = Path(__file__).resolve().parents[1] / "sample-projects"
BAND = QRect(0, 0, 300, 200)  # what a scroll step exposes


def _open(qtbot, project: Path):
    def reload_plugins(project_path):
        registry = default_registry()
        folder = project_plugin_dir(project_path)
        issues = load_user_plugins(
            registry, [folder], project_dir=folder, confirm=lambda *a, **k: True
        )
        return registry, issues

    win = window_module.MainWindow()
    qtbot.addWidget(win)
    win._reload_plugins = reload_plugins
    win.resize(1400, 900)
    win.show()
    win._load_project(str(project))
    return win


def _band_paint(widget, times: int = 10) -> float:
    """Milliseconds for one repaint of a 300x200 band — a scroll step."""
    image = QImage(BAND.size(), QImage.Format.Format_ARGB32)
    t0 = time.perf_counter()
    for _ in range(times):
        widget.render(image, QPoint(), QRegion(BAND))
    return (time.perf_counter() - t0) * 1000 / times


def _report(label: str, widget) -> None:
    print(f"  {label}: band repaint {_band_paint(widget):.1f} ms")


def test_probe_views(qtbot) -> None:
    project = ROOT / "melroon/melroon.celpix"
    if not project.is_file():
        pytest.skip("no melroon project")
    win = _open(qtbot, project)

    font = max(win._workspace.entries, key=lambda e: len(e.font_chars))
    win._activate_entry(font)
    sheet = win._font_alphabet._sheet
    print(f"\nfont sheet {font.name}: {len(sheet._ids)} tiles")
    _report("alphabet sheet", sheet)
    _report("canvas", win._canvas)

    prof = cProfile.Profile()
    prof.enable()
    _band_paint(sheet, 10)
    prof.disable()
    pstats.Stats(prof).sort_stats("tottime").print_stats(8)

    # The biggest tilemap in the project, with the id overlay on.
    maps = [e for e in win._workspace.entries if e.content_kind.value == "tilemap"]
    biggest = max(maps, key=lambda e: e.slice_length or 0, default=None)
    if biggest is not None:
        win._activate_entry(biggest)
        doc = win._doc
        print(
            f"map {biggest.name}: {len(doc.cells) if doc.cells else 0} cells,"
            f" dock tiles {len(win._tile_source_panel._ids)}"
        )
        win._show_tile_ids_action.setChecked(True)
        win._refresh_view()
        _report("canvas (ids on)", win._canvas)
        _report("tile source dock", win._tile_source_panel)
        prof = cProfile.Profile()
        prof.enable()
        _band_paint(win._canvas, 10)
        prof.disable()
        pstats.Stats(prof).sort_stats("tottime").print_stats(10)
