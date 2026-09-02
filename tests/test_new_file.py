"""File ▸ New File…: the blank payload's arithmetic, the container framing
probe, and the gesture that turns the answers into a file and an entry."""

from __future__ import annotations

from pathlib import Path

import pytest

from celpix.core.capabilities import ContentKind
from celpix.core.errors import PipelineError
from celpix.pipeline import pipeline
from celpix.plugins.base import RAW_CONTAINER
from celpix.plugins.registry import default_registry
from celpix.ui.main_window import MainWindow
from celpix.ui.new_file_dialog import NewFileDialog, NewFileParams

_4BPP = "preset.pixel.snes-4bpp"
_BGR555 = "preset.palette.bgr555"
_SNES_BG = "preset.tilemap.snes-bg"
# The 4bpp variant of the tile-bank family: 0x8000 bytes of tiles, which at 32
# bytes a tile is 16x64. The one size that container builds a bank around.
_CGX_4BPP_TILES = 0x8000 // 32


def test_blank_size_counts_units_through_the_codec() -> None:
    reg = default_registry()
    assert pipeline.blank_size(ContentKind.PIXELS, _4BPP, 256, reg) == 256 * 32
    assert pipeline.blank_size(ContentKind.TILEMAP, _SNES_BG, 1024, reg) == 1024 * 2
    assert pipeline.blank_size(ContentKind.PALETTE, _BGR555, 256, reg) == 512


def test_blank_size_rounds_a_packed_palette_up_to_a_whole_unit() -> None:
    """The handheld grayscale registers pack four entries into one byte, so
    three colors still cost the whole unit that holds them."""
    reg = default_registry()
    packed = "preset.palette.gb-bgp"
    if not reg.has_preset(packed):
        pytest.skip("no packed palette format registered")
    assert pipeline.blank_size(ContentKind.PALETTE, packed, 3, reg) == 1
    assert pipeline.blank_size(ContentKind.PALETTE, packed, 5, reg) == 2


def test_create_file_writes_the_payload_through_a_plain_container(tmp_path) -> None:
    reg = default_registry()
    path = tmp_path / "tiles.bin"
    size = pipeline.create_file(
        str(path),
        kind=ContentKind.PIXELS,
        container_id=RAW_CONTAINER,
        codec_id=_4BPP,
        units=64,
        reg=reg,
    )
    assert size == 64 * 32
    assert path.read_bytes() == bytes(64 * 32)


def test_create_file_replaces_rather_than_splicing_into_what_is_there(
    tmp_path,
) -> None:
    """A container's write normally preserves what it did not decode. Creating a
    file must not: the picker already asked about the overwrite, and splicing
    would leave the tail of a file the user said to replace."""
    reg = default_registry()
    path = tmp_path / "tiles.bin"
    path.write_bytes(b"\xee" * 4096)
    pipeline.create_file(
        str(path),
        kind=ContentKind.PIXELS,
        container_id=RAW_CONTAINER,
        codec_id=_4BPP,
        units=8,
        reg=reg,
    )
    assert path.read_bytes() == bytes(8 * 32)


def test_create_file_builds_a_container_that_can_frame_a_fresh_payload(
    tmp_path,
) -> None:
    """The tile-bank container writes its header, signature and row table around
    a payload of a length its family has, so the file reopens as that format."""
    reg = default_registry()
    path = tmp_path / "bank.cgx"
    size = pipeline.create_file(
        str(path),
        kind=ContentKind.PIXELS,
        container_id="container.scgcad-cgx",
        codec_id=_4BPP,
        units=_CGX_4BPP_TILES,
        reg=reg,
    )
    assert size == 0x8000
    assert path.stat().st_size > size  # header and row table around the tiles
    from celpix.plugins.detect import detect_container

    assert detect_container(reg, str(path)) == "container.scgcad-cgx"


def test_frames_new_file_probes_the_container_at_this_size() -> None:
    """Behavioural, and size-dependent: the bank format builds its framing for
    the payload lengths its family has and passes anything else through."""
    reg = default_registry()
    cgx = "container.scgcad-cgx"
    assert pipeline.frames_new_file(
        ContentKind.PIXELS, cgx, _4BPP, _CGX_4BPP_TILES, reg
    )
    assert not pipeline.frames_new_file(ContentKind.PIXELS, cgx, _4BPP, 16, reg)
    # Plain bytes frame nothing, and iNES can only preserve a header it was
    # shown - neither adds anything to a payload handed over fresh.
    assert not pipeline.frames_new_file(
        ContentKind.PIXELS, RAW_CONTAINER, _4BPP, 256, reg
    )
    assert not pipeline.frames_new_file(
        ContentKind.PIXELS, "container.ines", _4BPP, 256, reg
    )


def test_new_file_refuses_a_size_the_container_cannot_frame(tmp_path) -> None:
    """A copier image is whole 16 KiB blocks behind a 512-byte header. Handed
    less than one, the container writes the header and drops the rest - so the
    result is read back and refused before it can be called a file."""
    reg = default_registry()
    smd = "container.smd"
    path = tmp_path / "tiles.smd"
    with pytest.raises(PipelineError, match="would hold 0 bytes"):
        pipeline.create_file(
            str(path),
            kind=ContentKind.PIXELS,
            container_id=smd,
            codec_id=_4BPP,
            units=256,
            reg=reg,
        )
    assert not path.exists()
    # The dialog's probe is the same call, so it refuses at the same size and
    # goes through at a whole block.
    with pytest.raises(PipelineError):
        pipeline.frames_new_file(ContentKind.PIXELS, smd, _4BPP, 256, reg)
    assert pipeline.frames_new_file(ContentKind.PIXELS, smd, _4BPP, 512, reg)


def test_new_tpl_palette_names_its_codec_in_the_header(tmp_path) -> None:
    """A TPL header states the color format, and a new file has no file to copy
    it from - so it is built from the codec the payload is written in."""
    from celpix.plugins.detect import detect_container

    reg = default_registry()
    tpl = "container.tpl-palette"
    path = tmp_path / "colors.tpl"
    pipeline.create_file(
        str(path),
        kind=ContentKind.PALETTE,
        container_id=tpl,
        codec_id=_BGR555,
        units=16,
        reg=reg,
    )
    assert path.read_bytes() == b"TPL\x02" + bytes(32)
    assert detect_container(reg, str(path), kind=ContentKind.PALETTE) == tpl
    # A codec the header has no byte for is refused rather than guessed at.
    with pytest.raises(PipelineError, match="not a format a TPL header can name"):
        pipeline.blank_file_bytes(
            ContentKind.PALETTE, tpl, "preset.palette.bgr555-snes", 16, reg
        )


def test_blank_size_reports_an_unusable_codec_as_a_pipeline_error() -> None:
    reg = default_registry()
    with pytest.raises((PipelineError, KeyError)):
        pipeline.blank_size(ContentKind.PIXELS, "preset.pixel.nonesuch", 4, reg)


# -- the dialog -----------------------------------------------------------


def test_dialog_content_row_reshapes_the_size_row_and_the_codec_list(qtbot) -> None:
    dialog = NewFileDialog(default_registry())
    qtbot.addWidget(dialog)
    assert dialog._codec.currentData() == _4BPP
    assert dialog._rows.isVisibleTo(dialog) and not dialog._colors.isVisibleTo(dialog)

    dialog._content.setCurrentIndex(dialog._content.findData(ContentKind.PALETTE))
    assert dialog._codec.currentData() == _BGR555
    # A palette is a run, so the grid goes away and the color count appears.
    assert dialog._colors.isVisibleTo(dialog) and not dialog._rows.isVisibleTo(dialog)

    dialog._content.setCurrentIndex(dialog._content.findData(ContentKind.TILEMAP))
    assert dialog._codec.currentData() == _SNES_BG
    # And switching back lands on the format that kind was left on, not on
    # whichever preset sorts first.
    dialog._content.setCurrentIndex(dialog._content.findData(ContentKind.PIXELS))
    assert dialog._codec.currentData() == _4BPP


def test_dialog_offers_only_containers_that_can_write_this_content(qtbot) -> None:
    from uihelpers import _combo_ids

    dialog = NewFileDialog(default_registry())
    qtbot.addWidget(dialog)
    pixels = set(_combo_ids(dialog._container))
    dialog._content.setCurrentIndex(dialog._content.findData(ContentKind.PALETTE))
    palettes = set(_combo_ids(dialog._container))
    assert RAW_CONTAINER in pixels and RAW_CONTAINER in palettes
    assert "container.ines" in pixels and "container.ines" not in palettes


def test_dialog_states_the_byte_size_and_the_unbuildable_framing(qtbot) -> None:
    dialog = NewFileDialog(default_registry())
    qtbot.addWidget(dialog)
    dialog._columns.setValue(16)
    dialog._rows.setValue(16)
    assert "8,192 bytes" in dialog._size.text()
    assert not dialog._note.isVisibleTo(dialog)  # plain bytes frame nothing

    at = dialog._container.findData("container.ines")
    dialog._container.setCurrentIndex(at)
    assert "will not reopen as that format" in dialog._note.text()


def test_dialog_params_fold_a_palettes_count_into_one_axis(qtbot) -> None:
    dialog = NewFileDialog(default_registry())
    qtbot.addWidget(dialog)
    dialog._content.setCurrentIndex(dialog._content.findData(ContentKind.PALETTE))
    dialog._colors.setValue(64)
    dialog._accept()
    params = dialog._params
    assert params is not None
    assert (params.columns, params.rows, params.units) == (64, 1, 64)


# -- the gesture ----------------------------------------------------------


def _run_new_file(window, monkeypatch, path, params) -> None:
    """Drive File ▸ New File… with the dialog and picker already answered."""
    monkeypatch.setattr(
        NewFileDialog, "get_params", staticmethod(lambda *a, **k: params)
    )
    monkeypatch.setattr(
        "celpix.ui.main_window.entries.ask_save_path", lambda *a, **k: str(path)
    )
    window._new_file()


def test_new_file_creates_the_file_and_stamps_its_answers_on_the_entry(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    path = tmp_path / "sheet.bin"
    _run_new_file(
        window,
        monkeypatch,
        path,
        NewFileParams(ContentKind.PIXELS, RAW_CONTAINER, _4BPP, 8, 4),
    )
    entry = window._workspace.current
    assert path.read_bytes() == bytes(8 * 4 * 32)
    assert entry is not None and entry.path == str(path)
    assert entry.content_kind is ContentKind.PIXELS
    assert entry.container_id == RAW_CONTAINER
    assert entry.session.pixel_preset_id == _4BPP
    # The size the dialog was given is the shape the sheet opens at, rather than
    # the window's own 16x16 default.
    assert (entry.doc.view.columns, entry.doc.view.rows) == (8, 4)


def test_new_tilemap_file_carries_its_cell_codec(qtbot, tmp_path, monkeypatch) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    path = tmp_path / "screen.bin"
    _run_new_file(
        window,
        monkeypatch,
        path,
        NewFileParams(ContentKind.TILEMAP, RAW_CONTAINER, _SNES_BG, 32, 32),
    )
    entry = window._workspace.current
    assert path.stat().st_size == 32 * 32 * 2
    assert entry.content_kind is ContentKind.TILEMAP
    assert entry.tilemap_preset_id == _SNES_BG


def test_new_palette_file_registers_under_the_codec_it_was_written_with(
    qtbot, tmp_path, monkeypatch
) -> None:
    """A .pal records nothing about its own encoding, so the entry has to."""
    window = MainWindow()
    qtbot.addWidget(window)
    path = tmp_path / "colors.pal"
    _run_new_file(
        window,
        monkeypatch,
        path,
        NewFileParams(ContentKind.PALETTE, RAW_CONTAINER, _BGR555, 128, 1),
    )
    entry = window._workspace.find_palette(str(path))
    assert entry is not None
    assert path.stat().st_size == 256
    assert entry.palette_preset_id == _BGR555
    assert entry.session is None  # registered, never activated


def test_new_file_refuses_a_path_already_open(
    qtbot, tmp_path, monkeypatch, captured_alerts
) -> None:
    """Creating over an open file would blank it under the entry editing it,
    which no undo puts back."""
    window = MainWindow()
    qtbot.addWidget(window)
    path = tmp_path / "sheet.bin"
    params = NewFileParams(ContentKind.PIXELS, RAW_CONTAINER, _4BPP, 8, 4)
    _run_new_file(window, monkeypatch, path, params)
    before = path.read_bytes()
    path.write_bytes(b"\x77" * len(before))

    _run_new_file(window, monkeypatch, path, params)
    assert path.read_bytes() == b"\x77" * len(before)  # untouched
    assert any("already open" in message for _title, message in captured_alerts)
    assert len(window._workspace.entries) == 1


def test_undoing_a_new_file_closes_the_entry_and_keeps_the_file(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    path = tmp_path / "sheet.bin"
    _run_new_file(
        window,
        monkeypatch,
        path,
        NewFileParams(ContentKind.PIXELS, RAW_CONTAINER, _4BPP, 8, 4),
    )
    window._undo_stack.undo()
    assert window._workspace.entries == []
    assert Path(path).exists()
