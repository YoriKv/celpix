"""Compress & Reshape: a compression scheme with a reshape run over its output.

What carries the risk is the *order* the two halves run in, in each direction, and
the facts the host reads off the plugin object before any entry opens — whether it
saves, what it needs bound. Both are decided at load, from a preset file, so the
tests go through discovery rather than constructing the class by hand.
"""

from __future__ import annotations

import pytest

from celpix.core.context import KEY_COMPRESSED_SIZE, PipelineContext
from celpix.core.errors import Stage
from celpix.plugins import discovery
from celpix.plugins.compress_reshape import write_preset
from celpix.plugins.registry import default_registry

# A compression scheme with no `compress`: what a pair must inherit view-only from.
_READ_ONLY_CODEC = """
from celpix.plugins import PluginInfo


class ReadOnly:
    info = PluginInfo(id="compression.read-only", name="Read only")

    def decompress(self, data, ctx):
        return data


def register(registry):
    registry.register(ReadOnly())
"""

# An XOR with $FF, as a data-LUT preset: a reshape that lives in the same plugin
# root as the pair naming it, in a folder that sorts *after* compression/.
_INVERT = """
id = "reshape.invert"
name = "Invert"
engine_id = "reshape.data-lut"

[params]
luts = [[{table}]]
""".format(table=", ".join(str(255 - i) for i in range(256)))


def _load(root):
    reg = default_registry()
    issues = discovery.load_directory(reg, str(root), confirm=lambda pending: True)
    return reg, issues


def test_running_sum_is_the_games_loop_and_wraps() -> None:
    reg = default_registry()
    plugin = reg.plugin(Stage.RESHAPE, "reshape.running-sum")
    ctx = PipelineContext()
    # $40 then three steps of 1, then a step that carries past $FF.
    assert plugin.reshape(bytes([0x40, 1, 1, 1, 0xF0]), ctx) == bytes(
        [0x40, 0x41, 0x42, 0x43, 0x33]
    )
    for data in (b"", b"\x07", bytes(range(256)) * 2, b"\xff\x00\xff\x01"):
        assert plugin.unshape(plugin.reshape(data, ctx), ctx) == data
        assert plugin.reshape(plugin.unshape(data, ctx), ctx) == data


def test_pair_decompresses_then_reshapes_and_saves_in_reverse(tmp_path) -> None:
    write_preset(
        tmp_path, "compression.pair", "Pair", "compression.rnc2", "reshape.running-sum"
    )
    reg, issues = _load(tmp_path)
    assert issues == []
    pair = reg.plugin(Stage.COMPRESSION, "compression.pair")
    rnc = reg.plugin(Stage.COMPRESSION, "compression.rnc2")
    total = reg.plugin(Stage.RESHAPE, "reshape.running-sum")

    # Consecutive tile numbers: the picture the pairing exists for.
    data = bytes(range(0x40, 0x80)) * 9
    ctx = PipelineContext()
    packed = pair.compress(data, ctx)
    # The stream holds the *differences*, so the scheme alone reads them back...
    assert rnc.decompress(packed, PipelineContext()) == total.unshape(data, ctx)
    # ...and the pair reads the picture, reporting the scheme's own extent.
    read = PipelineContext()
    assert pair.decompress(packed, read) == data
    assert read.get(KEY_COMPRESSED_SIZE) == len(packed)
    # Which is why it is worth doing at all.
    assert len(packed) < len(rnc.compress(data, PipelineContext()))


def test_pair_may_name_plugins_dropped_beside_it(tmp_path) -> None:
    # Both members come from this root, and both load *after* the pair would in
    # scan order: reshape/ sorts behind compression/, and a .py behind a .toml.
    (tmp_path / "reshape").mkdir()
    (tmp_path / "reshape" / "invert.toml").write_text(_INVERT, encoding="utf-8")
    (tmp_path / "compression").mkdir()
    (tmp_path / "compression" / "zz_codec.py").write_text(
        _READ_ONLY_CODEC, encoding="utf-8"
    )
    write_preset(
        tmp_path, "compression.pair", "Pair", "compression.read-only", "reshape.invert"
    )
    reg, issues = _load(tmp_path)
    assert issues == []
    pair = reg.plugin(Stage.COMPRESSION, "compression.pair")
    assert pair.decompress(b"\x00\x0f", PipelineContext()) == b"\xff\xf0"
    # A half that cannot run backwards makes the pair view-only up front, by the
    # one test the host applies to every plugin.
    assert reg.resolve_stage(Stage.COMPRESSION, "compression.pair") == (
        "compression.pair",
        False,
    )


def test_pair_declares_its_compression_halfs_inputs(tmp_path) -> None:
    write_preset(
        tmp_path,
        "compression.pair",
        "Pair",
        "compression.phantasy-star-rle",
        "reshape.running-sum",
    )
    reg, issues = _load(tmp_path)
    assert issues == []
    member = reg.plugin(Stage.COMPRESSION, "compression.phantasy-star-rle")
    assert member.info.inputs  # or this test has stopped testing anything
    pair = reg.plugin(Stage.COMPRESSION, "compression.pair")
    assert pair.info.inputs == member.info.inputs


@pytest.mark.parametrize(
    ("compression_id", "reshape_id", "said"),
    [
        ("compression.none", "reshape.running-sum", "pass-through"),
        ("compression.rnc2", "reshape.none", "pass-through"),
        ("compression.rnc2", "reshape.no-such", "no reshape plugin"),
        ("compression.first", "reshape.running-sum", "itself a Compress & Reshape"),
    ],
)
def test_pair_refuses_members_that_make_no_pair(
    tmp_path, compression_id, reshape_id, said
) -> None:
    # `first` sorts ahead of `second`, so the nested case finds it registered.
    write_preset(
        tmp_path,
        "compression.first",
        "First",
        "compression.rnc2",
        "reshape.running-sum",
    )
    write_preset(tmp_path, "compression.second", "Second", compression_id, reshape_id)
    reg, issues = _load(tmp_path)
    assert [issue.path.endswith("second.toml") for issue in issues] == [True]
    assert said in issues[0].message
    with pytest.raises(KeyError):
        reg.plugin(Stage.COMPRESSION, "compression.second")


def test_written_preset_survives_its_own_name_and_is_never_overwritten(
    tmp_path,
) -> None:
    name = 'RNC "2" \\ sum — naïve'
    path = write_preset(
        tmp_path, "compression.pair", name, "compression.rnc2", "reshape.running-sum"
    )
    reg, issues = _load(tmp_path)
    assert issues == []
    assert reg.plugin(Stage.COMPRESSION, "compression.pair").info.name == name
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_preset(tmp_path, "compression.pair", "Other", "compression.rnc1", "x")
    assert path.read_bytes() == before
