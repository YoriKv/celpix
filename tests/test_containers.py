"""Container Read plugins (iNES / .smd / SNES interleave) and Konami NES RLE."""

from __future__ import annotations

from celpix.core.context import KEY_SOURCE_OFFSET, PipelineContext
from celpix.plugins.base import FileRef
from celpix.plugins.builtins.container_read import (
    INesReader,
    SmdReader,
    SnesInterleavedReader,
)


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
