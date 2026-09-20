"""File ▸ New Compress & Reshape Plugin… — the dialog, and the file it leads to."""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import NO_COMPRESSION, NO_RESHAPE
from celpix.plugins.discovery import load_user_plugins, project_plugin_dir
from celpix.plugins.registry import default_registry
from celpix.ui.compress_reshape_dialog import (
    CompressReshapeDialog,
    CompressReshapeParams,
)
from celpix.ui.main_window import MainWindow
from uihelpers import _combo_ids


def _pick(dialog: CompressReshapeDialog, compression_id: str, reshape_id: str) -> None:
    dialog._compression.setCurrentIndex(dialog._compression.findData(compression_id))
    dialog._reshape.setCurrentIndex(dialog._reshape.findData(reshape_id))


def test_dialog_names_the_pair_and_refuses_a_taken_id(qtbot, tmp_path) -> None:
    dialog = CompressReshapeDialog(default_registry(), tmp_path / "plugins")
    qtbot.addWidget(dialog)
    # What the loader would refuse is not offered.
    assert NO_COMPRESSION not in _combo_ids(dialog._compression)
    assert NO_RESHAPE not in _combo_ids(dialog._reshape)

    _pick(dialog, "compression.rnc2", "reshape.running-sum")
    # A name that slugs onto a shipped id is caught here, not at the next load.
    dialog._name.setText("RNC2")
    dialog._validate_and_accept()
    assert dialog._params is None
    assert "already exists" in dialog._error.text()

    dialog._name.setText("MK2 screen maps")
    dialog._validate_and_accept()
    assert dialog._params == CompressReshapeParams(
        "compression.mk2-screen-maps",
        "MK2 screen maps",
        "compression.rnc2",
        "reshape.running-sum",
    )


def test_new_plugin_lands_in_the_project_and_in_the_picker(
    qtbot, tmp_path, monkeypatch
) -> None:
    project = tmp_path / "game.celpix"

    def reload(project_path=None):
        reg = default_registry()
        found = project_plugin_dir(project_path)
        dirs = [found] if found else []
        return reg, load_user_plugins(reg, dirs, project_dir=found)

    window = MainWindow(registry=reload()[0], reload_plugins=reload)
    qtbot.addWidget(window)
    window._project_path = str(project)
    monkeypatch.setattr(
        CompressReshapeDialog,
        "ask",
        staticmethod(
            lambda *a: CompressReshapeParams(
                "compression.pair", "Pair", "compression.rnc2", "reshape.running-sum"
            )
        ),
    )
    # The project has no plugins/ folder yet: making the first plugin makes it.
    window._new_compress_reshape_plugin()

    assert (tmp_path / "plugins" / "compression" / "pair.toml").is_file()
    pair = window._registry.plugin(Stage.COMPRESSION, "compression.pair")
    assert pair.info.category == "Project plugins"
    assert "compression.pair" in _combo_ids(window._compression)
