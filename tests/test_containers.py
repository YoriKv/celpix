"""Container Read plugins (iNES / .smd / SNES interleave) and Konami NES RLE."""

from __future__ import annotations

from celpix.core.context import KEY_SOURCE_OFFSET, PipelineContext
from celpix.plugins.base import RAW_READ, RAW_WRITE, FileRef
from celpix.plugins.builtins.container_read import (
    INesReader,
    SmdReader,
    SnesInterleavedReader,
)
from celpix.plugins.builtins.gb_rom import GbRomWriter
from celpix.plugins.builtins.n64_rom import (
    KEY_N64_SWAP,
    N64RomReader,
    N64RomWriter,
    swap_groups,
)
from celpix.plugins.detect import (
    container_ids,
    container_write_id,
    detect_container,
)
from celpix.plugins.registry import default_registry
from celpix.project.workspace import Entry, EntryKind, new_slice, pixel_config_for


def test_ines_skips_header_to_chr(tmp_path) -> None:
    chr_rom = bytes((i * 7) & 0xFF for i in range(8192))  # 1 CHR bank
    prg = bytes(16384)  # 1 PRG bank
    header = bytes([*b"NES\x1a", 1, 1, 0, 0]) + bytes(8)  # PRG=1, CHR=1, no trainer
    f = tmp_path / "game.nes"
    f.write_bytes(header + prg + chr_rom)

    ctx = PipelineContext()
    data = INesReader().read(FileRef(str(f)), ctx)
    assert data == chr_rom
    assert ctx.get(KEY_SOURCE_OFFSET) == 16 + 16384  # header + PRG


def test_ines_non_ines_reads_whole_file(tmp_path) -> None:
    f = tmp_path / "plain.bin"
    f.write_bytes(b"\x01\x02\x03\x04not-a-nes")
    data = INesReader().read(FileRef(str(f)), PipelineContext())
    assert data == b"\x01\x02\x03\x04not-a-nes"


def test_smd_deinterleaves(tmp_path) -> None:
    # Build a known deinterleaved 16 KB block, interleave it into .smd layout, and
    # confirm the reader reconstructs the original.
    block = bytes((i * 5 + 1) & 0xFF for i in range(16384))
    odd = bytes(block[j] for j in range(1, 16384, 2))  # odd positions -> first half
    even = bytes(block[j] for j in range(0, 16384, 2))  # even positions -> second half
    f = tmp_path / "rom.smd"
    f.write_bytes(bytes(512) + odd + even)  # 512-byte header + interleaved block

    data = SmdReader().read(FileRef(str(f)), PipelineContext())
    assert data == block


def test_snes_interleaved_restores_bank_order(tmp_path) -> None:
    # Two 64 KB HiROM banks. Interleaved layout stores every bank's upper 32 KB
    # half first, then all the lower halves (the header trick that puts $FFC0 at
    # file offset 0x7FC0); the reader must reassemble lower+upper per bank.
    halves = [bytes([n]) * 0x8000 for n in range(4)]  # bank0 = 0,1; bank1 = 2,3
    f = tmp_path / "rom.smc"
    f.write_bytes(halves[1] + halves[3] + halves[0] + halves[2])

    ctx = PipelineContext()
    data = SnesInterleavedReader().read(FileRef(str(f)), ctx)
    assert data == halves[0] + halves[1] + halves[2] + halves[3]
    assert ctx.get(KEY_SOURCE_OFFSET) == 0


def test_snes_interleaved_skips_copier_header_by_size(tmp_path) -> None:
    # size % 1024 == 512 marks a 512-byte copier header (carts are whole KiB).
    upper, lower = b"\x01" * 0x8000, b"\x00" * 0x8000
    f = tmp_path / "rom.swc"
    f.write_bytes(bytes(512) + upper + lower)

    ctx = PipelineContext()
    data = SnesInterleavedReader().read(FileRef(str(f)), ctx)
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
    assert detect_container(reg, "cart.nes", b"NES\x1a\x01\x01") == "read.ines"
    assert detect_container(reg, "cart.bin", b"NES\x1a\x01\x01") == "read.ines"
    assert detect_container(reg, "cart.nes", b"not-an-ines-header") == RAW_READ
    # No magic to assert on, so .smd is claimed on its name alone.
    assert detect_container(reg, "cart.smd", b"\x00" * 16) == "read.smd"
    assert detect_container(reg, "cart.bin", b"\x00" * 16) == RAW_READ
    # Never auto-detected: deinterleaving a plain image would scramble it.
    assert detect_container(reg, "cart.sfc", b"\x00" * 16) != "read.snes-interleaved"


def test_detection_reads_the_head_off_disk(tmp_path) -> None:
    # The head argument is an optimisation, not a requirement — callers that
    # have not read the file hand over the path alone.
    f = tmp_path / "cart.nes"
    f.write_bytes(b"NES\x1a" + bytes(64))
    assert detect_container(default_registry(), str(f)) == "read.ines"
    assert detect_container(default_registry(), str(tmp_path / "gone.nes")) == RAW_READ


def test_container_write_id_pairs_by_convention() -> None:
    # read.X -> write.X, falling back to plain bytes for a container with no
    # writer of its own (iNES ships none).
    reg = default_registry()
    assert container_write_id(reg, RAW_READ) == RAW_WRITE
    assert container_write_id(reg, "read.ines") == RAW_WRITE


def test_container_ids_degrade_when_the_plugin_is_gone() -> None:
    # A project names the container its files were opened with, and the plugin
    # behind it can be uninstalled or left untrusted before the next launch —
    # that must show raw bytes, not fail the load.
    reg = default_registry()
    assert container_ids(reg, "read.ines") == ("read.ines", RAW_WRITE)
    assert container_ids(reg, "read.no-such-plugin") == (RAW_READ, RAW_WRITE)


def test_file_entry_reads_through_its_container(tmp_path) -> None:
    # The Entry -> PathwayConfig hop: a file's container reaches Read/Write,
    # while a slice of it stays on plain bytes (its offsets are its parent's).
    reg = default_registry()
    entry = Entry(name="g.nes", kind=EntryKind.FILE, path="g.nes")
    entry.container_id = "read.ines"
    cfg = pixel_config_for(entry, "preset.pixel.nes-2bpp", 0, reg)
    assert (cfg.read_id, cfg.write_id) == ("read.ines", RAW_WRITE)

    sliced = new_slice("g.nes", "s", 0x10, 0x20)
    slice_cfg = pixel_config_for(sliced, "preset.pixel.nes-2bpp", 0, reg)
    assert (slice_cfg.read_id, slice_cfg.write_id) == (RAW_READ, RAW_WRITE)


def _gb_rom(size: int = 0x8000) -> bytearray:
    rom = bytearray(size)
    rom[0x104:0x10C] = bytes([0xCE, 0xED, 0x66, 0x66, 0xCC, 0x0D, 0x00, 0x0B])
    rom[0x134:0x144] = b"TESTCART\x00\x00\x00\x00\x00\x00\x00\x00"
    return rom


def test_gb_write_repairs_both_checksums(tmp_path) -> None:
    # The header sum is what the boot ROM checks (a wrong one is a blank screen
    # on hardware); the global sum is the one tile edits actually invalidate.
    rom = _gb_rom()
    f = tmp_path / "game.gb"
    f.write_bytes(bytes(rom))

    edited = bytearray(rom)
    edited[0x4000:0x4010] = bytes(range(16))  # "tile edits", far from the header
    GbRomWriter().write(bytes(edited), FileRef(str(f)), PipelineContext())
    out = f.read_bytes()

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


def test_gb_write_leaves_a_headerless_file_alone(tmp_path) -> None:
    # Too short to hold a header: inventing one would corrupt whatever it is.
    f = tmp_path / "tiny.gb"
    GbRomWriter().write(b"\xaa" * 64, FileRef(str(f)), PipelineContext())
    assert f.read_bytes() == b"\xaa" * 64


def test_n64_round_trips_each_byte_order(tmp_path) -> None:
    # Read normalises to native so offsets mean what the docs say; Write puts
    # the file back in the order it arrived in, so other tools still read it.
    native = b"\x80\x37\x12\x40" + bytes((i * 11) & 0xFF for i in range(0x400 - 4))
    for name, width in (("rom.z64", 0), ("rom.v64", 2), ("rom.n64", 4)):
        on_disk = swap_groups(native, width)
        f = tmp_path / name
        f.write_bytes(on_disk)

        ctx = PipelineContext()
        assert N64RomReader().read(FileRef(str(f)), ctx) == native
        assert ctx.get(KEY_N64_SWAP) == width
        N64RomWriter().write(native, FileRef(str(f)), ctx)
        assert f.read_bytes() == on_disk


def test_n64_splices_a_bounded_edit_in_native_coordinates(tmp_path) -> None:
    # A byte's position only survives the swap within its own group, so an
    # unaligned splice has to happen in native order and be swapped back whole.
    native = bytearray(b"\x80\x37\x12\x40" + bytes(range(0x40)))
    f = tmp_path / "rom.v64"
    f.write_bytes(swap_groups(bytes(native), 2))

    ctx = PipelineContext()
    N64RomReader().read(FileRef(str(f)), ctx)  # arms the width
    N64RomWriter().write(b"\xee\xff", FileRef(str(f), offset=0x11, length=2), ctx)

    native[0x11:0x13] = b"\xee\xff"
    assert f.read_bytes() == swap_groups(bytes(native), 2)


def test_n64_swap_leaves_a_trailing_partial_group(tmp_path) -> None:
    # A truncated dump keeps its tail rather than losing it to a partial group.
    assert swap_groups(b"\x01\x02\x03\x04\x05", 4) == b"\x04\x03\x02\x01\x05"
    assert swap_groups(b"\x01\x02\x03", 2) == b"\x02\x01\x03"
    assert swap_groups(b"\x01\x02\x03", 0) == b"\x01\x02\x03"
