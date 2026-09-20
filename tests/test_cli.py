"""The command line: what it refuses, and what it opens.

The refusals carry the risk here. Each one is a rule about which combinations of
arguments mean anything, and getting one wrong is silent — the launch succeeds
and opens the wrong reading, or nothing.
"""

from __future__ import annotations

import pytest

from celpix.app import _build_parser, _open_arguments, _parse_args
from celpix.core.capabilities import ContentKind
from celpix.project.projectfile import PROJECT_EXTENSION
from celpix.ui.main_window import MainWindow
from uihelpers import _make_snes_file


def _parse(*argv: str):
    return _parse_args(_build_parser(), list(argv))


def _refused(*argv: str) -> None:
    """Assert the launch is refused — argparse exits 2 and says why on stderr."""
    with pytest.raises(SystemExit) as caught:
        _parse(*argv)
    assert caught.value.code == 2


def test_a_project_opens_with_its_entries(tmp_path, qtbot):
    """The whole point of a path on the command line: `celpix thing.celpix`."""
    px = _make_snes_file(tmp_path)
    saver = MainWindow()
    qtbot.addWidget(saver)
    saver._load_pixel(str(px))
    project = tmp_path / f"saved{PROJECT_EXTENSION}"
    saver._save_project_to(str(project))

    window = MainWindow()
    qtbot.addWidget(window)
    _open_arguments(window, _parse(str(project)))
    assert [e.name for e in window._workspace.entries] == [px.name]
    assert window._project_path == str(project)


def test_type_forces_the_reading(tmp_path, qtbot):
    """--type overrides detection, which never guesses a map for a raw file."""
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    _open_arguments(window, _parse("--type", "tilemap", str(px)))
    (entry,) = window._workspace.entries
    assert entry.content_kind is ContentKind.TILEMAP


def test_palette_suffix_lands_in_palettes(tmp_path, qtbot):
    """A .pal opens as palette data without being asked, as it does on a drop."""
    pal = tmp_path / "colors.pal"
    pal.write_bytes(bytes(range(48)))
    window = MainWindow()
    qtbot.addWidget(window)
    _open_arguments(window, _parse(str(pal)))
    assert window._workspace.find_palette(str(pal)) is not None


def test_several_files_open_as_one_undo_step(tmp_path, qtbot):
    """One launch is one gesture, so its files undo together."""
    first = _make_snes_file(tmp_path)
    second = tmp_path / "other.4bpp.sfc"
    second.write_bytes(first.read_bytes())
    window = MainWindow()
    qtbot.addWidget(window)
    _open_arguments(window, _parse(str(first), str(second)))
    assert len(window._workspace.entries) == 2
    window._undo_stack.undo()
    assert window._workspace.entries == []


def test_a_project_opens_on_its_own(tmp_path):
    """Loading a project replaces the workspace, so a file named beside one
    would be discarded by the replace or land in it as an unasked-for edit."""
    project = tmp_path / f"p{PROJECT_EXTENSION}"
    project.write_text("{}")
    data = _make_snes_file(tmp_path)
    _refused(str(project), str(data))
    _refused(str(project), str(tmp_path / f"q{PROJECT_EXTENSION}"))
    _refused("--type", "pixels", str(project))


def test_a_launch_that_cannot_work_is_refused(tmp_path):
    """Before the window, not in a dialog in front of it."""
    _refused(str(tmp_path / "gone.bin"))
    # A PNG is imported into an open graphic; a launch has nothing open yet.
    png = tmp_path / "art.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    _refused(str(png))
