"""Throwaway probe: the subsprite view on a real object, and overlay equality."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QImage, QRegion

from celpix.plugins.discovery import load_user_plugins, project_plugin_dir
from celpix.plugins.registry import default_registry
from celpix.ui.main_window import window as window_module

ROOT = Path(__file__).resolve().parents[1] / "sample-projects"
BAND = QRect(0, 0, 300, 200)


def _open(qtbot, project: Path):
    def reload_plugins(project_path):
        registry = default_registry()
        folder = project_plugin_dir(project_path)
        dirs = [folder] if folder else []
        issues = load_user_plugins(
            registry, dirs, project_dir=folder, confirm=lambda *a, **k: True
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
    image = QImage(BAND.size(), QImage.Format.Format_ARGB32)
    t0 = time.perf_counter()
    for _ in range(times):
        widget.render(image, QPoint(), QRegion(BAND))
    return (time.perf_counter() - t0) * 1000 / times


def test_probe_subsprites(qtbot) -> None:
    for name in ("alttp/alttp.celpix", "leak-ver2/ver2.celpix", "melroon/melroon.celpix"):
        project = ROOT / name
        if not project.is_file():
            continue
        win = _open(qtbot, project)
        found = False
        candidates = [
            e
            for e in win._workspace.entries
            if any(w in (e.tilemap_preset_id or "").lower() for w in ("obj", "sprite"))
        ]
        print(f"{name}: {len(candidates)} sprite-ish entries")
        for entry in candidates:
            win._activate_entry(entry)
            doc = win._doc
            if doc is None or not doc.is_sprite:
                continue
            win._show_subsprites()
            panel = win._subsprites._panel
            print(
                f"\n{name} :: {entry.name}: {len(panel._records)} records,"
                f" band repaint {_band_paint(panel):.1f} ms"
            )
            found = True
            break
        if not found:
            print(f"\n{name}: no sprite object found")
        win.close()
