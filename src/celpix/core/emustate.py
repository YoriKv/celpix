"""Locate a console's live palette inside an emulator save-state file.

An emulator save state is a dump of the machine's RAM, and buried in it is the
PPU/CGRAM palette the running game was displaying. "Emulator State" palette mode
pulls those bytes back out so the tiles render in the game's actual colors,
without having to hunt for a palette in the ROM by hand.

This module is only the *locator*. Given a state file's bytes it recognises the
emulator/console and returns a :class:`PaletteRegion`: the palette bytes plus the
palette-format preset that decodes them. The decode itself is the ordinary
palette pipeline, so every color format the app already ships (BGR555, the NES
master-palette table, the Genesis 9bpp layout) is reused as-is — an emulator
state is just an extraction plus a known palette preset. Kept Qt-free (model
layer): the UI opens the file and hands the bytes here.

**Detection is signature-first, on content — never on the file extension.** Every
supported emulator is current and writes a recognisable state, so a renamed file
still resolves and there is no ambiguous extension-keyed fallback tier. Save
states are frequently compressed (Snes9x and standalone Genesis Plus GX gzip
their states; FCEUX zlib-compresses the payload; mGBA embeds a zlib stream in a
PNG), so most locators decompress before they can even read their signature.

Rather than point at a raw file offset, every locator *extracts* the palette —
decompress, walk labelled records or fixed struct offsets — and hands the located
bytes back inline (:attr:`PaletteRegion.data`). The caller feeds those straight
through the pipeline. New emulators are added by appending to :data:`STATE_FORMATS`.

Scope note: these read the *console* palette RAM, which only some machines have.
Game Boy Color states carry a palette (the original monochrome DMG has none, so a
DMG state is reported as unsupported); Mega Drive states are read, but a Genesis
Plus GX SMS/Game Gear state has a different layout and is not.
"""

from __future__ import annotations

import gzip
import zlib
from collections.abc import Callable
from dataclasses import dataclass

# The palette-format presets each console's colors decode through — the same
# built-ins the palette-format dropdown offers, reused verbatim.
_NES_PRESET = "preset.palette.nes-indexed"
# SNES/GBC/GBA palette RAM is 15-bit BGR555. Snes9x is the odd one out: it
# serialises CGRAM big-endian (see :func:`_locate_snes9x`), so it uses the
# big-endian variant of the very same color codec.
_BGR555_PRESET = "preset.palette.bgr555"
_BGR555_BE_PRESET = "preset.palette.bgr555-be"
_GENESIS_PRESET = "preset.palette.genesis-9bpp"

# Native palette sizes (in colors). NES PPU palette RAM is 32 one-byte
# master-palette indices; SNES CGRAM is 256 BGR555 colors; the GBC has 8 BG + 8
# OBJ palettes of 4 colors (64 total); the GBA has 256 BG + 256 OBJ (512 total);
# Genesis VDP CRAM is 64 9-bit colors (four 16-color palettes).
_NES_ENTRIES = 32
_SNES_ENTRIES = 256
_GBC_ENTRIES = 64
_GBA_ENTRIES = 512
_GENESIS_ENTRIES = 64


class StateError(Exception):
    """The file isn't a recognised save state, or its palette can't be located.

    Carries a human-facing message for the status bar.
    """


@dataclass(frozen=True)
class PaletteRegion:
    """A located palette, as pipeline-ready bytes.

    ``count`` is the console's full palette size; the caller floors it to what
    actually fits ``data``, so a truncated state still yields the colors it does
    contain. ``data`` holds the extracted palette bytes and ``preset_id`` names
    the color codec that decodes them. (``offset`` indexes into ``data`` and is
    0 for every current format — the field is retained for a future locator that
    wants to point into a raw file instead of extracting.)
    """

    offset: int
    count: int
    preset_id: str
    data: bytes | None = None


@dataclass(frozen=True)
class StateFormat:
    """One emulator save-state layout.

    ``locate`` returns the palette region for a file it recognises, ``None`` if
    the file isn't this format, and raises :class:`StateError` if the file *is*
    this format but its palette can't be found (unsupported version, missing
    chunk, monochrome console) — the difference between "not mine" and "mine but
    broken" drives the error message the user sees.
    """

    id: str
    name: str
    console: str
    locate: Callable[[bytes, str], PaletteRegion | None]


def _u32le(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 4], "little")


def _maybe_gunzip(data: bytes) -> bytes:
    """Transparently inflate a gzip-wrapped state (Snes9x, standalone Genesis
    Plus GX). A non-gzip file — or one that won't inflate — is returned as-is so
    the caller's signature check simply fails and detection moves on."""
    if data[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(data)
        except (OSError, EOFError, zlib.error):
            return data
    return data


# --- FCEUX (NES) -----------------------------------------------------------
#
# A modern FCEUX state opens with the ASCII magic "FCSX" and a 16-byte header:
# uncompressed payload size at 4, version at 8, and a compressed length at 12
# (0xFFFFFFFF meaning "not compressed"). When set, the payload after the header
# is a single zlib stream — NOT gzip, and only the payload, not the whole file.
# The decompressed payload is a flat run of sections (1-byte type + uint32 size);
# section type 3 is the PPU, and inside it the field tagged "PRAM" (4-byte tag +
# uint32 size) is the 32 bytes of palette RAM. Values are master-palette indices
# 0x00-0x3F, decoded by the same NES-indexed preset the dropdown offers.


def _locate_fceux(data: bytes, ext: str) -> PaletteRegion | None:
    if not data.startswith(b"FCSX"):
        return None
    if len(data) < 16:
        raise StateError("FCEUX state is truncated (no header).")
    total_size = _u32le(data, 4)
    compressed_len = _u32le(data, 12)
    payload = data[16:]
    if compressed_len != 0xFFFFFFFF:
        try:
            body = zlib.decompress(payload[:compressed_len])
        except zlib.error as exc:
            raise StateError(f"FCEUX state won't decompress ({exc}).") from exc
    else:
        body = payload[:total_size]
    pram = _fceux_pram(body)
    if pram is None:
        raise StateError("FCEUX state: no PPU 'PRAM' palette record found.")
    return PaletteRegion(0, _NES_ENTRIES, _NES_PRESET, data=pram)


def _fceux_pram(body: bytes) -> bytes | None:
    """Walk the section list for the PPU section (type 3) and return its "PRAM"
    field — the 32 bytes of palette RAM."""
    i, n = 0, len(body)
    while i + 5 <= n:
        section_type = body[i]
        size = _u32le(body, i + 1)
        i += 5
        section = body[i : i + size]
        i += size
        if section_type == 3:  # PPU
            field = _fceux_field(section, b"PRAM", _NES_ENTRIES)
            if field is not None:
                return field
    return None


def _fceux_field(section: bytes, tag: bytes, size: int) -> bytes | None:
    """Value of the ``size``-byte field tagged ``tag`` within a section — a run
    of 4-byte tag / uint32 length / value. ``None`` if absent."""
    j, m = 0, len(section)
    while j + 8 <= m:
        name = section[j : j + 4]
        field_size = _u32le(section, j + 4)
        j += 8
        value = section[j : j + field_size]
        j += field_size
        if name == tag and field_size == size:
            return value
    return None


# --- Snes9x (SNES) ---------------------------------------------------------
#
# A Snes9x freeze state (.000, .001, …) is gzip-compressed; inflated, it opens
# with "#!s9xsnp:" and a 4-digit version. The body is a run of blocks, each an
# 11-byte header (3-char NAME + ':' + 6-digit decimal length + ':') then the raw
# bytes. CGRAM lives inside the "PPU" block: Snes9x serialises the SPPU struct
# positionally (no per-field tags), so the CGDATA array sits at a fixed byte
# offset — 64 for state version >= 11, 63 before (one preceding byte, CGSavedByte,
# was added in v11). Each of the 256 colors is a uint16 written **big-endian**
# (MSB first), the reverse of a raw little-endian CGRAM dump — hence the
# big-endian BGR555 preset rather than the plain one.
_SNES9X_MAGIC = b"#!s9xsnp:"
_SNES9X_CGDATA_V11 = 64  # byte offset of CGDATA within the PPU block
_SNES9X_CGDATA_PRE_V11 = 63


def _locate_snes9x(data: bytes, ext: str) -> PaletteRegion | None:
    raw = _maybe_gunzip(data)
    if not raw.startswith(_SNES9X_MAGIC):
        return None
    try:
        version = int(raw[len(_SNES9X_MAGIC) : len(_SNES9X_MAGIC) + 4])
    except ValueError as exc:
        raise StateError("Snes9x state has a malformed version header.") from exc
    ppu = _snes9x_block(raw, b"PPU")
    if ppu is None:
        raise StateError("Snes9x state has no 'PPU' block (no CGRAM).")
    offset = _SNES9X_CGDATA_V11 if version >= 11 else _SNES9X_CGDATA_PRE_V11
    end = offset + _SNES_ENTRIES * 2
    if len(ppu) < end:
        raise StateError("Snes9x PPU block is too short to contain CGRAM.")
    return PaletteRegion(0, _SNES_ENTRIES, _BGR555_BE_PRESET, data=ppu[offset:end])


def _snes9x_block(raw: bytes, name: bytes) -> bytes | None:
    """Walk the block list past the version line and return the named block's
    body. Block headers are 11 bytes; an oversized block (> 999999) replaces the
    6 decimal digits with a dash sentinel and packs its length big-endian into
    header bytes 6-9, so the walk stays in sync even past VRAM/RAM."""
    start = raw.find(b"\n")
    if start < 0:
        return None
    i, n = start + 1, len(raw)
    while i + 11 <= n:
        if raw[i + 3] != 0x3A or raw[i + 10] != 0x3A:  # the two ':' separators
            return None
        tag = raw[i : i + 3]
        if raw[i + 4] == 0x2D:  # '-': length didn't fit, big-endian at bytes 6-9
            size = int.from_bytes(raw[i + 6 : i + 10], "big")
        else:
            try:
                size = int(raw[i + 4 : i + 10])
            except ValueError:
                return None
        body_start = i + 11
        if tag == name:
            return raw[body_start : body_start + size]
        i = body_start + size
    return None


# --- Mesen / MesenCE (NES, SNES, Game Boy Color) ---------------------------
#
# A Mesen ".mss" state is a "MSS" header (emu + format versions, console type, a
# zlib-compressed screenshot, the ROM name) followed by the serialized machine.
# That serialized blob is a flat run of **labelled** records — a NUL-terminated
# ASCII key, a uint32 length, then the value — optionally zlib-compressed as a
# whole. So unlike a memory image, the palette needs no scanning: it is the
# record whose key ends in the field name Mesen's serializer emits (every PPU
# field serialises under the "ppu" prefix, e.g. "ppu.cgram"). Reading it by name
# is robust to everything but a rename of that field.

# ConsoleType enum order in the Mesen source; index by the header's console id.
_MESEN_CONSOLES = ("SNES", "Game Boy", "NES", "PC Engine", "SMS", "GBA", "WonderSwan")

# Each CGB palette record is 8 palettes * 4 colors * 2 bytes.
_GBC_RECORD_BYTES = 8 * 4 * 2


def _mesen_state_blob(blob: bytes) -> bytes:
    """The serialized record stream: a leading flag byte, then either the raw
    records or (the default) a zlib block prefixed with its two uint32 sizes."""
    if not blob:
        raise StateError("Mesen state has no serialized data.")
    if blob[0] != 1:  # not whole-blob compressed — records follow the flag byte
        return blob[1:]
    compressed = blob[9 : 9 + _u32le(blob, 5)]  # after flag + original/compressed sizes
    try:
        return zlib.decompress(compressed)
    except zlib.error as exc:
        raise StateError(f"Mesen state won't decompress ({exc}).") from exc


def _mesen_record(blob: bytes, key: bytes, size: int) -> bytes | None:
    """Value of the record whose key is (or ends in ``.``) ``key`` and is exactly
    ``size`` bytes — walking the key/uint32-length/value stream. ``None`` if absent."""
    i, n = 0, len(blob)
    while i < n:
        end = blob.find(b"\x00", i)
        if end < 0 or end + 4 > n:
            break
        name = blob[i:end].lower()
        i = end + 1
        value_size = _u32le(blob, i)
        i += 4
        if i + value_size > n:
            break
        if value_size == size and (name == key or name.endswith(b"." + key)):
            return blob[i : i + value_size]
        i += value_size
    return None


def _locate_mesen(data: bytes, ext: str) -> PaletteRegion | None:
    if not data.startswith(b"MSS"):
        return None
    try:
        pos = 3 + 4  # magic + emulator version
        format_version = _u32le(data, pos)
        pos += 4
        if format_version <= 3:
            pos += 40  # older states carry a SHA1 field here; newer ones don't
        console = _u32le(data, pos)
        pos += 4
        pos += 16  # video data: frame size, width, height, scale
        pos += 4 + _u32le(data, pos)  # compressed screenshot (length-prefixed)
        pos += 4 + _u32le(data, pos)  # ROM name (length-prefixed)
        blob = _mesen_state_blob(data[pos:])
    except IndexError as exc:
        raise StateError("Mesen state is truncated or unsupported.") from exc
    return _mesen_palette(blob, console)


def _mesen_palette(blob: bytes, console: int) -> PaletteRegion:
    if console == 0:  # SNES: CGRAM, 256 BGR555 colors
        cgram = _mesen_record(blob, b"cgram", _SNES_ENTRIES * 2)
        if cgram is None:
            raise StateError("Mesen SNES state: no 'cgram' palette record found.")
        return PaletteRegion(0, _SNES_ENTRIES, _BGR555_PRESET, data=cgram)
    if console == 2:  # NES: 32 bytes of master-palette-index RAM
        pram = _mesen_record(blob, b"paletteram", _NES_ENTRIES)
        if pram is None:
            raise StateError("Mesen NES state: no palette RAM record found.")
        return PaletteRegion(0, _NES_ENTRIES, _NES_PRESET, data=pram)
    if console == 1:  # Game Boy / Game Boy Color
        # The CGB palette RAM is serialized only in color mode; a monochrome DMG
        # state simply has no such records, so its absence *is* the DMG signal.
        bg = _mesen_record(blob, b"cgbbgpalettes", _GBC_RECORD_BYTES)
        obj = _mesen_record(blob, b"cgbobjpalettes", _GBC_RECORD_BYTES)
        if bg is None or obj is None:
            raise StateError(
                "Mesen Game Boy state carries no color palette - only Game Boy "
                "Color states do (the original DMG has no palette RAM)."
            )
        return PaletteRegion(0, _GBC_ENTRIES, _BGR555_PRESET, data=bg + obj)
    name = _MESEN_CONSOLES[console] if console < len(_MESEN_CONSOLES) else "?"
    raise StateError(
        f"Mesen {name} state: only NES, SNES and Game Boy Color states are supported."
    )


# --- Genesis Plus GX (Sega Genesis / Mega Drive) ---------------------------
#
# A Genesis Plus GX state is (when standalone) gzip-wrapped; inflated, it opens
# with the version string "GENPLUS-GX ...". The core serialises its state
# positionally with no chunk tags, so VDP CRAM sits at a fixed offset for a Mega
# Drive state: 16 (version) + 0x10000 (work RAM) + 0x2000 (Z80 RAM) + 1 + 4
# (Z80 state) + 0x10 (I/O regs) + 0x400 (sprite cache) + 0x10000 (VRAM). CRAM is
# 64 words but is NOT the raw VDP bus value: the emulator packs each color to a
# contiguous 9-bit form (0b0000000B_BBGG_GRRR, host little-endian), so we expand
# it back to the spaced "0000 BBB0 GGG0 RRR0" layout the Genesis preset decodes.
# SMS/Game Gear states have a different preceding layout and are not handled.
_GPGX_MAGIC = b"GENPLUS-GX "
_GPGX_CRAM_OFFSET = 16 + 0x10000 + 0x2000 + 1 + 4 + 0x10 + 0x400 + 0x10000  # 0x22425
_GPGX_CRAM_BYTES = _GENESIS_ENTRIES * 2


def _locate_gpgx(data: bytes, ext: str) -> PaletteRegion | None:
    raw = _maybe_gunzip(data)
    if not raw.startswith(_GPGX_MAGIC):
        return None
    end = _GPGX_CRAM_OFFSET + _GPGX_CRAM_BYTES
    if len(raw) < end:
        raise StateError(
            "Genesis Plus GX state is too short for Mega Drive CRAM "
            "(an SMS / Game Gear state is not supported)."
        )
    packed = raw[_GPGX_CRAM_OFFSET:end]
    return PaletteRegion(
        0, _GENESIS_ENTRIES, _GENESIS_PRESET, data=_gpgx_expand(packed)
    )


def _gpgx_expand(packed: bytes) -> bytes:
    """Expand Genesis Plus GX's packed 9-bit CRAM (little-endian BBBGGGRRR words)
    to the spaced, big-endian ``0000 BBB0 GGG0 RRR0`` words the Genesis preset
    reads (component masks R=0x000E, G=0x00E0, B=0x0E00)."""
    out = bytearray()
    for i in range(0, len(packed), 2):
        word = packed[i] | (packed[i + 1] << 8)
        r = word & 0x7
        g = (word >> 3) & 0x7
        b = (word >> 6) & 0x7
        spaced = (b << 9) | (g << 5) | (r << 1)
        out += bytes((spaced >> 8, spaced & 0xFF))  # big-endian
    return bytes(out)


# --- BESS: SameBoy / BGB / Emulicious (Game Boy Color) ---------------------
#
# BESS ("Best Effort Save State") is a portable block chain *appended* to an
# emulator's native Game Boy state, so it is found from the end: the last 4 bytes
# are the ASCII magic "BESS" and the uint32 before them points (from file start)
# at the first block. Each block is a 4-char id + uint32 length + data, walked to
# the "END " terminator. The "CORE" block doesn't embed the palettes inline; it
# stores a size+offset pair for each (background at 0xC0/0xC4, object at
# 0xC8/0xCC, offsets from file start), pointing at the raw CGB palette RAM
# elsewhere in the file. Both are 0x40 bytes of 15-bit BGR555; a pre-color model
# stores size 0, which is the DMG signal.
_BESS_CORE_LEN = 0xD0
_BESS_BG_FIELD = 0xC0  # size (uint32) then offset (uint32) of the BG palettes
_BESS_OBJ_FIELD = 0xC8  # ... and the OBJ palettes
_BESS_PAL_BYTES = 0x40


def _locate_bess(data: bytes, ext: str) -> PaletteRegion | None:
    if len(data) < 8 or data[-4:] != b"BESS":
        return None
    core = _bess_block(data, _u32le(data, len(data) - 8), b"CORE")
    if core is None:
        raise StateError("BESS state has no 'CORE' block.")
    if len(core) < _BESS_CORE_LEN:
        raise StateError("BESS 'CORE' block is truncated.")
    bg = _bess_buffer(data, core, _BESS_BG_FIELD)
    obj = _bess_buffer(data, core, _BESS_OBJ_FIELD)
    if bg is None or obj is None:
        raise StateError(
            "BESS state carries no color palette - only Game Boy Color states "
            "do (the original DMG has no palette RAM)."
        )
    return PaletteRegion(0, _GBC_ENTRIES, _BGR555_PRESET, data=bg + obj)


def _bess_block(data: bytes, start: int, want: bytes) -> bytes | None:
    """Walk the BESS block chain from ``start`` and return the named block's body,
    stopping at the "END " terminator."""
    i, n = start, len(data)
    while i + 8 <= n:
        block_id = data[i : i + 4]
        length = _u32le(data, i + 4)
        body_start = i + 8
        if block_id == want:
            return data[body_start : body_start + length]
        if block_id == b"END ":
            break
        i = body_start + length
    return None


def _bess_buffer(data: bytes, core: bytes, field: int) -> bytes | None:
    """A palette buffer referenced by CORE: a (size, offset-from-file-start) pair.
    ``None`` unless it is the expected 0x40 bytes and lies within the file."""
    size = _u32le(core, field)
    offset = _u32le(core, field + 4)
    if size != _BESS_PAL_BYTES or offset + size > len(data):
        return None
    return data[offset : offset + size]


# --- mGBA (Game Boy Advance) -----------------------------------------------
#
# An mGBA state is a fixed C struct (GBASerializedState). Desktop mGBA saves it
# zlib-compressed inside a PNG (custom "gbAs" chunks); the raw form starts with a
# little-endian versionMagic whose top byte is 0x01. Either way, once we have the
# fixed struct, palette RAM ("pram") sits at the documented offset 0x800: 512
# uint16 BGR555 colors (256 BG then 256 OBJ), a verbatim copy of hardware PRAM.
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_MGBA_PRAM_OFFSET = 0x800
_MGBA_PRAM_BYTES = _GBA_ENTRIES * 2
_MGBA_MAGIC_MIN = 0x01000000  # versionMagic = 0x01000000 + state version
_MGBA_MAGIC_MAX = 0x0100000B  # newest version this layout is known good for


def _locate_mgba(data: bytes, ext: str) -> PaletteRegion | None:
    if data.startswith(_PNG_SIG):
        state = _mgba_from_png(data)
        if state is None:
            return None  # a PNG, but not an mGBA state
    elif (
        len(data) >= _MGBA_PRAM_OFFSET + _MGBA_PRAM_BYTES
        and data[3] == 0x01
        and _MGBA_MAGIC_MIN <= _u32le(data, 0) <= _MGBA_MAGIC_MAX
    ):
        state = data
    else:
        return None
    end = _MGBA_PRAM_OFFSET + _MGBA_PRAM_BYTES
    if len(state) < end:
        raise StateError("mGBA state is truncated (no palette RAM).")
    return PaletteRegion(
        0, _GBA_ENTRIES, _BGR555_PRESET, data=state[_MGBA_PRAM_OFFSET:end]
    )


def _mgba_from_png(data: bytes) -> bytes | None:
    """The fixed state struct out of a PNG-wrapped mGBA state: the zlib stream
    carried in one or more "gbAs" chunks. ``None`` if there are none (an ordinary
    PNG that isn't an mGBA state)."""
    i, n = len(_PNG_SIG), len(data)
    stream = bytearray()
    while i + 8 <= n:
        length = int.from_bytes(data[i : i + 4], "big")
        chunk_type = data[i + 4 : i + 8]
        body = i + 8
        if chunk_type == b"gbAs":
            stream += data[body : body + length]
        elif chunk_type == b"IEND":
            break
        i = body + length + 4  # skip the data and the 4-byte CRC
    if not stream:
        return None
    try:
        return zlib.decompress(bytes(stream))
    except zlib.error as exc:
        raise StateError(f"mGBA PNG state won't decompress ({exc}).") from exc


# The supported formats, all detected by content. Order is not significant: each
# signature is specific enough that at most one claims a given file (BESS is a
# trailing footer on a Game Boy state, so it is tried before mGBA's looser
# raw-struct check as a matter of defensiveness, not necessity).
STATE_FORMATS: tuple[StateFormat, ...] = (
    StateFormat("mesen", "Mesen", "NES / SNES / Game Boy Color", _locate_mesen),
    StateFormat("fceux", "FCEUX", "NES", _locate_fceux),
    StateFormat("snes9x", "Snes9x", "SNES", _locate_snes9x),
    StateFormat("gpgx", "Genesis Plus GX", "Genesis", _locate_gpgx),
    StateFormat(
        "bess", "BESS (SameBoy / BGB / Emulicious)", "Game Boy Color", _locate_bess
    ),
    StateFormat("mgba", "mGBA", "Game Boy Advance", _locate_mgba),
)


def locate_palette(data: bytes, ext: str) -> tuple[StateFormat, PaletteRegion]:
    """Detect the emulator behind ``data`` and locate its palette.

    ``ext`` is the file's extension; it is currently unused (every supported
    format is detected by content), retained for API stability and any future
    extension-keyed locator. Returns the matched :class:`StateFormat` and its
    :class:`PaletteRegion`. Raises :class:`StateError` if nothing matches, or a
    matched format's palette can't be located.
    """
    for fmt in STATE_FORMATS:
        region = fmt.locate(data, ext)
        if region is not None:
            return fmt, region
    raise StateError("Unrecognised save state - no known emulator format matched.")
