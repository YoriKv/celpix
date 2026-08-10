"""User plugin discovery: drop a file into a typed subfolder and it loads."""

from __future__ import annotations

import struct

import pytest

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.core.tilemap import Cell, CellOp
from celpix.plugins import discovery
from celpix.plugins.base import ReadSource, WriteTarget
from celpix.plugins.bitswap import BitswapReshape
from celpix.plugins.data_lut import DataLutReshape
from celpix.plugins.registry import default_registry
from celpix.plugins.trust import TrustStore

# Auto-approve confirm callback for tests that aren't exercising the gate itself.
_ALLOW = lambda pending: True  # noqa: E731

# The magic header the seeded containers/ example wraps its payload in.
_MAGIC = b"CELPIXEX"


def _two_strip_tiff(first: bytes, second: bytes) -> bytes:
    """A minimal uncompressed little-endian TIFF holding two strips.

    The strips are separated by a filler gap on purpose: a TIFF's image data is
    reached through the directory and need not be contiguous, which is the whole
    reason the seeded example's reader joins and its writer scatters.
    """
    ifd_at, offsets_at, counts_at, first_at, second_at = 8, 50, 58, 66, 74
    out = bytearray(b"II*\x00" + struct.pack("<I", ifd_at))
    entries = (
        (259, 3, 1, struct.pack("<HH", 1, 0)),  # Compression: none, inline value
        (273, 4, 2, struct.pack("<I", offsets_at)),  # StripOffsets
        (279, 4, 2, struct.pack("<I", counts_at)),  # StripByteCounts
    )
    out += struct.pack("<H", len(entries))
    for tag, type_id, count, field in entries:
        out += struct.pack("<HHI", tag, type_id, count) + field
    out += struct.pack("<I", 0)  # no second image
    assert len(out) == offsets_at
    out += struct.pack("<II", first_at, second_at)
    out += struct.pack("<II", len(first), len(second))
    assert len(out) == first_at
    return bytes(out + first + b"\x99" * 4 + second)


# A minimal code plugin: a container plus the register() hook the host calls.
# Belongs in containers/. It deliberately omits the stage — the shipped examples
# state theirs, so this is what covers the other path, where the folder supplies
# it and the shape check is the only thing standing between a misplaced plugin
# and a registration it cannot honour.
_CODE_PLUGIN = """
from celpix.plugins.base import PluginInfo


class HelloContainer:
    info = PluginInfo(id="container.hello", name="Hello container")

    def read(self, source, ctx):
        return b"hello"


def register(registry):
    registry.register(HelloContainer())
"""

# A zero-code preset: a new 1bpp planar format for the built-in planar engine.
# No stage field — the pixel/ folder it is dropped into determines the stage.
_PRESET = """
id = "preset.pixel.custom-1bpp"
name = "Custom 1bpp"
engine_id = "codec.pixel.planar"

[params]
bpp = 1
planes = [ { base = 0, stride = 1 } ]
"""

# A self-contained code format: 2x2 tiles, one byte per tile, 2 bits per pixel.
_FORMAT_PLUGIN = """
from celpix.core.index_grid import IndexGrid
from celpix.plugins import FormatInfo


class TwoBit2x2:
    info = FormatInfo(id="format.pixel.twobit", name="Two-bit 2x2")

    def decode(self, data, ctx):
        tiles = []
        for b in data:
            tile = IndexGrid(2, 2)
            for i in range(4):
                tile.set(i % 2, i // 2, (b >> (6 - 2 * i)) & 0x3)
            tiles.append(tile)
        return tiles

    def encode(self, tiles, ctx):
        out = bytearray()
        for tile in tiles:
            b = 0
            for i in range(4):
                b |= tile.get(i % 2, i // 2) << (6 - 2 * i)
            out.append(b)
        return bytes(out)

    def bytes_per_tile(self):
        return 1

    def tile_size(self):
        return (2, 2)


def register(registry):
    registry.register_format(TwoBit2x2())
"""

# One file registering two plugins, one of which is out of scope for its folder:
# the scheme belongs in compression/, the container does not. Only the offending
# registration may be skipped — the rest of the file still loads.
_SCHEME_PLUGIN = """
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo


class Doubler:
    info = PluginInfo(id="compression.double", name="Doubler", stage=Stage.COMPRESSION)

    def decompress(self, data, ctx):
        return data + data

    def compress(self, data, ctx):
        return data[: len(data) // 2]


class Stray:
    info = PluginInfo(id="container.stray", name="Stray", stage=Stage.CONTAINER)

    def read(self, source, ctx):
        return b""


def register(registry):
    registry.register(Doubler())
    registry.register(Stray())
"""


def _drop(root, folder: str, name: str, text: str) -> None:
    """Write one plugin file into a typed subfolder of the plugin root."""
    sub = root / folder
    sub.mkdir(parents=True, exist_ok=True)
    (sub / name).write_text(text, encoding="utf-8")


def test_drop_in_preset_is_registered_and_usable(tmp_path) -> None:
    _drop(tmp_path, "pixel", "custom.toml", _PRESET)
    reg = default_registry()

    issues = discovery.load_directory(reg, str(tmp_path))
    assert issues == []

    preset = reg.preset("preset.pixel.custom-1bpp")
    assert preset.stage is Stage.INTERPRET_PIXEL  # inferred from the folder
    engine = reg.plugin(Stage.INTERPRET_PIXEL, preset.engine_id)
    tiles = engine.decode(b"\x80" + b"\x00" * 7, preset.params, PipelineContext())
    assert tiles[0].get(0, 0) == 1  # leftmost bit set -> index 1


def test_drop_in_code_plugin_loads_when_approved(tmp_path) -> None:
    _drop(tmp_path, "containers", "hello.py", _CODE_PLUGIN)
    reg = default_registry()

    issues = discovery.load_directory(reg, str(tmp_path), confirm=_ALLOW)
    assert issues == []

    plugin = reg.plugin(Stage.CONTAINER, "container.hello")
    assert plugin.read(None, PipelineContext()) == b"hello"


def test_code_plugin_skipped_when_not_approved(tmp_path) -> None:
    _drop(tmp_path, "containers", "hello.py", _CODE_PLUGIN)
    reg = default_registry()

    # No confirm callback and nothing trusted -> default deny.
    issues = discovery.load_directory(reg, str(tmp_path))
    assert len(issues) == 1
    assert "not approved" in issues[0].message
    with pytest.raises(KeyError):
        reg.plugin(Stage.CONTAINER, "container.hello")


def _plugin_dir(tmp_path):
    # Mirror production: plugins live in a subdir; the trust store sits outside it
    # (in the data dir) so it is never itself scanned as a plugin.
    plugdir = tmp_path / "plugins"
    _drop(plugdir, "containers", "hello.py", _CODE_PLUGIN)
    trust = TrustStore(str(tmp_path / "trust.json"))
    return plugdir, trust


def test_approval_is_remembered_by_hash(tmp_path) -> None:
    plugdir, trust = _plugin_dir(tmp_path)

    # First load: approve once; it is remembered.
    reg1 = default_registry()
    issues1 = discovery.load_directory(reg1, str(plugdir), trust=trust, confirm=_ALLOW)
    assert issues1 == []
    assert reg1.plugin(Stage.CONTAINER, "container.hello")

    # Second load: deny everything, but the trusted hash loads silently.
    deny = lambda pending: False  # noqa: E731
    reg2 = default_registry()
    issues2 = discovery.load_directory(reg2, str(plugdir), trust=trust, confirm=deny)
    assert issues2 == []
    assert reg2.plugin(Stage.CONTAINER, "container.hello")


def test_changed_code_is_reprompted_in_a_new_run(tmp_path) -> None:
    plugdir, trust = _plugin_dir(tmp_path)
    discovery.load_directory(
        default_registry(), str(plugdir), trust=trust, confirm=_ALLOW
    )

    # Editing the file changes its hash. A *fresh* run (new TrustStore reading the
    # persisted file — empty session set) does not trust the new hash, so it prompts.
    _drop(plugdir, "containers", "hello.py", _CODE_PLUGIN + "\n# edited\n")
    fresh_trust = TrustStore(str(tmp_path / "trust.json"))
    reg = default_registry()
    deny = lambda pending: False  # noqa: E731
    issues = discovery.load_directory(
        reg, str(plugdir), trust=fresh_trust, confirm=deny
    )
    assert len(issues) == 1
    with pytest.raises(KeyError):
        reg.plugin(Stage.CONTAINER, "container.hello")


def test_session_edit_reloads_without_prompt(tmp_path) -> None:
    plugdir, trust = _plugin_dir(tmp_path)
    # Approve once this run -> the path becomes session-trusted.
    discovery.load_directory(
        default_registry(), str(plugdir), trust=trust, confirm=_ALLOW
    )

    # Edit the code and reload within the *same* run (same TrustStore): the
    # developer loop auto-approves the changed file, even with a denying callback.
    _drop(plugdir, "containers", "hello.py", _CODE_PLUGIN + "\n# edited\n")
    reg = default_registry()
    deny = lambda pending: False  # noqa: E731
    issues = discovery.load_directory(reg, str(plugdir), trust=trust, confirm=deny)
    assert issues == []
    assert reg.plugin(Stage.CONTAINER, "container.hello")


def test_broken_preset_is_reported_not_raised(tmp_path) -> None:
    _drop(tmp_path, "pixel", "bad.toml", "this is not valid toml")
    reg = default_registry()

    issues = discovery.load_directory(reg, str(tmp_path))
    assert len(issues) == 1
    assert "bad.toml" in issues[0].path


def test_module_without_register_is_reported(tmp_path) -> None:
    _drop(tmp_path, "containers", "nohook.py", "x = 1\n")
    reg = default_registry()

    # Approve past the gate so we reach (and report) the missing register hook.
    issues = discovery.load_directory(reg, str(tmp_path), confirm=_ALLOW)
    assert len(issues) == 1
    assert "register" in issues[0].message


def test_env_path_is_searched(tmp_path, monkeypatch) -> None:
    _drop(tmp_path, "pixel", "custom.toml", _PRESET)
    monkeypatch.setenv(discovery.ENV_PLUGIN_PATH, str(tmp_path))
    reg = default_registry()

    issues = discovery.load_user_plugins(reg)
    assert issues == []
    assert reg.preset("preset.pixel.custom-1bpp")


def test_project_plugin_dir_only_when_the_folder_is_there(tmp_path) -> None:
    project = tmp_path / "hack.celpix"
    project.write_text("{}", encoding="utf-8")

    assert discovery.project_plugin_dir(None) is None  # no project open
    assert discovery.project_plugin_dir(str(project)) is None  # no plugins folder
    (tmp_path / "plugins").mkdir()
    assert discovery.project_plugin_dir(str(project)) == str(tmp_path / "plugins")


def test_missing_directory_is_silent(tmp_path) -> None:
    reg = default_registry()
    assert discovery.load_directory(reg, str(tmp_path / "does-not-exist")) == []


# -- the typed layout's own guarantees ------------------------------------------


def test_a_misplaced_plugin_is_reported_and_the_rest_still_loads(tmp_path) -> None:
    """The folder determines a plugin's stage, so being in the wrong one has to be
    caught by something other than a stage field it no longer has to declare.

    Two ways in, and both are covered here because a plugin may still *state* its
    stage: a stated one that disagrees with the folder is refused outright, and one
    that says nothing is refused for not having the methods that folder's stage
    needs. The second is the stronger check — it would also catch a typo'd method
    name in a plugin that is in the right place.
    """
    # Says nothing, so it is judged on shape: a container has no decode/encode.
    _drop(tmp_path, "pixel", "misplaced.py", _CODE_PLUGIN)
    # Registers one in-scope plugin plus one that *declares* a foreign stage; the
    # scope check is per registration, so the first still loads.
    _drop(tmp_path, "compression", "scheme.py", _SCHEME_PLUGIN)
    reg = default_registry()

    issues = discovery.load_directory(reg, str(tmp_path), confirm=_ALLOW)
    reported = " ".join(issue.message for issue in issues)
    assert len(issues) == 2
    assert "is missing decode, encode, bytes_per_tile, tile_size" in reported
    assert "not allowed in folder 'compression/'" in reported
    for plugin_id in ("container.hello", "container.stray"):
        with pytest.raises(KeyError):
            reg.plugin(Stage.CONTAINER, plugin_id)
    with pytest.raises(KeyError):
        reg.plugin(Stage.INTERPRET_PIXEL, "container.hello")
    assert reg.plugin(Stage.COMPRESSION, "compression.double")


def test_loose_root_file_reported_and_unknown_folder_ignored(tmp_path) -> None:
    (tmp_path / "custom.toml").write_text(_PRESET, encoding="utf-8")
    # Parked plugins: an unknown folder name is skipped without complaint.
    _drop(tmp_path, "pixel.off", "parked.toml", _PRESET)
    reg = default_registry()

    issues = discovery.load_directory(reg, str(tmp_path))
    assert len(issues) == 1
    assert "typed subfolders" in issues[0].message
    with pytest.raises(KeyError):
        reg.preset("preset.pixel.custom-1bpp")


def test_preset_in_code_only_folder_is_reported(tmp_path) -> None:
    _drop(tmp_path, "compression", "custom.toml", _PRESET)
    reg = default_registry()

    issues = discovery.load_directory(reg, str(tmp_path))
    assert len(issues) == 1
    assert "pixel/palette/tilemap/reshape only" in issues[0].message


def test_conflicting_legacy_stage_field_is_reported(tmp_path) -> None:
    # A matching leftover stage field still loads (cheap migration tolerance)...
    _drop(tmp_path, "pixel", "ok.toml", 'stage = "interpret-pixel"\n' + _PRESET)
    reg = default_registry()
    assert discovery.load_directory(reg, str(tmp_path)) == []
    assert reg.preset("preset.pixel.custom-1bpp")

    # ...but a conflicting one is an error: the folder is authoritative.
    _drop(
        tmp_path,
        "palette",
        "conflict.toml",
        'stage = "interpret-pixel"\n' + _PRESET.replace("custom", "conflict"),
    )
    reg2 = default_registry()
    issues = discovery.load_directory(reg2, str(tmp_path))
    assert any("conflicts with the folder" in issue.message for issue in issues)
    with pytest.raises(KeyError):
        reg2.preset("preset.pixel.conflict-1bpp")


def test_code_format_lands_in_picker_and_round_trips(tmp_path) -> None:
    _drop(tmp_path, "pixel", "twobit.py", _FORMAT_PLUGIN)
    reg = default_registry()

    issues = discovery.load_directory(reg, str(tmp_path), confirm=_ALLOW)
    assert issues == []

    # The format surfaces as a preset (what the UI picker lists) and resolves
    # through the ordinary preset -> engine_id -> engine path.
    preset = reg.preset("format.pixel.twobit")
    assert preset in reg.presets(Stage.INTERPRET_PIXEL)
    engine = reg.plugin(Stage.INTERPRET_PIXEL, preset.engine_id)
    assert engine.bytes_per_tile(preset.params) == 1
    assert engine.tile_size(preset.params) == (2, 2)

    data = bytes([0b11_10_01_00, 0b00_01_10_11])
    ctx = PipelineContext()
    tiles = engine.decode(data, preset.params, ctx)
    assert [tiles[0].get(x, y) for y in range(2) for x in range(2)] == [3, 2, 1, 0]
    assert engine.encode(tiles, preset.params, ctx) == data


def test_underscore_files_are_ignored(tmp_path) -> None:
    # Inert-by-convention: _-prefixed files load nothing and report nothing,
    # even when their content is broken (that is what makes them safe examples).
    _drop(tmp_path, "pixel", "_broken.toml", "this is not valid toml")
    _drop(tmp_path, "pixel", "_broken.py", "raise RuntimeError('never runs')")
    reg = default_registry()

    assert discovery.load_directory(reg, str(tmp_path), confirm=_ALLOW) == []


def test_seeded_examples_are_valid_when_activated(tmp_path) -> None:
    # Seeding lays down the `_`-prefixed reference files, and each must actually
    # work once renamed — examples drifting from the real schema or format
    # contract is exactly the regression this guards.
    # No folders pre-made: seeding creates what it has content for, which is
    # what keeps a stage added after a user's plugin folder was from being
    # skipped for want of a directory.
    discovery.seed_examples(str(tmp_path))

    assert (tmp_path / discovery.PLUGIN_README).is_file()
    seeded = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("_*"))
    assert seeded == [
        "compression/_example.py",
        "containers/_example.py",
        "containers/_tiff.py",
        "palette/_color-indexed.toml",
        "palette/_color-mask.toml",
        "palette/_example.py",
        "palette/_nes-custom.py",
        "pixel/_direct-color.toml",
        "pixel/_example.py",
        "pixel/_linear-bespoke.toml",
        "pixel/_nibble-planar.toml",
        "pixel/_packed.toml",
        "pixel/_planar.toml",
        "reshape/_bitswap.toml",
        "reshape/_data-lut.toml",
        "reshape/_example.py",
        "tilemap/_example.py",
        "tilemap/_md-sprite.toml",
        "tilemap/_object.toml",
        "tilemap/_obz.toml",
        "tilemap/_packed.toml",
        "tilemap/_ys-spr.toml",
    ]

    # A stale reference file is replaced rather than left behind, so the examples
    # track the running build. Both the README and the `_` examples, and matched
    # by name, so this is what keeps an upgraded celPix from documenting itself
    # with a previous version's files.
    for stale in (tmp_path / "pixel" / "_planar.toml", tmp_path / "README.md"):
        shipped = stale.read_text(encoding="utf-8")
        stale.write_text("# stale\n", encoding="utf-8")
        discovery.seed_examples(str(tmp_path))
        assert stale.read_text(encoding="utf-8") == shipped

    # An activated copy is a different filename, so it is never matched.
    mine = tmp_path / "pixel" / "planar.toml"
    mine.write_text("# mine\n", encoding="utf-8")
    discovery.seed_examples(str(tmp_path))
    assert mine.read_text(encoding="utf-8") == "# mine\n"
    mine.unlink()

    # Activate every example (drop the underscore) and load for real.
    for path in tmp_path.rglob("_*"):
        path.rename(path.with_name(path.name[1:]))
    reg = default_registry()
    issues = discovery.load_directory(reg, str(tmp_path), confirm=_ALLOW)
    assert issues == []

    # Registering is necessary but not sufficient: a preset whose params drifted
    # from its engine, or a code format whose method signatures drifted, still
    # registers cleanly and only breaks at decode. Round-trip each example
    # through its stage so that drift fails here, matching this test's promise.
    ctx = PipelineContext()

    def pixel_round_trips(engine_id: str, params: dict) -> bool:
        eng = reg.plugin(Stage.INTERPRET_PIXEL, engine_id)
        data = bytes(range(eng.bytes_per_tile(params)))
        return eng.encode(eng.decode(data, params, ctx), params, ctx) == data

    def palette_round_trips(engine_id: str, params: dict, data: bytes) -> bool:
        eng = reg.plugin(Stage.INTERPRET_PALETTE, engine_id)
        return eng.encode(eng.decode(data, params, ctx), params, ctx) == data

    # There is one TOML example per engine, and each is only useful for the
    # engine it names — so they are enumerated from the seeded files rather than
    # listed here, and a new engine's example is covered the moment it lands.
    def toml_examples(folder: str, stage: Stage) -> list:
        return [
            discovery.preset_from_toml(path.read_text(encoding="utf-8"), stage)
            for path in sorted((tmp_path / folder).glob("*.toml"))
        ]

    # Coverage is checked against a *clean* registry: `reg` also holds the code
    # formats the examples above registered, which are not preset engines.
    builtin = default_registry()
    pixel_examples = toml_examples("pixel", Stage.INTERPRET_PIXEL)
    assert {p.engine_id for p in pixel_examples} == {
        plugin.info.id for plugin in builtin.plugins(Stage.INTERPRET_PIXEL)
    }
    for preset in pixel_examples:
        engine = reg.plugin(Stage.INTERPRET_PIXEL, preset.engine_id)
        data = bytes(
            (i * 61 + 7) & 0xFF for i in range(engine.bytes_per_tile(preset.params))
        )
        again = engine.encode(
            engine.decode(data, preset.params, ctx), preset.params, ctx
        )
        if preset.engine_id == "codec.pixel.direct-color":
            # Fewer than 8 bits a channel is lossy, so only the decoded value
            # round-trips — the raw bits cannot, and never could.
            assert engine.decode(again, preset.params, ctx) == engine.decode(
                data, preset.params, ctx
            )
        else:
            assert again == data
    # The code format too. It is sub-8bpp, so its grid holds four indices per
    # stored byte — the unpack the example exists to show, and the one thing a
    # code pixel format gets wrong by handing IndexGrid the raw slice.
    assert pixel_round_trips("format.pixel.example-2bpp", {})
    packed2 = reg.plugin(Stage.INTERPRET_PIXEL, "format.pixel.example-2bpp")
    tile = packed2.decode(bytes(16), {}, ctx)[0]
    assert (tile.width, tile.height, len(tile.data)) == (8, 8, 64)

    palette_examples = toml_examples("palette", Stage.INTERPRET_PALETTE)
    assert {p.engine_id for p in palette_examples} == {
        plugin.info.id for plugin in builtin.plugins(Stage.INTERPRET_PALETTE)
    }
    for preset in palette_examples:
        engine = reg.plugin(Stage.INTERPRET_PALETTE, preset.engine_id)
        size = engine.bytes_per_entry(preset.params)
        # An indexed preset's bytes are table indices, so keep them in range;
        # a mask preset takes any byte.
        limit = len(preset.params.get("colors", ())) or 256
        data = bytes(i % limit for i in range(size * 8))
        pal = engine.decode(data, preset.params, ctx)
        assert len(pal) == 8
        # Idempotent rather than byte-exact: the sub-8-bit mask formats do not
        # preserve unused bits, but the colour they decode to must survive.
        assert (
            engine.decode(engine.encode(pal, preset.params, ctx), preset.params, ctx)
            == pal
        )
    # The gray ramp only preserves the top nibble, so feed bytes whose low nibble
    # is already zero for an exact round-trip.
    assert palette_round_trips(
        "format.palette.example-gray4", {}, bytes([0x00, 0x40, 0xF0])
    )

    tilemap_examples = toml_examples("tilemap", Stage.INTERPRET_TILEMAP)
    assert {p.engine_id for p in tilemap_examples} == {
        plugin.info.id for plugin in builtin.plugins(Stage.INTERPRET_TILEMAP)
    }
    for preset in tilemap_examples:
        engine = reg.plugin(Stage.INTERPRET_TILEMAP, preset.engine_id)
        data = bytes(
            (i * 61 + 7) & 0xFF for i in range(engine.bytes_per_cell(preset.params) * 4)
        )
        assert (
            engine.encode(engine.decode(data, preset.params, ctx), preset.params, ctx)
            == data
        )
    # The code format too: a cell whose fields straddle bytes is exactly what the
    # engines cannot express, so its round trip is the one most worth checking.
    split = reg.plugin(Stage.INTERPRET_TILEMAP, "format.tilemap.example-split")
    raw = bytes([0x34, 0x3D, 0x0E, 0xFF, 0x00, 0x03])
    cells = split.decode(raw, {}, ctx)
    # Named field by field, not just round-tripped: a field the example forgets
    # to read decodes as its default and encodes back as that default, so the
    # round trip still passes while the file loses the bits on the next save.
    assert (cells[0].index, cells[0].palette_row, cells[0].priority) == (0x234, 7, 1)
    assert (cells[0].flip_h, cells[0].flip_v, cells[0].flags) == (True, False, 3)
    assert split.encode(cells, {}, ctx) == raw
    # Its optional methods have to reach the *engine* surface, params and all —
    # that forwarding is what makes an example's cells editable at all. A format
    # whose index_limit never arrives leaves the cell reference unsettable and
    # every flip refused, with nothing shown to say why.
    assert split.index_limit({}) == 0x3FF
    assert split.palette_row_limit({}) == 0x07
    assert split.has_palette_rows({}) is True
    assert split.transform_cell(Cell(index=1), CellOp.FLIP_H, {}).flip_h is True
    assert split.transform_cell(Cell(index=1), CellOp.ROTATE_CW, {}) is None
    # NES-custom code format (no companion .pal, so its baked master palette is
    # used): index bytes whose colors are unique in that table, so nearest-color
    # encode maps each straight back to the index it came from.
    assert palette_round_trips(
        "format.palette.nes-custom", {}, bytes([0x00, 0x11, 0x16, 0x18, 0x2A])
    )

    # Compression example: compress → decompress restores the bytes, and the
    # decoder reports the packed structure's true length + completeness via ctx.
    dec = reg.plugin(Stage.COMPRESSION, "compression.example-rle")
    raw = b"AAAAABBBC" + bytes([0x07]) * 300  # runs (some > 255) plus a literal tail
    packed = dec.compress(raw, ctx)
    assert dec.decompress(packed, ctx) == raw
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is True
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(packed)
    # Strict unless asked otherwise: bytes with no terminator are not a
    # structure, so Scan (which reads a successful decode as a hit) can tell
    # them apart. The lenient path is opt-in via KEY_DECOMPRESS_PARTIAL.
    cut = packed[: len(packed) // 2]
    with pytest.raises(ValueError):
        dec.decompress(cut, PipelineContext())
    lenient = PipelineContext()
    lenient.set(KEY_DECOMPRESS_PARTIAL, True)
    assert raw.startswith(dec.decompress(cut, lenient))
    assert lenient.get(KEY_DECOMPRESS_COMPLETE) is False

    # Reshape example: reshape → unshape restores the bytes at every parity,
    # including the odd tail byte the example deliberately passes through.
    swap = reg.plugin(Stage.RESHAPE, "reshape.example-swap-halves")
    for data in (b"", b"Z", b"frontback", b"frontback!"):
        assert swap.unshape(swap.reshape(data, ctx), ctx) == data
    assert swap.reshape(b"aabb", ctx) == b"bbaa"

    # reshape/ carries one TOML example per engine too — engine_id there is a
    # discriminator picking the adapter, so the two produce different classes.
    # Asserted per example rather than by counting instances: the shipped
    # bit-order tables are data-LUTs as well (builtins.register_builtins).
    assert len(discovery.RESHAPE_ENGINES) == 2
    assert isinstance(reg.plugin(Stage.RESHAPE, "reshape.gaelco-16x16"), BitswapReshape)
    assert isinstance(reg.plugin(Stage.RESHAPE, "reshape.nmk-bg"), DataLutReshape)

    # Bitswap preset example: the TOML registers as an ordinary reshape plugin.
    # It carries a real table — Gaelco's Modular System 16x16 tile scramble,
    # the only one celPix ships anywhere — so this checks it against the MAME
    # driver's bitswap<20> rather than against a toy the example alone defines.
    bs = reg.plugin(Stage.RESHAPE, "reshape.gaelco-16x16")
    bits = [19, 18, 17, 16, 15, 12, 11, 10, 9, 8, 7, 6, 5, 14, 13, 4, 3, 2, 1, 0]
    block = bytes(range(256)) * 4096  # exactly one 1 MiB block
    out = bs.reshape(block, ctx)
    for i in (0, 1 << 5, 1 << 13, 1 << 14, (1 << 19) | 0x1F, 0xABCDE):
        # out[bitswap(i)] == in[i]: the scatter direction the driver writes.
        assert (
            out[sum(((i >> s) & 1) << (19 - k) for k, s in enumerate(bits))] == block[i]
        )
    assert bs.unshape(out, ctx) == block

    # Data-LUT example: the value-side engine, dispatched from the same folder
    # by its engine_id. Its table set is NMK's decode_data_bg, so this checks it
    # against that driver's loop — three address bits pick one of eight
    # permutations, which is the part a constant-table engine could not do.
    nmk = reg.plugin(Stage.RESHAPE, "reshape.nmk-bg")
    bg = [
        [3, 0, 7, 2, 5, 1, 4, 6], [1, 2, 6, 5, 4, 0, 3, 7],
        [7, 6, 5, 4, 3, 2, 1, 0], [7, 6, 5, 0, 1, 4, 3, 2],
        [2, 0, 1, 4, 3, 5, 7, 6], [5, 3, 7, 0, 4, 6, 2, 1],
        [2, 7, 0, 6, 5, 3, 1, 4], [3, 4, 7, 6, 2, 0, 5, 1],
    ]  # fmt: skip
    data = bytes((i * 61 + 7) & 0xFF for i in range(1 << 19))
    out = nmk.reshape(data, ctx)
    # Spot-checked rather than fully reproduced: a byte-at-a-time reference over
    # half a megabyte costs more than the whole rest of this test. These offsets
    # hit all eight selector values, which is what the table pick has to get right.
    for i in (0, 4, 0x800, 0x804, 0x40000, 0x40004, 0x40800, 0x40804, 0x7FFFF):
        s = ((i & 4) >> 2) | ((i & 0x800) >> 10) | ((i & 0x40000) >> 16)
        assert out[i] == sum(((data[i] >> bg[s][7 - k]) & 1) << k for k in range(8))
    assert nmk.unshape(out, ctx) == data

    # Container example: write wraps the payload in its magic; read strips it back.
    example = reg.plugin(Stage.CONTAINER, "container.example")
    payload = b"tile-bytes-here"
    blob = example.write(payload, WriteTarget(b""), ctx)
    assert blob.startswith(b"CELPIXEX")
    assert example.read(ReadSource(blob), ctx) == payload
    # A bounded write target is spliced rather than replacing the file — the
    # WriteTarget case a container is most likely to get wrong.
    assert (
        example.write(b"XX", WriteTarget(blob, offset=9, length=2), ctx)
        == _MAGIC + b"tXXe-bytes-here"
    )

    # TIFF example: the strips are found through the directory rather than at a
    # fixed offset, and are non-contiguous — so a read that joins them and a
    # write that scatters them back is the whole of what it has to get right.
    tiff_plugin = reg.plugin(Stage.CONTAINER, "container.tiff")
    tiff = _two_strip_tiff(b"AAAA", b"BBBB")
    assert tiff_plugin.read(ReadSource(tiff), ctx) == b"AAAABBBB"
    edited = tiff_plugin.write(b"12345678", WriteTarget(tiff), ctx)
    assert tiff_plugin.read(ReadSource(edited), ctx) == b"12345678"
    assert edited == _two_strip_tiff(b"1234", b"5678")
    # The strips are fixed slots, so a wrong-sized result is refused outright
    # rather than written into part of the image.
    with pytest.raises(ValueError):
        tiff_plugin.write(b"short", WriteTarget(tiff), ctx)
    # Save As: an *empty* destination, where the strip map cannot be looked up
    # because the file holding it is the one being created. The read stashes the
    # image it came from so the write copies that — without it this raised, and a
    # container that instead wrote the strips alone would emit a pixel run that is
    # not a TIFF. Every container has this case and it is the easiest to miss.
    fresh_ctx = PipelineContext()
    assert tiff_plugin.read(ReadSource(tiff), fresh_ctx) == b"AAAABBBB"
    copied = tiff_plugin.write(b"12345678", WriteTarget(b""), fresh_ctx)
    assert copied == _two_strip_tiff(b"1234", b"5678")
    assert tiff_plugin.read(ReadSource(copied), PipelineContext()) == b"12345678"
    # ...and with nothing read on this pathway there is genuinely nothing to copy,
    # which is refused rather than guessed at.
    with pytest.raises(ValueError):
        tiff_plugin.write(b"12345678", WriteTarget(b""), PipelineContext())

    # Both containers describe themselves for the container-info popup. Optional
    # and display-only, so what matters is that the rows exist and every one
    # explains itself — an unexplained row is the failure mode here.
    for plugin, source in (
        (example, ReadSource(blob)),
        (tiff_plugin, ReadSource(tiff)),
    ):
        rows = plugin.describe(source, ctx)
        assert rows and all(f.name and f.value and f.detail for f in rows)


def test_example_presets_name_shipped_presets_that_exist(tmp_path) -> None:
    """Each example TOML lists the shipped presets built on its engine, to point
    a reader at a real one to copy. Those names are prose and drift silently — a
    preset renamed or retired leaves the example naming something that is not
    there, which is worse than naming nothing.
    """
    discovery.seed_examples(str(tmp_path))
    known = {preset.id.rsplit(".", 1)[-1] for preset in default_registry().presets()}

    named: dict[str, set[str]] = {}
    for path in sorted(tmp_path.rglob("_*.toml")):
        listing = False
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# SHIPPED PRESETS"):
                listing = True
                continue
            if not listing:
                continue
            # The block is `#   name  description`, ends at the first line that
            # is not one, and its rules and continuation lines are neither.
            body = line[1:].strip() if line.startswith("#") else ""
            if not line.startswith("#") or not body:
                break
            if line.startswith("#   ") and not line.startswith("#     "):
                first = body.split()[0]
                if not first.startswith("-"):
                    # Keyed by path, not name: pixel/ and tilemap/ both carry a
                    # _packed.toml, and merging their two lists would report a
                    # failure against the wrong file.
                    named.setdefault(path.relative_to(tmp_path).as_posix(), set()).add(
                        first
                    )

    # Every example in the three format folders has to carry a parsed block, or
    # one that quietly stops writing the list opts out of this check instead of
    # being caught by it — which is also what guards the parse itself against the
    # day the comment format changes. The other two folders are out because their
    # block is prose rather than a list: `reshape/` ships no preset built on
    # either engine (the shipped tables are plugins in their own right), and
    # `alphabet/`'s one engine reads a table rather than heading a tier.
    expected = {
        path.relative_to(tmp_path).as_posix()
        for folder in ("palette", "pixel", "tilemap")
        for path in (tmp_path / folder).glob("_*.toml")
    }
    assert set(named) == expected

    missing = {
        name: sorted(found - known) for name, found in named.items() if found - known
    }
    assert not missing, f"examples name presets that do not exist: {missing}"


def test_wrong_shaped_format_is_reported(tmp_path) -> None:
    # A palette-shaped format (no tile geometry) dropped in pixel/ must be a load
    # issue, not a decode-time crash.
    palette_shaped = """
from celpix.plugins import FormatInfo


class NoGeometry:
    info = FormatInfo(id="format.pixel.nogeo", name="No geometry")

    def decode(self, data, ctx):
        return None

    def encode(self, palette, ctx):
        return b""


def register(registry):
    registry.register_format(NoGeometry())
"""
    _drop(tmp_path, "pixel", "nogeo.py", palette_shaped)
    reg = default_registry()

    issues = discovery.load_directory(reg, str(tmp_path), confirm=_ALLOW)
    assert len(issues) == 1
    assert "missing" in issues[0].message
    with pytest.raises(KeyError):
        reg.preset("format.pixel.nogeo")


def test_palette_format_without_entry_size_is_reported(tmp_path) -> None:
    # The host sizes palette reads via bytes_per_entry; a palette format without
    # it must be a load issue, not a failure when the feature is first used.
    incomplete = """
from celpix.core.palette import Palette
from celpix.plugins import FormatInfo


class NoEntrySize:
    info = FormatInfo(id="format.palette.nosize", name="No entry size")

    def decode(self, data, ctx):
        return Palette([])

    def encode(self, palette, ctx):
        return b""


def register(registry):
    registry.register_format(NoEntrySize())
"""
    _drop(tmp_path, "palette", "nosize.py", incomplete)
    reg = default_registry()

    issues = discovery.load_directory(reg, str(tmp_path), confirm=_ALLOW)
    assert len(issues) == 1
    assert "bytes_per_entry" in issues[0].message
    with pytest.raises(KeyError):
        reg.preset("format.palette.nosize")
