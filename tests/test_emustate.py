"""Tests for the emulator save-state palette locator.

The regression risk here is the byte/offset math and the detection ordering:
a wrong offset or a mis-ordered format silently yields a plausible-but-wrong
palette, which is worse than an error. Each console's fixture is the minimal
state that exercises its locator.
"""

from __future__ import annotations

import pytest

from celpix.core import emustate


def _virtuanes(version: int, *, block_size: int = 0, size: int = 4096) -> bytes:
    """A VirtuaNES state header: magic, version byte at 15, v2 block size at 44."""
    data = bytearray(b"\x00" * size)
    data[0:9] = b"VirtuaNES"
    data[15] = version
    data[44:48] = block_size.to_bytes(4, "little")
    return bytes(data)


def test_virtuanes_v1_is_a_fixed_offset() -> None:
    fmt, region = emustate.locate_palette(_virtuanes(1), ".st0")
    assert (fmt.id, fmt.console) == ("virtuanes", "NES")
    assert region.offset == 2440
    assert region.count == 32  # NES PPU palette RAM
    assert region.preset_id == "preset.palette.nes-indexed"


def test_virtuanes_v2_offset_tracks_the_stored_block_size() -> None:
    # v2 palette RAM sits past a variable RAM block: 48 + block + 16 + 2048.
    _, region = emustate.locate_palette(_virtuanes(2, block_size=100), "st1")
    assert region.offset == 48 + 100 + 16 + 2048
    _, region = emustate.locate_palette(_virtuanes(2, block_size=0), "st1")
    assert region.offset == 48 + 0 + 16 + 2048


def test_virtuanes_unsupported_version_raises() -> None:
    with pytest.raises(emustate.StateError, match="version 9"):
        emustate.locate_palette(_virtuanes(9), "st0")


def test_virtuanes_truncated_raises_not_returns_none() -> None:
    # Magic present but too short for the version byte: "mine but broken".
    with pytest.raises(emustate.StateError, match="truncated"):
        emustate.locate_palette(b"VirtuaNES", "st0")


def _snes9x_gt(*, pre_tag: bytes = b"\x00" * 4, cgram: bytes = b"\xab" * 512) -> bytes:
    # GTSF container, a "PAL " chunk (4-byte tag + 4-byte length) then CGRAM.
    return b"GTSF" + pre_tag + b"PAL " + b"\x00\x00\x00\x00" + cgram


def test_snes9x_gt_palette_starts_eight_bytes_past_the_pal_tag() -> None:
    fmt, region = emustate.locate_palette(_snes9x_gt(pre_tag=b"XY"), ".sv0")
    assert (fmt.id, fmt.console) == ("snes9x-gt", "SNES")
    # "GTSF"(4) + pre_tag(2) = tag at 6; data 8 past the tag.
    assert region.offset == 6 + 8
    assert region.count == 256
    assert region.preset_id == "preset.palette.bgr555"


def test_snes9x_gt_without_pal_chunk_raises() -> None:
    with pytest.raises(emustate.StateError, match="PAL"):
        emustate.locate_palette(b"GTSF" + b"\x00" * 200, "sv0")


def test_zsnes_is_extension_keyed_fixed_offset() -> None:
    for ext in ("zst", "zs0", "zs9"):
        fmt, region = emustate.locate_palette(b"\x00" * 3000, ext)
        assert (fmt.id, fmt.console) == ("zsnes", "SNES")
        assert region.offset == 1560
        assert region.count == 256
        assert region.preset_id == "preset.palette.bgr555"


def test_genesis_is_extension_keyed_fixed_offset() -> None:
    for ext in ("gs0", "gsx"):
        fmt, region = emustate.locate_palette(b"\x00" * 500, ext)
        assert (fmt.id, fmt.console) == ("gens", "Genesis")
        assert region.offset == 274
        assert region.count == 64
        assert region.preset_id == "preset.palette.genesis-9bpp"


def test_signature_wins_over_extension() -> None:
    # A GTSF state that happens to carry a ZSNES extension must still be read as
    # Snes9x-GT — magic formats are tried before the extension fallbacks, so a
    # renamed (or wrong-slot-extension) file resolves by its content.
    fmt, region = emustate.locate_palette(_snes9x_gt(), "zst")
    assert fmt.id == "snes9x-gt"
    assert region.offset == 4 + 4 + 8  # not ZSNES's 1560


def test_extension_is_case_and_dot_insensitive() -> None:
    fmt_a, _ = emustate.locate_palette(b"\x00" * 3000, ".ZST")
    fmt_b, _ = emustate.locate_palette(b"\x00" * 3000, "zst")
    assert fmt_a.id == fmt_b.id == "zsnes"


def test_unrecognised_state_raises() -> None:
    with pytest.raises(emustate.StateError, match="Unrecognised"):
        emustate.locate_palette(b"not a save state", "bin")


def test_wrong_length_extension_does_not_match() -> None:
    # The family match is exactly prefix + one slot char; a 2- or 4-char
    # extension in the same family must not be mistaken for it.
    with pytest.raises(emustate.StateError):
        emustate.locate_palette(b"\x00" * 3000, "zs")
    with pytest.raises(emustate.StateError):
        emustate.locate_palette(b"\x00" * 3000, "zsta")


# --- Mesen / MesenCE -------------------------------------------------------

import struct  # noqa: E402
import zlib  # noqa: E402

# Three distinct, 5-bit-exact colours used to build a CGRAM to assert on.
_CGRAM_COLORS = [(0xF8, 0x00, 0x00), (0x00, 0xF8, 0x00), (0x00, 0x00, 0xF8)]


def _bgr555(r: int, g: int, b: int) -> int:
    return ((b >> 3) << 10) | ((g >> 3) << 5) | (r >> 3)


def _cgram_bytes(colors: list[tuple[int, int, int]]) -> bytes:
    # 256-word CGRAM: the given colours up front, the rest black (0x0000).
    words = [_bgr555(*c) for c in colors] + [0] * (256 - len(colors))
    return struct.pack("<256H", *words)


def _u32(v: int) -> bytes:
    return struct.pack("<I", v)


def _mesen_records(*records: tuple[bytes, bytes]) -> bytes:
    # The labelled record stream: NUL-terminated key, uint32 length, value.
    return b"".join(key + b"\x00" + _u32(len(val)) + val for key, val in records)


def _mesen_state(
    *,
    console: int = 0,
    format_version: int = 4,
    records: bytes | None = None,
    compressed: bool = True,
) -> bytes:
    """A ``.mss`` state built to the MesenCE on-disk format: header, a (dummy)
    zlib screenshot, ROM name, then the serialized record stream."""
    if records is None:
        cgram = _cgram_bytes(_CGRAM_COLORS)  # 512-byte CGRAM to find by label
        records = _mesen_records(
            (b"ppu.vramaddress", b"\x00\x00"), (b"ppu.cgram", cgram)
        )

    screenshot = zlib.compress(b"\x00" * 16)  # header stores a compressed frame
    header = b"MSS" + _u32(0) + _u32(format_version)
    if format_version <= 3:
        header += b"\x00" * 40  # legacy SHA1 field
    header += _u32(console)
    video = (
        _u32(16) + _u32(2) + _u32(2) + _u32(100) + _u32(len(screenshot)) + screenshot
    )
    name = _u32(4) + b"game"

    if compressed:
        comp = zlib.compress(records)
        blob = b"\x01" + _u32(len(records)) + _u32(len(comp)) + comp
    else:
        blob = b"\x00" + records
    return header + video + name + blob


def test_mesen_reads_cgram_by_label() -> None:
    fmt, region = emustate.locate_palette(_mesen_state(), ".mss")
    assert (fmt.id, fmt.console) == ("mesen", "SNES")
    assert (region.count, region.preset_id) == (256, "preset.palette.bgr555")
    assert region.data == _cgram_bytes(_CGRAM_COLORS)


def test_mesen_handles_uncompressed_blob_and_legacy_header() -> None:
    # An older (<=3) state carries a 40-byte SHA1 the parser must skip; the blob
    # may also be stored uncompressed. Both still resolve to the same CGRAM.
    _, region = emustate.locate_palette(
        _mesen_state(format_version=2, compressed=False), ".mss"
    )
    assert region.data == _cgram_bytes(_CGRAM_COLORS)


def test_mesen_non_snes_console_is_rejected_by_name() -> None:
    with pytest.raises(emustate.StateError, match="NES"):
        emustate.locate_palette(_mesen_state(console=2), ".mss")  # 2 = NES


def test_mesen_without_cgram_record_raises() -> None:
    only_other = _mesen_records((b"ppu.vramaddress", b"\x00\x00"))
    with pytest.raises(emustate.StateError, match="cgram"):
        emustate.locate_palette(_mesen_state(records=only_other), ".mss")


def test_mesen_similar_key_or_wrong_size_is_not_mistaken_for_cgram() -> None:
    # "internalcgramaddress" ends the wrong way and a short "cgram" is the wrong
    # size — neither is the 512-byte palette record.
    decoys = _mesen_records(
        (b"ppu.internalcgramaddress", b"\x00\x00"),
        (b"ppu.cgram", b"\x01\x02"),  # too small
    )
    with pytest.raises(emustate.StateError, match="cgram"):
        emustate.locate_palette(_mesen_state(records=decoys), ".mss")
