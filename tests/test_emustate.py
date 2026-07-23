"""Tests for the emulator save-state palette locator.

The regression risk here is the byte/offset math, the decompression handling and
the content-detection: a wrong offset or a mis-detected format silently yields a
plausible-but-wrong palette, which is worse than an error. Each console's fixture
is the minimal state that exercises its locator.
"""

from __future__ import annotations

import gzip
import struct
import zlib

import pytest

from celpix.core import emustate

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _u32(v: int) -> bytes:
    return struct.pack("<I", v)


def _bgr555(r: int, g: int, b: int) -> int:
    return ((b >> 3) << 10) | ((g >> 3) << 5) | (r >> 3)


# ---------------------------------------------------------------------------
# FCEUX (NES)
# ---------------------------------------------------------------------------


def _fceux_field(tag: bytes, value: bytes) -> bytes:
    return tag + _u32(len(value)) + value


def _fceux_section(section_type: int, body: bytes) -> bytes:
    return bytes([section_type]) + _u32(len(body)) + body


def _fceux_state(*, pram: bytes = bytes(range(32)), compressed: bool = False) -> bytes:
    # A PPU section (type 3) with the palette field flanked by decoys of the
    # same shape (nametable, sprite RAM) that must not be mistaken for it.
    ppu = (
        _fceux_field(b"NTAR", b"\x00" * 0x800)
        + _fceux_field(b"PRAM", pram)
        + _fceux_field(b"SPRA", b"\x00" * 0x100)
    )
    payload = _fceux_section(1, b"\x00" * 4) + _fceux_section(3, ppu)
    if compressed:
        comp = zlib.compress(payload)
        return b"FCSX" + _u32(len(payload)) + _u32(0x00020400) + _u32(len(comp)) + comp
    return b"FCSX" + _u32(len(payload)) + _u32(0x00020400) + _u32(0xFFFFFFFF) + payload


def test_fceux_reads_pram_from_the_ppu_section() -> None:
    pram = bytes(range(32))
    fmt, region = emustate.locate_palette(_fceux_state(pram=pram), ".fc0")
    assert (fmt.id, fmt.console) == ("fceux", "NES")
    assert (region.count, region.preset_id) == (32, "preset.palette.nes-indexed")
    assert region.data == pram


def test_fceux_decompresses_a_zlib_payload() -> None:
    pram = bytes(range(31, -1, -1))
    _, region = emustate.locate_palette(
        _fceux_state(pram=pram, compressed=True), ".fc0"
    )
    assert region.data == pram


def test_fceux_without_pram_field_raises() -> None:
    payload = _fceux_section(3, _fceux_field(b"NTAR", b"\x00" * 0x800))
    state = b"FCSX" + _u32(len(payload)) + _u32(0) + _u32(0xFFFFFFFF) + payload
    with pytest.raises(emustate.StateError, match="PRAM"):
        emustate.locate_palette(state, ".fc0")


# ---------------------------------------------------------------------------
# Snes9x (SNES)
# ---------------------------------------------------------------------------


def _snes9x_block(name: bytes, body: bytes) -> bytes:
    return name + b":" + f"{len(body):06d}".encode() + b":" + body


def _snes9x_state(
    *, cgram: bytes, version: int = 12, gzipped: bool = True, offset: int = 64
) -> bytes:
    ppu = b"\x00" * offset + cgram + b"\x00" * 8
    raw = (
        f"#!s9xsnp:{version:04d}\n".encode()
        + _snes9x_block(b"CPU", b"\x00" * 5)
        + _snes9x_block(b"PPU", ppu)
    )
    return gzip.compress(raw) if gzipped else raw


def test_snes9x_reads_cgdata_at_offset_64_for_v11plus() -> None:
    cgram = bytes(range(256)) * 2  # 512 distinct-ish bytes
    fmt, region = emustate.locate_palette(_snes9x_state(cgram=cgram), ".000")
    assert (fmt.id, fmt.console) == ("snes9x", "SNES")
    # Big-endian CGRAM decodes through the big-endian BGR555 preset, not the plain one.
    assert (region.count, region.preset_id) == (256, "preset.palette.bgr555-be")
    assert region.data == cgram


def test_snes9x_cgdata_offset_is_63_before_v11() -> None:
    cgram = b"\xab" * 512
    _, region = emustate.locate_palette(
        _snes9x_state(cgram=cgram, version=10, offset=63), ".000"
    )
    assert region.data == cgram


def test_snes9x_reads_an_uncompressed_state() -> None:
    cgram = b"\xcd" * 512
    _, region = emustate.locate_palette(
        _snes9x_state(cgram=cgram, gzipped=False), ".000"
    )
    assert region.data == cgram


def test_snes9x_without_ppu_block_raises() -> None:
    raw = b"#!s9xsnp:0012\n" + _snes9x_block(b"CPU", b"\x00" * 5)
    with pytest.raises(emustate.StateError, match="PPU"):
        emustate.locate_palette(gzip.compress(raw), ".000")


# ---------------------------------------------------------------------------
# Mesen / MesenCE (NES, SNES, Game Boy Color)
# ---------------------------------------------------------------------------

_CGRAM_COLORS = [(0xF8, 0x00, 0x00), (0x00, 0xF8, 0x00), (0x00, 0x00, 0xF8)]


def _cgram_bytes(colors: list[tuple[int, int, int]]) -> bytes:
    words = [_bgr555(*c) for c in colors] + [0] * (256 - len(colors))
    return struct.pack("<256H", *words)


def _mesen_records(*records: tuple[bytes, bytes]) -> bytes:
    return b"".join(key + b"\x00" + _u32(len(val)) + val for key, val in records)


def _mesen_state(
    *,
    console: int = 0,
    format_version: int = 4,
    records: bytes,
    compressed: bool = True,
) -> bytes:
    """A ``.mss`` state: header, a dummy zlib screenshot, ROM name, then the
    serialized record stream (optionally whole-blob zlib-compressed)."""
    screenshot = zlib.compress(b"\x00" * 16)
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


def test_mesen_snes_reads_cgram_by_label() -> None:
    cgram = _cgram_bytes(_CGRAM_COLORS)
    records = _mesen_records((b"ppu.vramaddress", b"\x00\x00"), (b"ppu.cgram", cgram))
    fmt, region = emustate.locate_palette(_mesen_state(records=records), ".mss")
    assert (fmt.id, fmt.console) == ("mesen", "NES / SNES / Game Boy Color")
    assert (region.count, region.preset_id) == (256, "preset.palette.bgr555")
    assert region.data == cgram


def test_mesen_nes_reads_palette_ram() -> None:
    pram = bytes(range(32))
    records = _mesen_records((b"ppu.paletteRam", pram))
    _, region = emustate.locate_palette(
        _mesen_state(console=2, records=records), ".mss"
    )
    assert (region.count, region.preset_id) == (32, "preset.palette.nes-indexed")
    assert region.data == pram


def test_mesen_gbc_concatenates_bg_then_object_palettes() -> None:
    bg = bytes(range(64))
    obj = bytes(range(64, 128))
    records = _mesen_records((b"ppu.cgbBgPalettes", bg), (b"ppu.cgbObjPalettes", obj))
    _, region = emustate.locate_palette(
        _mesen_state(console=1, records=records), ".mss"
    )
    assert (region.count, region.preset_id) == (64, "preset.palette.bgr555")
    assert region.data == bg + obj


def test_mesen_dmg_state_without_color_records_raises() -> None:
    # A monochrome DMG state simply has no CGB palette records; that absence is
    # the DMG signal and must surface as a clear "GBC only" error.
    records = _mesen_records((b"ppu.bgPalette", b"\xe4"))
    with pytest.raises(emustate.StateError, match="Game Boy Color"):
        emustate.locate_palette(_mesen_state(console=1, records=records), ".mss")


def test_mesen_handles_uncompressed_blob_and_legacy_header() -> None:
    cgram = _cgram_bytes(_CGRAM_COLORS)
    records = _mesen_records((b"ppu.cgram", cgram))
    _, region = emustate.locate_palette(
        _mesen_state(format_version=2, records=records, compressed=False), ".mss"
    )
    assert region.data == cgram


def test_mesen_unsupported_console_names_itself() -> None:
    records = _mesen_records((b"ppu.cgram", _cgram_bytes(_CGRAM_COLORS)))
    with pytest.raises(emustate.StateError, match="GBA"):
        emustate.locate_palette(_mesen_state(console=5, records=records), ".mss")


# ---------------------------------------------------------------------------
# Genesis Plus GX (Sega Genesis / Mega Drive)
# ---------------------------------------------------------------------------


def _gpgx_state(*, packed_cram: bytes, gzipped: bool = True) -> bytes:
    body = bytearray(emustate._GPGX_CRAM_OFFSET)
    body[0:16] = b"GENPLUS-GX 1.7.6"
    body += packed_cram
    return gzip.compress(bytes(body)) if gzipped else bytes(body)


def test_gpgx_expands_packed_cram_to_the_spaced_genesis_layout() -> None:
    # Packed 9-bit words BBBGGGRRR (little-endian) → spaced 0000BBB0GGG0RRR0 (BE).
    packed = struct.pack(
        "<64H", 0b000_000_111, 0b000_111_000, 0b111_000_000, *([0] * 61)
    )
    fmt, region = emustate.locate_palette(_gpgx_state(packed_cram=packed), ".gp0")
    assert (fmt.id, fmt.console) == ("gpgx", "Genesis")
    assert (region.count, region.preset_id) == (64, "preset.palette.genesis-9bpp")
    # R=7 → 0x000E; G=7 → 0x00E0; B=7 → 0x0E00, each big-endian.
    assert region.data[0:6] == b"\x00\x0e\x00\xe0\x0e\x00"


def test_gpgx_reads_an_uncompressed_state() -> None:
    packed = b"\x00" * 128
    _, region = emustate.locate_palette(
        _gpgx_state(packed_cram=packed, gzipped=False), ".gp0"
    )
    assert region.data == b"\x00" * 128


def test_gpgx_too_short_for_mega_drive_cram_raises() -> None:
    short = gzip.compress(b"GENPLUS-GX 1.7.6" + b"\x00" * 16)
    with pytest.raises(emustate.StateError, match="Mega Drive"):
        emustate.locate_palette(short, ".gp0")


# ---------------------------------------------------------------------------
# BESS (SameBoy / BGB / Emulicious — Game Boy Color)
# ---------------------------------------------------------------------------


def _bess_state(*, bg: bytes | None, obj: bytes | None) -> bytes:
    parts = bytearray(b"\x00" * 16)  # host-native leading area
    bg_off = len(parts)
    parts += bg if bg is not None else b""
    obj_off = len(parts)
    parts += obj if obj is not None else b""
    core = bytearray(0xD0)
    if bg is not None and obj is not None:
        core[0xC0:0xC4] = _u32(0x40)
        core[0xC4:0xC8] = _u32(bg_off)
        core[0xC8:0xCC] = _u32(0x40)
        core[0xCC:0xD0] = _u32(obj_off)
    first_block = len(parts)
    parts += b"CORE" + _u32(len(core)) + bytes(core)
    parts += b"END " + _u32(0)
    parts += _u32(first_block) + b"BESS"
    return bytes(parts)


def test_bess_reads_palettes_via_the_core_block_pointers() -> None:
    bg = bytes(range(0x40))
    obj = bytes(range(0x40, 0x80))
    fmt, region = emustate.locate_palette(_bess_state(bg=bg, obj=obj), ".sav")
    assert (fmt.id, fmt.console) == ("bess", "Game Boy Color")
    assert (region.count, region.preset_id) == (64, "preset.palette.bgr555")
    assert region.data == bg + obj


def test_bess_dmg_state_with_zero_sized_palettes_raises() -> None:
    with pytest.raises(emustate.StateError, match="Game Boy Color"):
        emustate.locate_palette(_bess_state(bg=None, obj=None), ".sav")


# ---------------------------------------------------------------------------
# mGBA (Game Boy Advance)
# ---------------------------------------------------------------------------


def _mgba_struct(*, pram: bytes, version: int = 0x0B) -> bytes:
    state = bytearray(0x800 + 1024)
    state[0:4] = (0x01000000 + version).to_bytes(4, "little")
    state[0x800 : 0x800 + 1024] = pram
    return bytes(state)


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + chunk_type + data + b"\x00\x00\x00\x00"


def test_mgba_reads_pram_from_a_raw_struct() -> None:
    pram = bytes(i & 0xFF for i in range(1024))
    fmt, region = emustate.locate_palette(_mgba_struct(pram=pram), ".ss0")
    assert (fmt.id, fmt.console) == ("mgba", "Game Boy Advance")
    assert (region.count, region.preset_id) == (512, "preset.palette.bgr555")
    assert region.data == pram


def test_mgba_reads_pram_from_a_png_wrapped_state() -> None:
    pram = bytes((i * 3) & 0xFF for i in range(1024))
    struct_bytes = _mgba_struct(pram=pram)
    png = (
        _PNG_SIG
        + _png_chunk(b"IHDR", b"\x00" * 13)
        + _png_chunk(b"gbAs", zlib.compress(struct_bytes))
        + _png_chunk(b"IEND", b"")
    )
    _, region = emustate.locate_palette(png, ".ss0")
    assert region.data == pram


def test_mgba_ignores_a_png_that_is_not_a_state() -> None:
    png = _PNG_SIG + _png_chunk(b"IHDR", b"\x00" * 13) + _png_chunk(b"IEND", b"")
    with pytest.raises(emustate.StateError, match="Unrecognised"):
        emustate.locate_palette(png, ".png")


# ---------------------------------------------------------------------------
# Detection / dispatch
# ---------------------------------------------------------------------------


def test_unrecognised_state_raises() -> None:
    with pytest.raises(emustate.StateError, match="Unrecognised"):
        emustate.locate_palette(b"not a save state", ".bin")


def test_detection_is_content_not_extension() -> None:
    # A correct FCEUX state under a wrong (SNES-ish) extension still resolves by
    # its "FCSX" magic — extensions are never consulted.
    fmt, _ = emustate.locate_palette(_fceux_state(), ".000")
    assert fmt.id == "fceux"
