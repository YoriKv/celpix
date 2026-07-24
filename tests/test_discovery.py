"""User plugin discovery: drop a file into a typed subfolder and it loads."""

from __future__ import annotations

import pytest

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins import discovery
from celpix.plugins.base import FileRef
from celpix.plugins.registry import default_registry
from celpix.plugins.trust import TrustStore

# Auto-approve confirm callback for tests that aren't exercising the gate itself.
_ALLOW = lambda pending: True  # noqa: E731

# A minimal code plugin: a Read plugin plus the register() hook the host calls.
# Belongs in containers/ (read + write handlers).
_CODE_PLUGIN = """
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo


class HelloReader:
    info = PluginInfo(id="read.hello", name="Hello reader", stage=Stage.READ)

    def read(self, source, ctx):
        return b"hello"


def register(registry):
    registry.register(HelloReader())
"""

# A zero-code preset: a new 1bpp planar format for the built-in planar engine.
# No stage field — the pixel/ folder it is dropped into determines the stage.
_PRESET = """
id = "preset.pixel.custom-1bpp"
name = "Custom 1bpp"
engine_id = "codec.planar"

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

# A compression scheme's two halves registered from one file (the pair case).
_PAIR_PLUGIN = """
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo


class Doubler:
    info = PluginInfo(id="decompress.double", name="Doubler", stage=Stage.DECOMPRESS)

    def decompress(self, data, ctx):
        return data + data


class Halver:
    info = PluginInfo(id="compress.halve", name="Halver", stage=Stage.COMPRESS)

    def compress(self, data, ctx):
        return data[: len(data) // 2]


def register(registry):
    registry.register(Doubler())
    registry.register(Halver())
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

    plugin = reg.plugin(Stage.READ, "read.hello")
    assert plugin.read(None, PipelineContext()) == b"hello"


def test_code_plugin_skipped_when_not_approved(tmp_path) -> None:
    _drop(tmp_path, "containers", "hello.py", _CODE_PLUGIN)
    reg = default_registry()

    # No confirm callback and nothing trusted -> default deny.
    issues = discovery.load_directory(reg, str(tmp_path))
    assert len(issues) == 1
    assert "not approved" in issues[0].message
    with pytest.raises(KeyError):
        reg.plugin(Stage.READ, "read.hello")


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
    assert reg1.plugin(Stage.READ, "read.hello")

    # Second load: deny everything, but the trusted hash loads silently.
    deny = lambda pending: False  # noqa: E731
    reg2 = default_registry()
    issues2 = discovery.load_directory(reg2, str(plugdir), trust=trust, confirm=deny)
    assert issues2 == []
    assert reg2.plugin(Stage.READ, "read.hello")


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
        reg.plugin(Stage.READ, "read.hello")


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
    assert reg.plugin(Stage.READ, "read.hello")


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


def test_missing_directory_is_silent(tmp_path) -> None:
    reg = default_registry()
    assert discovery.load_directory(reg, str(tmp_path / "does-not-exist")) == []


# -- the typed layout's own guarantees ------------------------------------------


def test_stage_mismatch_is_reported_and_pair_still_loads(tmp_path) -> None:
    # A READ plugin in pixel/ is out of scope: reported, not registered.
    _drop(tmp_path, "pixel", "misplaced.py", _CODE_PLUGIN)
    # A compression scheme registering both halves from one file loads both.
    _drop(tmp_path, "compression", "scheme.py", _PAIR_PLUGIN)
    reg = default_registry()

    issues = discovery.load_directory(reg, str(tmp_path), confirm=_ALLOW)
    assert len(issues) == 1
    assert "not allowed in folder 'pixel/'" in issues[0].message
    with pytest.raises(KeyError):
        reg.plugin(Stage.READ, "read.hello")
    assert reg.plugin(Stage.DECOMPRESS, "decompress.double")
    assert reg.plugin(Stage.COMPRESS, "compress.halve")


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
    assert "pixel/palette only" in issues[0].message


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
    # Seeding lays down _example.* reference files (never overwriting), and each
    # must actually work once renamed — examples drifting from the real schema
    # or format contract is exactly the regression this guards.
    for sub in discovery.FOLDER_STAGES:
        (tmp_path / sub).mkdir()
    discovery.seed_examples(str(tmp_path))

    seeded = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("_*"))
    assert seeded == [
        "compression/_example.py",
        "containers/_example.py",
        "palette/_example.py",
        "palette/_example.toml",
        "palette/_nes-custom.py",
        "pixel/_example.py",
        "pixel/_example.toml",
    ]

    # Re-seeding must not clobber a user's edits.
    marker = "# user edit\n"
    edited = tmp_path / "pixel" / "_example.toml"
    edited.write_text(marker, encoding="utf-8")
    discovery.seed_examples(str(tmp_path))
    assert edited.read_text(encoding="utf-8") == marker
    # Drop the marker and re-seed so the shipped pixel preset is restored — it,
    # too, gets decoded below (activating the marker file would just be ignored).
    edited.unlink()
    discovery.seed_examples(str(tmp_path))

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

    pixel_preset = reg.preset("preset.pixel.example-2bpp")
    assert pixel_round_trips(pixel_preset.engine_id, pixel_preset.params)
    assert pixel_round_trips("format.pixel.example-4x4", {})  # code format

    palette_preset = reg.preset("preset.palette.example-rgb555")
    # Two BGR555 entries, little-endian; exact bytes so encode must reproduce them.
    assert palette_round_trips(
        palette_preset.engine_id, palette_preset.params, bytes([0x1F, 0x7C, 0xE0, 0x03])
    )
    # The gray ramp only preserves the top nibble, so feed bytes whose low nibble
    # is already zero for an exact round-trip.
    assert palette_round_trips(
        "format.palette.example-gray4", {}, bytes([0x00, 0x40, 0xF0])
    )
    # NES-custom code format (no companion .pal, so its baked master palette is
    # used): index bytes whose colors are unique in that table, so nearest-color
    # encode maps each straight back to the index it came from.
    assert palette_round_trips(
        "format.palette.nes-custom", {}, bytes([0x00, 0x11, 0x16, 0x18, 0x2A])
    )

    # Compression example: compress → decompress restores the bytes, and the
    # decoder reports the packed structure's true length + completeness via ctx.
    comp = reg.plugin(Stage.COMPRESS, "compress.example-rle")
    dec = reg.plugin(Stage.DECOMPRESS, "decompress.example-rle")
    raw = b"AAAAABBBC" + bytes([0x07]) * 300  # runs (some > 255) plus a literal tail
    packed = comp.compress(raw, ctx)
    assert dec.decompress(packed, ctx) == raw
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is True
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(packed)

    # Container example: write wraps the payload in its magic; read strips it back.
    writer = reg.plugin(Stage.WRITE, "write.example-container")
    reader = reg.plugin(Stage.READ, "read.example-container")
    payload = b"tile-bytes-here"
    blob = tmp_path / "blob.bin"
    writer.write(payload, FileRef(str(blob)), ctx)
    assert blob.read_bytes().startswith(b"CELPIXEX")
    assert reader.read(FileRef(str(blob)), ctx) == payload


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
