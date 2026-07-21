"""Container Read plugins (iNES / .smd) and the Konami NES RLE decompressor."""

from __future__ import annotations

from celpix.core.context import KEY_SOURCE_OFFSET, PipelineContext
from celpix.plugins.base import FileRef
from celpix.plugins.builtins.container_read import INesReader, SmdReader
from celpix.plugins.builtins.konami_rle import KonamiNesRleDecompress


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


def test_konami_rle_fill_literal_end() -> None:
    # fill 3×0xAA ; literal copy of 2 bytes ; end.
    stream = bytes([0x03, 0xAA, 0x82, 0x11, 0x22, 0xFF, 0x99])
    out = KonamiNesRleDecompress().decompress(stream, PipelineContext())
    assert out == b"\xaa\xaa\xaa\x11\x22"  # 0x99 after the 0xFF terminator is ignored


def test_konami_rle_block_separator_continues() -> None:
    # 0x7F ends a block but decompression continues into the next.
    stream = bytes([0x02, 0x01, 0x7F, 0x02, 0x02, 0xFF])
    out = KonamiNesRleDecompress().decompress(stream, PipelineContext())
    assert out == b"\x01\x01\x02\x02"
