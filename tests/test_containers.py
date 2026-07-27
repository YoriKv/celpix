"""Container plugins (iNES / .smd / SNES interleave) and how one is picked."""

from __future__ import annotations

import pytest

from celpix.core.context import KEY_SOURCE_OFFSET, PipelineContext
from celpix.core.errors import Stage
from celpix.core.notices import (
    KEY_NOTICES,
    NoticeLevel,
    inform,
    notices,
    warn,
)
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import (
    RAW_CONTAINER,
    FileRef,
    PluginInfo,
    ReadSource,
    WriteTarget,
)
from celpix.plugins.builtins.containers import (
    CopierHeaderContainer,
    INesContainer,
    SmdContainer,
    SnesInterleavedContainer,
)
from celpix.plugins.builtins.gb_rom import GbRomContainer, repair_checksums
from celpix.plugins.builtins.n64_rom import (
    KEY_N64_SWAP,
    N64RomContainer,
    swap_groups,
)
from celpix.plugins.detect import (
    container_write_enabled,
    detect_container,
    resolved_container_id,
)
from celpix.plugins.registry import default_registry
from celpix.project.workspace import Entry, EntryKind, new_slice, pixel_config_for


def test_ines_skips_header_to_chr() -> None:
    chr_rom = bytes((i * 7) & 0xFF for i in range(8192))  # 1 CHR bank
    prg = bytes(16384)  # 1 PRG bank
    header = bytes([*b"NES\x1a", 1, 1, 0, 0]) + bytes(8)  # PRG=1, CHR=1, no trainer

    ctx = PipelineContext()
    data = INesContainer().read(ReadSource(header + prg + chr_rom), ctx)
    assert data == chr_rom
    assert ctx.get(KEY_SOURCE_OFFSET) == 16 + 16384  # header + PRG


def test_ines_non_ines_reads_whole_file() -> None:
    plain = b"\x01\x02\x03\x04not-a-nes"
    assert INesContainer().read(ReadSource(plain), PipelineContext()) == plain


def test_smd_deinterleaves() -> None:
    # Build a known deinterleaved 16 KB block, interleave it into .smd layout, and
    # confirm the reader reconstructs the original.
    block = bytes((i * 5 + 1) & 0xFF for i in range(16384))
    odd = bytes(block[j] for j in range(1, 16384, 2))  # odd positions -> first half
    even = bytes(block[j] for j in range(0, 16384, 2))  # even positions -> second half

    # 512-byte header + interleaved block.
    smd = ReadSource(bytes(512) + odd + even)
    assert SmdContainer().read(smd, PipelineContext()) == block


def test_snes_interleaved_restores_bank_order() -> None:
    # Two 64 KB HiROM banks. Interleaved layout stores every bank's upper 32 KB
    # half first, then all the lower halves (the header trick that puts $FFC0 at
    # file offset 0x7FC0); the reader must reassemble lower+upper per bank.
    halves = [bytes([n]) * 0x8000 for n in range(4)]  # bank0 = 0,1; bank1 = 2,3

    ctx = PipelineContext()
    interleaved = ReadSource(halves[1] + halves[3] + halves[0] + halves[2])
    data = SnesInterleavedContainer().read(interleaved, ctx)
    assert data == halves[0] + halves[1] + halves[2] + halves[3]
    assert ctx.get(KEY_SOURCE_OFFSET) == 0


def test_snes_interleaved_skips_copier_header_by_size() -> None:
    # size % 1024 == 512 marks a 512-byte copier header (carts are whole KiB).
    upper, lower = b"\x01" * 0x8000, b"\x00" * 0x8000

    ctx = PipelineContext()
    data = SnesInterleavedContainer().read(ReadSource(bytes(512) + upper + lower), ctx)
    assert data == lower + upper
    assert ctx.get(KEY_SOURCE_OFFSET) == 512


# Konami RLE decoding (fill/literal/terminator, the PPU-address-change desync
# guard, and plugin context recording) lives in the dedicated, far richer suite
# in test_compression.py — the container-reader file stays focused on readers.


def test_detection_prefers_magic_and_falls_back_to_raw() -> None:
    # The three outcomes that matter: magic claims a file whatever it is named,
    # a magic-declaring container refuses a file whose bytes disagree even when
    # the suffix matches, and anything unclaimed lands on plain bytes.
    reg = default_registry()
    assert detect_container(reg, "cart.nes", b"NES\x1a\x01\x01") == "container.ines"
    assert detect_container(reg, "cart.bin", b"NES\x1a\x01\x01") == "container.ines"
    assert detect_container(reg, "cart.nes", b"not-an-ines-header") == RAW_CONTAINER
    # No magic to assert on, so .smd is claimed on its name alone.
    assert detect_container(reg, "cart.smd", b"\x00" * 16) == "container.smd"
    assert detect_container(reg, "cart.bin", b"\x00" * 16) == RAW_CONTAINER
    # Never auto-detected: deinterleaving a plain image would scramble it.
    assert (
        detect_container(reg, "cart.sfc", b"\x00" * 16) != "container.snes-interleaved"
    )


def test_copier_header_needs_both_a_suffix_and_a_plausible_size(tmp_path) -> None:
    """The size terms narrow a match; they never make one on their own.

    A 512-byte copier header has no marker, so the only giveaway is the file
    being 512 over a whole number of KiB. That arithmetic alone is far too eager:
    it also fits the small ``*.4bpp.sfc`` tile sheets celPix opens all the time,
    and claiming one hands back an empty document.
    """
    reg = default_registry()

    def detected(name: str, size: int) -> str:
        f = tmp_path / name
        f.write_bytes(bytes(size))
        return detect_container(reg, str(f))

    assert detected("rom.sfc", 512 + 0x8000) == "container.copier-header"
    assert detected("rom.smc", 512 + 0x200000) == "container.copier-header"
    assert detected("rom.sfc", 0x200000) == RAW_CONTAINER  # whole KiB: no header
    assert detected("rom.bin", 512 + 0x8000) == RAW_CONTAINER  # right size, no suffix
    # The floor: both of these fit the modulo rule and neither is a headered cart.
    assert detected("tiles.4bpp.sfc", 512) == RAW_CONTAINER
    assert detected("tiles.4bpp.sfc", 512 + 1024) == RAW_CONTAINER
    # A .smd is 512-over too, and must stay with its own container.
    assert detected("rom.smd", 512 + 16384) == "container.smd"


def test_detection_reads_the_head_off_disk(tmp_path) -> None:
    # The head argument is an optimisation, not a requirement — callers that
    # have not read the file hand over the path alone.
    f = tmp_path / "cart.nes"
    f.write_bytes(b"NES\x1a" + bytes(64))
    assert detect_container(default_registry(), str(f)) == "container.ines"
    assert (
        detect_container(default_registry(), str(tmp_path / "gone.nes"))
        == RAW_CONTAINER
    )


def _noise(n: int, seed: int = 0) -> bytes:
    return bytes(((i * 7 + seed) & 0xFF) for i in range(n))


# One plausible file per shipped container. Each must be something its reader
# actually claims, since the point is to exercise the real read→write pair.
_CONTAINER_SAMPLES = {
    RAW_CONTAINER: ("f.bin", _noise(4096)),
    "container.ines": (
        "f.nes",
        bytes([*b"NES\x1a", 1, 1, 0, 0])
        + bytes(8)
        + _noise(16384, 1)
        + _noise(8192, 2),
    ),
    # Past COPIER_MIN_SIZE, else the size rule correctly declines to claim it.
    "container.copier-header": ("f.sfc", _noise(512, 8) + _noise(0x8000, 2)),
    "container.smd": ("f.smd", _noise(512, 9) + _noise(16384, 3)),
    "container.snes-interleaved": ("f.sfc", _noise(512, 5) + _noise(0x20000, 4)),
    "container.gb-rom": (
        "f.gb",
        repair_checksums(
            _noise(0x104)
            + bytes([0xCE, 0xED, 0x66, 0x66, 0xCC, 0x0D, 0x00, 0x0B])
            + _noise(0x8000 - 0x10C, 6)
        ),
    ),
    "container.n64-rom": ("f.v64", b"\x37\x80\x40\x12" + _noise(0x2000 - 4, 7)),
}


def _container_config(reg, path, container_id):
    return PathwayConfig(
        source=FileRef(str(path), offset=0),  # the host's header box, unticked
        interpret_preset_id="preset.pixel.chunky-8bpp",
        container_id=resolved_container_id(reg, container_id),
        write_enabled=container_write_enabled(reg, container_id),
    )


@pytest.mark.parametrize("container_id", sorted(_CONTAINER_SAMPLES))
def test_saving_an_unedited_file_through_its_container_changes_nothing(
    container_id, tmp_path
) -> None:
    """Load and save with no edit, through every container: the file must come
    back byte-identical.

    The check that a container's ``write`` really is its ``read``'s inverse. It is
    not implied by the read's own tests: a container can unwrap perfectly and
    still put the bytes back somewhere else, which silently destroys the file on
    the user's first save rather than failing.
    """
    reg = default_registry()
    name, content = _CONTAINER_SAMPLES[container_id]
    path = tmp_path / name
    path.write_bytes(content)
    cfg = _container_config(reg, path, container_id)
    doc = pipeline.load(cfg, cfg, reg)
    pipeline.save(doc, reg, pixel=True, palette=False)
    assert path.read_bytes() == content


@pytest.mark.parametrize("container_id", sorted(_CONTAINER_SAMPLES))
def test_an_edit_lands_where_its_container_read_it_from(container_id, tmp_path) -> None:
    """The other half: an edited byte must read back changed, in place, without
    resizing the file — i.e. the writer's destination matches the reader's start.

    One byte in the middle rather than the whole buffer, because two containers
    legitimately touch bytes of their own on the way out: Game Boy recomputes its
    checksums, and the N64 magic decides the byte order the file is written in.
    Editing near either would test the sample, not the offset arithmetic.
    """
    reg = default_registry()
    name, content = _CONTAINER_SAMPLES[container_id]
    path = tmp_path / name
    path.write_bytes(content)
    cfg = _container_config(reg, path, container_id)

    doc = pipeline.load(cfg, cfg, reg)
    at = len(doc.pixel_data) // 2
    want = doc.pixel_data[at] ^ 0xFF
    doc.pixel_data = doc.pixel_data[:at] + bytes([want]) + doc.pixel_data[at + 1 :]
    pipeline.save(doc, reg, pixel=True, palette=False)

    assert len(path.read_bytes()) == len(content)
    assert pipeline.load(cfg, cfg, reg).pixel_data[at] == want


def test_container_id_degrades_when_the_plugin_is_gone() -> None:
    # A project names the container its files were opened with, and the plugin
    # behind it can be uninstalled or left untrusted before the next launch —
    # that must show raw bytes, not fail the load.
    reg = default_registry()
    assert resolved_container_id(reg, "container.ines") == "container.ines"
    assert resolved_container_id(reg, "container.no-such-plugin") == RAW_CONTAINER


def test_a_container_without_a_write_half_is_view_only() -> None:
    """Unwrapping a file is not something plain bytes can undo, so a container
    with no ``write`` opens read-only rather than saving through the raw fallback.

    Doubles as the check that every shipped container really does implement its
    own inverse — a missing one would silently turn that format read-only.
    """
    reg = default_registry()
    for plugin in reg.plugins(Stage.CONTAINER):
        assert container_write_enabled(reg, plugin.info.id), plugin.info.id

    class _ReadOnly:
        info = PluginInfo(id="container.probe", name="probe", stage=Stage.CONTAINER)

        def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
            return b""

    reg.register(_ReadOnly())
    assert container_write_enabled(reg, "container.probe") is False
    # An id the registry has lost reads as plain bytes so the file still opens,
    # but it is view-only for the same reason: what is on screen is not what the
    # entry means, so saving it would put the wrong bytes back.
    assert container_write_enabled(reg, "container.no-such-plugin") is False


def test_file_entry_reads_through_its_container(tmp_path) -> None:
    # The Entry -> PathwayConfig hop: a file's container reaches Read/Write,
    # while a slice of it stays on plain bytes (its offsets are its parent's).
    reg = default_registry()
    entry = Entry(name="g.nes", kind=EntryKind.FILE, path="g.nes")
    entry.container_id = "container.ines"
    cfg = pixel_config_for(entry, "preset.pixel.nes-2bpp", reg)
    assert (cfg.container_id, cfg.container_id) == ("container.ines", "container.ines")
    assert cfg.write_enabled

    sliced = new_slice("g.nes", "s", 0x10, 0x20)
    slice_cfg = pixel_config_for(sliced, "preset.pixel.nes-2bpp", reg)
    assert (slice_cfg.container_id, slice_cfg.container_id) == (
        RAW_CONTAINER,
        RAW_CONTAINER,
    )


def _gb_rom(size: int = 0x8000) -> bytearray:
    rom = bytearray(size)
    rom[0x104:0x10C] = bytes([0xCE, 0xED, 0x66, 0x66, 0xCC, 0x0D, 0x00, 0x0B])
    rom[0x134:0x144] = b"TESTCART\x00\x00\x00\x00\x00\x00\x00\x00"
    return rom


def test_gb_write_repairs_both_checksums() -> None:
    # The header sum is what the boot ROM checks (a wrong one is a blank screen
    # on hardware); the global sum is the one tile edits actually invalidate.
    rom = _gb_rom()

    edited = bytearray(rom)
    edited[0x4000:0x4010] = bytes(range(16))  # "tile edits", far from the header
    target = WriteTarget(bytes(rom))
    out = GbRomContainer().write(bytes(edited), target, PipelineContext())

    expected = 0
    for byte in out[0x134:0x14D]:
        expected = (expected - byte - 1) & 0xFF
    assert out[0x14D] == expected
    zeroed = bytearray(out)
    zeroed[0x14E:0x150] = b"\x00\x00"
    assert out[0x14E:0x150] == (sum(zeroed) & 0xFFFF).to_bytes(2, "big")
    # Only the checksums and the edit differ; nothing else was rewritten.
    assert out[0x4000:0x4010] == bytes(range(16))
    assert out[:0x14D] == bytes(rom[:0x14D])


def test_gb_write_leaves_a_headerless_file_alone() -> None:
    # Too short to hold a header: inventing one would corrupt whatever it is.
    out = GbRomContainer().write(b"\xaa" * 64, WriteTarget(b""), PipelineContext())
    assert out == b"\xaa" * 64


def test_n64_round_trips_each_byte_order() -> None:
    # Read normalises to native so offsets mean what the docs say; Write puts
    # the file back in the order it arrived in, so other tools still read it.
    native = b"\x80\x37\x12\x40" + bytes((i * 11) & 0xFF for i in range(0x400 - 4))
    for width in (0, 2, 4):
        on_disk = swap_groups(native, width)

        ctx = PipelineContext()
        assert N64RomContainer().read(ReadSource(on_disk), ctx) == native
        assert ctx.get(KEY_N64_SWAP) == width
        assert N64RomContainer().write(native, WriteTarget(on_disk), ctx) == on_disk


def test_n64_splices_a_bounded_edit_in_native_coordinates() -> None:
    # A byte's position only survives the swap within its own group, so an
    # unaligned splice has to happen in native order and be swapped back whole.
    native = bytearray(b"\x80\x37\x12\x40" + bytes(range(0x40)))
    on_disk = swap_groups(bytes(native), 2)

    ctx = PipelineContext()
    N64RomContainer().read(ReadSource(on_disk), ctx)  # arms the width
    target = WriteTarget(on_disk, offset=0x11, length=2)
    out = N64RomContainer().write(b"\xee\xff", target, ctx)

    native[0x11:0x13] = b"\xee\xff"
    assert out == swap_groups(bytes(native), 2)


def test_n64_swap_leaves_a_trailing_partial_group(tmp_path) -> None:
    # A truncated dump keeps its tail rather than losing it to a partial group.
    assert swap_groups(b"\x01\x02\x03\x04\x05", 4) == b"\x04\x03\x02\x01\x05"
    assert swap_groups(b"\x01\x02\x03", 2) == b"\x02\x01\x03"
    assert swap_groups(b"\x01\x02\x03", 0) == b"\x01\x02\x03"


# -- notices ---------------------------------------------------------------


def test_notices_accumulate_across_stages_and_survive_junk() -> None:
    """The channel is append-only across stages, and tolerant of a plugin that
    writes something else to the key — the context is an open bag, so a bad value
    must not take the reader down with it."""
    ctx = PipelineContext()
    assert notices(ctx) == ()

    warn(ctx, "first", "detail", "container.a")
    inform(ctx, "second", source="compression.b")
    got = notices(ctx)
    assert [n.summary for n in got] == ["first", "second"]  # oldest first
    assert [n.source for n in got] == ["container.a", "compression.b"]
    assert [n.is_warning for n in got] == [True, False]

    ctx.set(KEY_NOTICES, "not a tuple of notices")
    assert notices(ctx) == ()


def test_containers_report_what_they_had_to_assume() -> None:
    """Each built-in container's non-fatal compromise reaches the user.

    These are the cases that used to be silent: the read succeeds, but the bytes
    on screen are not what the user would assume, and nothing said so.
    """

    def read_notices(reader, content: bytes):
        ctx = PipelineContext()
        reader.read(ReadSource(content), ctx)
        return notices(ctx)

    # CHR-RAM: the header declares no CHR banks, so there is no tile data at all.
    chr_ram = read_notices(
        INesContainer(), bytes([*b"NES\x1a", 2, 0, 0, 0]) + bytes(8) + bytes(0x8000)
    )
    assert [n.level for n in chr_ram] == [NoticeLevel.WARNING]
    assert "CHR-RAM" in chr_ram[0].summary

    # A file the iNES reader cannot claim is read raw rather than failing.
    assert read_notices(INesContainer(), b"not-an-ines-file")[0].is_warning

    # A tail too short to deinterleave is dropped, and not written back either.
    ragged = read_notices(SmdContainer(), bytes(512) + bytes(16384) + bytes(300))
    assert "300" in ragged[0].summary

    # Hand-picking the copier container on a file that is not 512-over strips
    # 512 bytes of real image.
    off = read_notices(CopierHeaderContainer(), bytes(0x8000))
    assert off and off[0].is_warning

    # A clean read says nothing at all.
    clean = bytes([*b"NES\x1a", 1, 1, 0, 0]) + bytes(8) + bytes(16384) + bytes(8192)
    assert read_notices(INesContainer(), clean) == ()


def test_a_plugin_may_record_notices_and_then_fail() -> None:
    """The context is handed in before the plugin runs, so observations made on
    the way to a hard failure survive the exception that ends the read."""
    ctx = PipelineContext()

    class _Doomed:
        info = PluginInfo(id="container.doomed", name="doomed", stage=Stage.CONTAINER)

        def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
            warn(ctx, "header looked odd", source=self.info.id)
            raise ValueError("and then it fell over")

    with pytest.raises(ValueError):
        _Doomed().read(ReadSource(b""), ctx)
    assert [n.summary for n in notices(ctx)] == ["header looked odd"]
