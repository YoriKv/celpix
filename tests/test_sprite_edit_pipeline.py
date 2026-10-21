"""The save path a sprite record edit needs: the write gate, the decoded extent,
the live re-read and the frames rebuilt per edit (``docs/design/sprite-map.md``
§8.2, §9)."""

from __future__ import annotations

from dataclasses import replace

from celpix.core.context import KEY_TILEMAP_FRAME_SIZES
from celpix.core.errors import Stage
from celpix.core.sprite import Subsprite, SubspriteLimits
from celpix.core.tilemap import Cell
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins import FormatInfo
from celpix.plugins.base import FileRef, PluginInfo
from celpix.plugins.formats import adapt_format
from celpix.plugins.registry import default_registry
from celpix.ui.main_window import MainWindow
from uihelpers import _make_snes_file, _obj_file

_TAIL = b"\xaa\xbb"
_LIMITS = SubspriteLimits("headed", range(-128, 128), range(-128, 128))


class _Headed:
    """A count byte, then that many two-byte records — X, then the tile.

    Headed on purpose: its encoding is one byte longer than ``cells * 2``, so a
    splice that counted cells instead of measuring would cut the tail a byte
    early, and one that kept ``old[len(new):]`` would re-append a dropped record.
    Declares no limits, so a map of it is view-only.
    """

    info = FormatInfo(
        id="format.tilemap.test-headed", name="Headed", declares={"layout": "sprite"}
    )

    def decode(self, data, ctx):  # noqa: ANN001, ANN201
        count = data[0] if data else 0
        return [
            Cell(index=data[2 + 2 * at], flags=data[1 + 2 * at])
            for at in range(count)
            if 2 + 2 * at < len(data)
        ]

    def encode(self, cells, ctx):  # noqa: ANN001, ANN201
        body = b"".join(bytes((c.flags & 0xFF, c.index & 0xFF)) for c in cells)
        return bytes((len(cells),)) + body

    def bytes_per_cell(self) -> int:
        return 2

    def cell_tiles(self) -> tuple[int, int]:
        return (1, 1)

    def frames(self, cells, ctx):  # noqa: ANN001, ANN201
        return [
            tuple(
                Subsprite(x=c.flags - 256 if c.flags > 127 else c.flags, index=c.index)
                for c in cells
            )
        ]


class _EditableHeaded(_Headed):
    info = FormatInfo(
        id="format.tilemap.test-headed-editable",
        name="Headed, editable",
        declares={"layout": "sprite"},
    )

    def subsprite_limits(self, ctx):  # noqa: ANN001, ANN201
        return _LIMITS


class _Unframed(_Headed):
    """Frames to ``[]``, as a sprite table does with no Frames region bound."""

    info = FormatInfo(
        id="format.tilemap.test-unframed",
        name="Unframed",
        declares={"layout": "sprite"},
    )

    def frames(self, cells, ctx):  # noqa: ANN001, ANN201
        return []


def _headless(tmp_path):  # noqa: ANN202
    """A registry carrying both stubs, and a builder for tilemap entries over it
    loaded the way the app loads them."""
    from celpix.core.capabilities import ContentKind
    from celpix.project.documents import load_document
    from celpix.project.workspace import Entry, EntryKind, EntrySession, Workspace

    registry, workspace = default_registry(), Workspace()
    for fmt in (_Headed(), _EditableHeaded(), _Unframed()):
        engine, preset = adapt_format(fmt, Stage.INTERPRET_TILEMAP)
        registry.register(engine)
        registry.register_preset(preset)

    def load(name: str, data: bytes, preset_id: str):  # noqa: ANN202
        path = tmp_path / name
        path.write_bytes(data)
        entry = Entry(name=name, kind=EntryKind.FILE, path=str(path))
        entry.content_kind = ContentKind.TILEMAP
        entry.tilemap_preset_id = preset_id
        entry.session = EntrySession("preset.pixel.snes-4bpp", "preset.palette.bgr555")
        workspace.entries.append(entry)
        return load_document(entry, registry, workspace).doc, path

    return registry, load


def _headed(*records: tuple[int, int]) -> bytes:
    return _Headed().encode([Cell(index=t, flags=x) for x, t in records], None)


def test_the_write_gate_is_the_codecs_declared_limits(tmp_path) -> None:
    """A sprite map is written and edited exactly where its codec declared what
    its records hold. A map that frames to ``[]`` — a table with no Frames
    region — is falsy, but a sprite map all the same, and as view-only as one
    with frames. The shipped table codec declares nothing."""
    registry, load = _headless(tmp_path)
    file = _headed((0, 1), (8, 2))

    editable, _ = load("e.bin", file, _EditableHeaded.info.id)
    assert editable.subsprite_limits == _LIMITS
    assert editable.tilemap_config.write_enabled and editable.cells_editable

    quiet, _ = load("q.bin", file, _Headed.info.id)
    assert quiet.is_sprite and quiet.subsprite_limits is None
    assert not quiet.tilemap_config.write_enabled and not quiet.cells_editable

    unframed, _ = load("u.bin", file, _Unframed.info.id)
    assert unframed.sprite_frames == [] and unframed.is_sprite
    assert not unframed.tilemap_config.write_enabled and not unframed.cells_editable

    path = tmp_path / "t.bin"
    path.write_bytes(b"\x00\x02")
    cfg = PathwayConfig(
        source=FileRef(str(path)), interpret_preset_id="preset.tilemap.sega-mappings-5"
    )
    table = pipeline.load_tilemap_data(cfg, registry)
    assert table.frames == [] and table.limits is None


def test_a_shrink_saves_the_tail_and_not_the_dropped_record(tmp_path) -> None:
    """The extent is the cells' own encoding — the count byte included — and the
    tail is what lies past it. Dropping a record shortens the encoding, and the
    save must write the new encoding and the file's tail, never the dropped
    record's bytes between them."""
    registry, load = _headless(tmp_path)
    doc, path = load(
        "e.bin", _headed((0, 1), (8, 2), (16, 3)) + _TAIL, _EditableHeaded.info.id
    )
    assert doc.tilemap_extent == 7  # 1 + 3 * 2, not 3 * bytes_per_cell

    doc.cells = doc.cells[:2]
    assert pipeline.save(doc, registry, palette=False).pixel
    assert path.read_bytes() == _headed((0, 1), (8, 2)) + _TAIL
    assert doc.tilemap_extent == 5


def test_a_live_reread_after_a_shrink_decodes_the_buffer_as_it_stands(
    tmp_path,
) -> None:
    """``live`` is the document's whole buffer, tail included; the file's bytes
    past its length are the dropped record again and must not come back."""
    registry, _load = _headless(tmp_path)
    path = tmp_path / "e.bin"
    path.write_bytes(_headed((0, 1), (8, 2), (16, 3)) + _TAIL)
    cfg = PathwayConfig(
        source=FileRef(str(path)), interpret_preset_id=_EditableHeaded.info.id
    )
    live = _headed((0, 1), (8, 2)) + _TAIL

    loaded = pipeline.load_tilemap_data(cfg, registry, live)

    assert loaded.data == live
    assert [c.index for c in loaded.cells] == [1, 2]
    assert loaded.data[loaded.extent :] == _TAIL
    assert loaded.disk == path.read_bytes()


def test_the_edited_frame_sizes_replace_the_files_before_the_decode(
    tmp_path,
) -> None:
    """A counted format's container publishes the file's counts; a re-read of
    edited cells has to be decoded under the counts the edit left."""
    seen = []

    class Counting:
        info = PluginInfo(
            id="container.test-counting", name="Counting", stage=Stage.CONTAINER
        )

        def read(self, source, ctx):  # noqa: ANN001, ANN202
            ctx.set(KEY_TILEMAP_FRAME_SIZES, (9, 9))
            return source.data

        def write(self, data, dest, ctx):  # noqa: ANN001, ANN202
            return data

    class Seeing(_EditableHeaded):
        info = FormatInfo(id="format.tilemap.test-seeing", name="Seeing")

        def decode(self, data, ctx):  # noqa: ANN001, ANN201
            seen.append(ctx.get(KEY_TILEMAP_FRAME_SIZES))
            return super().decode(data, ctx)

    registry = default_registry()
    registry.register(Counting())
    engine, preset = adapt_format(Seeing(), Stage.INTERPRET_TILEMAP)
    registry.register(engine)
    registry.register_preset(preset)
    path = tmp_path / "c.bin"
    path.write_bytes(_headed((0, 1)))
    cfg = PathwayConfig(
        source=FileRef(str(path)),
        interpret_preset_id=preset.id,
        container_id="container.test-counting",
    )

    pipeline.load_tilemap_data(cfg, registry)
    loaded = pipeline.load_tilemap_data(cfg, registry, frame_sizes=(1, 0))

    assert seen == [(9, 9), (1, 0)]
    assert loaded.ctx.get(KEY_TILEMAP_FRAME_SIZES) == (1, 0)


def test_a_cell_edit_rebuilds_the_frames_and_moves_the_extent(qtbot, tmp_path) -> None:
    """The picture is a function of the cells, so landing them re-frames; and
    the buffer under the map is spliced at the extent, so a list one record
    shorter leaves no stale record behind it."""
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str(_obj_file(tmp_path, [(0, 0, 1), (8, 0, 2)])))
    bank, entry = window._workspace.entries[0], window._workspace.current
    entry.tile_source = TileSource(mode=TileMode.ENTRY, entry=bank)
    entry.doc = None  # re-read through the new binding
    window._activate_entry(bank)
    window._activate_entry(entry)
    doc = entry.doc
    assert [s.index for s in doc.sprite_frames[0]] == [1, 2]

    cells = list(doc.cells)
    cells[1] = replace(cells[1], index=5)
    window._set_cells(entry, cells, entry.pixel_revision + 1)
    assert [s.index for s in entry.doc.sprite_frames[0]] == [1, 5]

    window._set_cells(entry, cells[1:], entry.pixel_revision + 1)
    preset = doc.tilemap_config.interpret_preset_id
    encoded = pipeline.encode_cells(
        doc.settled_cells, preset, window._registry, doc.tilemap_ctx
    )
    assert doc.tilemap_data == encoded
    assert doc.tilemap_extent == len(encoded)
    assert [s.index for s in doc.sprite_frames[0]] == [5]
