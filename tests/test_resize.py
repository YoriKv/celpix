"""Resizing a file: the payload arithmetic, and the size row on Edit File
Container that asks for it."""

from __future__ import annotations

import pytest

from celpix.core.capabilities import ContentKind
from celpix.core.errors import PipelineError
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import NO_RESHAPE, RAW_CONTAINER, FileRef
from celpix.plugins.registry import default_registry
from celpix.ui.container_dialog import ContainerDialog, ContainerEdit
from celpix.ui.main_window import MainWindow
from uihelpers import _make_snes_file

_4BPP = "preset.pixel.snes-4bpp"
_BGR555 = "preset.palette.bgr555"
# The 4bpp variant of the tile-bank family: 0x8000 bytes of tiles at 32 bytes a
# tile. The one payload size that container builds a bank around.
_CGX = "container.scgcad-cgx"
_CGX_TILES = 0x8000 // 32


def _config(path: str, container_id: str = RAW_CONTAINER) -> PathwayConfig:
    return PathwayConfig(
        source=FileRef((path,)),
        interpret_preset_id=_4BPP,
        container_id=container_id,
    )


def _blank(tmp_path, units: int, container_id: str = RAW_CONTAINER):
    path = tmp_path / "tiles.bin"
    pipeline.create_file(
        str(path),
        kind=ContentKind.PIXELS,
        container_id=container_id,
        codec_id=_4BPP,
        units=units,
        reg=default_registry(),
    )
    return path


# -- the arithmetic ---------------------------------------------------------
def test_blank_units_inverts_blank_size() -> None:
    """The number the size row puts in the spins has to come back out as the
    same file, so the two directions are one round trip."""
    reg = default_registry()
    for kind, codec, units in (
        (ContentKind.PIXELS, _4BPP, 100),
        (ContentKind.TILEMAP, "preset.tilemap.snes-bg", 1024),
        (ContentKind.PALETTE, _BGR555, 256),
    ):
        size = pipeline.blank_size(kind, codec, units, reg)
        assert pipeline.blank_units(kind, codec, size, reg) == units


def test_blank_units_floors_a_partial_trailing_unit() -> None:
    """Half a tile is not a tile the codec can read, so it is not counted."""
    reg = default_registry()
    assert pipeline.blank_units(ContentKind.PIXELS, _4BPP, 32 * 8 + 17, reg) == 8


def test_resize_grows_with_zeroes_and_keeps_what_was_there(tmp_path) -> None:
    reg = default_registry()
    path = _blank(tmp_path, 8)
    path.write_bytes(b"\xab" * (8 * 32))
    size = pipeline.resize_file(
        _config(str(path)), kind=ContentKind.PIXELS, codec_id=_4BPP, units=12, reg=reg
    )
    assert size == 12 * 32
    assert path.read_bytes() == b"\xab" * (8 * 32) + bytes(4 * 32)


def test_resize_shrinks_off_the_tail(tmp_path) -> None:
    reg = default_registry()
    path = _blank(tmp_path, 8)
    path.write_bytes(bytes(range(256)) * ((8 * 32) // 256))
    head = path.read_bytes()[: 3 * 32]
    pipeline.resize_file(
        _config(str(path)), kind=ContentKind.PIXELS, codec_id=_4BPP, units=3, reg=reg
    )
    assert path.read_bytes() == head  # the front is untouched, the tail is gone


def test_resize_rebuilds_the_container_framing(tmp_path) -> None:
    """The payload is what is resized, so a container that builds a header gets
    to restate it — which is what keeps the file readable as that format."""
    from celpix.plugins.detect import detect_container

    reg = default_registry()
    path = tmp_path / "bank.cgx"
    pipeline.create_file(
        str(path),
        kind=ContentKind.PIXELS,
        container_id=_CGX,
        codec_id=_4BPP,
        units=_CGX_TILES,
        reg=reg,
    )
    cfg = _config(str(path), _CGX)
    assert len(pipeline.read_region(cfg, reg)[0]) == 0x8000

    pipeline.resize_file(
        cfg, kind=ContentKind.PIXELS, codec_id=_4BPP, units=_CGX_TILES // 2, reg=reg
    )
    assert detect_container(reg, str(path)) == _CGX  # still its own format
    assert len(pipeline.read_region(cfg, reg)[0]) == 0x4000
    assert path.stat().st_size > 0x4000  # the framing is still around it


def test_resize_refuses_a_size_a_fixed_format_cannot_hold(tmp_path) -> None:
    """A screen is 0x2000 bytes of cells whatever it is handed: its write splices
    over the file it has. Read back, that is the old size - so the resize is
    refused rather than reported as done."""
    reg = default_registry()
    scr, cells = "container.scgcad-scr", "preset.tilemap.snes-bg"
    path = tmp_path / "screen.scr"
    pipeline.create_file(
        str(path),
        kind=ContentKind.TILEMAP,
        container_id=scr,
        codec_id=cells,
        units=64 * 64,
        reg=reg,
    )
    before = path.read_bytes()
    cfg = PathwayConfig(
        source=FileRef((str(path),)), interpret_preset_id=cells, container_id=scr
    )
    with pytest.raises(PipelineError, match="keeps 8,192 bytes"):
        pipeline.resize_file(
            cfg, kind=ContentKind.TILEMAP, codec_id=cells, units=32 * 32, reg=reg
        )
    assert path.read_bytes() == before


def test_resize_refuses_to_grow_a_bank_past_its_family(tmp_path) -> None:
    """Tiles of no size a bank holds would be spliced over the header and row
    table; the container refuses instead, and the file stays a bank."""
    from celpix.plugins.detect import detect_container

    reg = default_registry()
    path = tmp_path / "bank.cgx"
    pipeline.create_file(
        str(path),
        kind=ContentKind.PIXELS,
        container_id=_CGX,
        codec_id=_4BPP,
        units=_CGX_TILES,
        reg=reg,
    )
    before = path.read_bytes()
    with pytest.raises(PipelineError, match="none of them"):
        pipeline.resize_file(
            _config(str(path), _CGX),
            kind=ContentKind.PIXELS,
            codec_id=_4BPP,
            units=_CGX_TILES + 8,
            reg=reg,
        )
    assert path.read_bytes() == before
    assert detect_container(reg, str(path)) == _CGX


def test_resize_refuses_a_joined_region(tmp_path) -> None:
    """The boundary between two files is the length of the first, and nothing
    else records which bytes belong to which chip."""
    reg = default_registry()
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(bytes(256))
    b.write_bytes(bytes(256))
    cfg = PathwayConfig(source=FileRef((str(a), str(b))), interpret_preset_id=_4BPP)
    with pytest.raises(ValueError, match="boundaries"):
        pipeline.resize_file(
            cfg, kind=ContentKind.PIXELS, codec_id=_4BPP, units=4, reg=reg
        )
    assert a.read_bytes() == bytes(256) and b.read_bytes() == bytes(256)


def test_resize_refuses_a_read_only_pathway(tmp_path) -> None:
    reg = default_registry()
    path = _blank(tmp_path, 8)
    cfg = PathwayConfig(
        source=FileRef((str(path),)),
        interpret_preset_id=_4BPP,
        write_enabled=False,
    )
    with pytest.raises(ValueError, match="read-only"):
        pipeline.resize_file(
            cfg, kind=ContentKind.PIXELS, codec_id=_4BPP, units=4, reg=reg
        )
    assert path.stat().st_size == 8 * 32


# -- the size row -----------------------------------------------------------
def _dialog(path, **kwargs) -> ContainerDialog:
    defaults = {
        "container_id": RAW_CONTAINER,
        "kind": ContentKind.PIXELS,
        "codec_id": _4BPP,
    }
    return ContainerDialog(default_registry(), paths=(str(path),), **defaults | kwargs)


def test_size_row_seeds_exactly_what_the_region_holds(qtbot) -> None:
    """A count, not a grid, so 100 tiles seeds 100 and asks for nothing. A grid
    would have had to seed ceil(100 / 16) rows = 112 and then explain itself."""
    dialog = _dialog("tiles.bin", units=100)
    qtbot.addWidget(dialog)
    assert dialog._size_units.value() == 100
    assert dialog.size_units() == 100
    assert dialog.resize_units() is None
    assert dialog._size.text() == f"{100 * 32:,} bytes"


def test_size_row_reports_the_change_once_moved(qtbot) -> None:
    dialog = _dialog("tiles.bin", units=100)
    qtbot.addWidget(dialog)
    dialog._size_units.setValue(128)
    assert dialog.resize_units() == 128
    assert dialog._size.text() == f"{128 * 32:,} bytes (currently {100 * 32:,})"


def test_size_row_asks_for_no_resize_at_the_same_length(qtbot) -> None:
    """Moving the spin and moving it back is not a resize."""
    dialog = _dialog("tiles.bin", units=128)
    qtbot.addWidget(dialog)
    dialog._size_units.setValue(4)
    dialog._size_units.setValue(128)
    assert dialog.resize_units() is None


def test_size_row_never_clamps_a_file_bigger_than_the_grid_could_state(qtbot) -> None:
    """An 8 MB ROM holds more tiles than any grid this app draws. A ceiling that
    clamped the seed would open reading as a shrink nobody asked for."""
    huge = (8 << 20) // 32
    dialog = _dialog("rom.sfc", units=huge)
    qtbot.addWidget(dialog)
    assert dialog._size_units.value() == huge
    assert dialog.resize_units() is None


def test_size_row_is_dead_for_a_joined_region(qtbot) -> None:
    dialog = _dialog("a.bin", units=16)
    qtbot.addWidget(dialog)
    dialog._paths.append("b.bin")
    dialog._rebuild_rows()
    assert not dialog._size_units.isEnabled()
    assert "2 files" in dialog._size.text()
    assert dialog.resize_units() is None


def test_palette_size_row_counts_colours(qtbot) -> None:
    dialog = _dialog("colors.pal", kind=ContentKind.PALETTE, codec_id=_BGR555, units=64)
    qtbot.addWidget(dialog)
    assert dialog._size_caption.text() == "Colors:"
    assert dialog._size_units.value() == 64
    dialog._size_units.setValue(32)
    assert dialog.resize_units() == 32


def test_packed_palette_counts_that_share_a_length_are_not_a_resize(qtbot) -> None:
    """A packed format rounds up to a whole read unit, so several colour counts
    come to the same bytes — and a size the file already has is no resize."""
    packed = "preset.palette.gb-bgp"
    if not default_registry().has_preset(packed):
        pytest.skip("no packed palette format registered")
    dialog = _dialog("colors.pal", kind=ContentKind.PALETTE, codec_id=packed, units=4)
    qtbot.addWidget(dialog)
    dialog._size_units.setValue(3)  # still one byte, so still the same file
    assert dialog.resize_units() is None


# -- the gesture ------------------------------------------------------------
def _answer(monkeypatch, answer) -> None:
    monkeypatch.setattr(
        ContainerDialog, "edit_container", staticmethod(lambda *_a, **_k: answer)
    )


def test_edit_container_resizes_the_file_and_re_reads(
    qtbot, tmp_path, monkeypatch
) -> None:
    px = _make_snes_file(tmp_path)  # 8 tiles
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.find_file(str(px))
    assert len(entry.doc.pixel_data) == 8 * 32

    _answer(monkeypatch, ContainerEdit(RAW_CONTAINER, (str(px),), units=12))
    window._change_container_for(entry)
    assert px.stat().st_size == 12 * 32
    assert len(entry.doc.pixel_data) == 12 * 32  # the entry re-read the new bytes


def test_shrinking_asks_first_and_a_refusal_calls_the_whole_edit_off(
    qtbot, tmp_path, monkeypatch, confirmations
) -> None:
    """The prompt is the only gate on a truncation, since no undo puts the tail
    back — so declining must leave the container alone as well as the bytes."""
    px = _make_snes_file(tmp_path)
    before = px.read_bytes()
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.find_file(str(px))
    entry.container_id = RAW_CONTAINER
    depth = window._undo_stack.count()

    confirmations.yes = False
    # The reshape rather than the container is the other half of the edit here:
    # it is a permutation, so the payload keeps its length and the shrink stays
    # one — a header-stripping container would move that length itself.
    _answer(
        monkeypatch,
        ContainerEdit(
            RAW_CONTAINER, (str(px),), reshape_id="reshape.swap-bytes-2", units=4
        ),
    )
    window._change_container_for(entry)
    assert confirmations.asked and "drops" in confirmations.asked[0]
    assert px.read_bytes() == before  # nothing written
    assert entry.reshape_id == NO_RESHAPE  # and the rest of the edit is off too
    assert window._undo_stack.count() == depth  # nothing to undo


def test_growing_asks_nothing(qtbot, tmp_path, monkeypatch, confirmations) -> None:
    """Zeroes past the end take nothing away, so there is nothing to confirm."""
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.find_file(str(px))

    _answer(monkeypatch, ContainerEdit(RAW_CONTAINER, (str(px),), units=16))
    window._change_container_for(entry)
    assert confirmations.asked == []  # and so the default Cancel never bit
    assert px.stat().st_size == 16 * 32


def test_shrink_prompt_names_the_slices_it_would_orphan(
    qtbot, tmp_path, monkeypatch, confirmations
) -> None:
    px = _make_snes_file(tmp_path)  # 8 tiles = 256 bytes
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.find_file(str(px))
    window._workspace.add_slice(str(px), "late", 192, 32)  # past a 2-tile file

    _answer(monkeypatch, ContainerEdit(RAW_CONTAINER, (str(px),), units=2))
    window._change_container_for(entry)
    assert "late" in confirmations.asked[0]


def test_a_resize_is_not_on_the_undo_stack(qtbot, tmp_path, monkeypatch) -> None:
    """The container change undoes; the bytes on disk do not come back, so the
    command is never told about them."""
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.find_file(str(px))
    entry.container_id = RAW_CONTAINER

    _answer(
        monkeypatch,
        ContainerEdit(RAW_CONTAINER, (str(px),), "reshape.swap-bytes-2", units=16),
    )
    window._change_container_for(entry)
    assert px.stat().st_size == 16 * 32
    assert entry.reshape_id == "reshape.swap-bytes-2"

    window._undo_stack.undo()
    assert entry.reshape_id == NO_RESHAPE  # the reading is put back
    assert px.stat().st_size == 16 * 32  # the file keeps its new size
