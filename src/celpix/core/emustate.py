"""Locate a console's live palette inside an emulator save-state file.

An emulator save state is a dump of the machine's RAM, and buried in it is the
PPU/CGRAM palette the running game was displaying. "Emulator State" palette mode
pulls those bytes back out so the tiles render in the game's actual colours,
without having to hunt for a palette in the ROM by hand.

This module is only the *locator*. Given a state file's bytes it recognises the
emulator/console and returns a :class:`PaletteRegion`: where the palette sits,
how many entries, and which palette-format preset decodes it. The decode itself
is the ordinary palette pipeline, so every colour format the app already ships
(BGR555, the NES/Genesis indexed tables, …) is reused as-is — an emulator state
is just a file offset plus a known palette preset. Kept Qt-free (model layer):
the UI opens the file and hands the bytes here.

Detection is signature-first. Formats that carry a magic header (Mesen, VirtuaNES,
Snes9x-GT) are matched on *content*, so a renamed file still resolves and — more
importantly — they claim their files before the extension-keyed fallbacks below
get a chance to mis-read them (NESticle and VirtuaNES both use ``.st?``, so
extension alone is ambiguous). The fallbacks (ZSNES, Genesis) read a documented
fixed offset for emulators whose states carry no usable signature; they are
version-sensitive by nature, hence the lower priority. New emulators are added by
appending to :data:`STATE_FORMATS`.

Most formats point at an offset in the state file. Mesen instead *extracts* the
palette — its state is a zlib-compressed stream of labelled records, so CGRAM is
read by its "ppu.cgram" name and handed back inline (:attr:`PaletteRegion.data`)
rather than as a file offset.

(BizHawk ``.State`` isn't supported: its "Core" lump is an opaque, core-defined
memory image with no addressable regions — BizHawk itself only finds CGRAM by
asking the *running* core for a live pointer — so it can't be located reliably
from the file. Export CGRAM from BizHawk's Hex Editor and load it as a palette
file instead.)
"""

from __future__ import annotations

import zlib
from collections.abc import Callable
from dataclasses import dataclass

# The palette-format presets each console's colours decode through — the same
# built-ins the palette-format dropdown offers, reused verbatim.
_NES_PRESET = "preset.palette.nes-indexed"
_SNES_PRESET = "preset.palette.bgr555"
_GENESIS_PRESET = "preset.palette.genesis-9bpp"

# Native palette sizes: NES PPU palette RAM is 32 one-byte master-palette
# indices; SNES CGRAM is 256 BGR555 colours; Genesis VDP CRAM is 64 9-bit
# colours (four 16-colour palettes).
_NES_ENTRIES = 32
_SNES_ENTRIES = 256
_GENESIS_ENTRIES = 64


class StateError(Exception):
    """The file isn't a recognised save state, or its palette can't be located.

    Carries a human-facing message for the status bar.
    """


@dataclass(frozen=True)
class PaletteRegion:
    """Where a state's palette lives, as pipeline-ready coordinates.

    ``count`` is the console's full palette size; the caller floors it to what
    actually fits the source, so a truncated state still yields the colours it
    does contain (and an offset past end-of-source yields none, cleanly).

    Usually ``offset`` indexes into the state file itself. When a format has to
    *extract* the palette (decompress a container, walk a memory image), it puts
    the located bytes in ``data`` and points ``offset`` into those instead — the
    caller feeds ``data`` through the pipeline in place of the file.
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
    chunk) — the difference between "not mine" and "mine but broken" drives the
    error message the user sees.
    """

    id: str
    name: str
    console: str
    locate: Callable[[bytes, str], PaletteRegion | None]


def _u32le(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 4], "little")


def _ext_matches(ext: str, prefix: str, lasts: str) -> bool:
    """A 3-char extension of ``prefix`` + one char from ``lasts`` (e.g. the
    save-slot family ``zs0``..``zs9``/``zst``)."""
    return len(ext) == 3 and ext.startswith(prefix) and ext[2] in lasts


def _locate_virtuanes(data: bytes, ext: str) -> PaletteRegion | None:
    # VirtuaNES states open with the ASCII tag "VirtuaNES"; the byte at 15 is the
    # state-format version, which fixes where the 32 bytes of PPU palette RAM sit.
    if not data.startswith(b"VirtuaNES"):
        return None
    if len(data) <= 15:
        raise StateError("VirtuaNES state is truncated (no version byte).")
    version = data[15]
    if version == 1:
        offset = 2440  # v1: a fixed layout.
    elif version == 2:
        # v2 prepends a 48-byte header then a variable RAM block whose length is
        # stored at byte 44; palette RAM follows the block (+16-byte tag) past
        # 2 KB of nametable data.
        if len(data) < 48:
            raise StateError("VirtuaNES state is truncated (no block size).")
        offset = 48 + _u32le(data, 44) + 16 + 2048
    else:
        raise StateError(f"Unsupported VirtuaNES state version {version}.")
    return PaletteRegion(offset, _NES_ENTRIES, _NES_PRESET)


def _locate_snes9x_gt(data: bytes, ext: str) -> PaletteRegion | None:
    # Snes9x-GT uses the chunked "GTSF" container; CGRAM is the chunk tagged with
    # the ASCII marker "PAL ", its bytes starting 8 in (past the 4-byte tag and a
    # 4-byte length field). Scanning for the tag keeps us version-independent.
    if not data.startswith(b"GTSF"):
        return None
    tag = data.find(b"PAL ")
    if tag < 0:
        raise StateError("Snes9x-GT state has no 'PAL ' (CGRAM) chunk.")
    return PaletteRegion(tag + 8, _SNES_ENTRIES, _SNES_PRESET)


def _locate_zsnes(data: bytes, ext: str) -> PaletteRegion | None:
    # ZSNES .zst/.zs0-.zs9 states have no signature we can rely on across
    # versions, so they are keyed by extension and read CGRAM at the fixed offset
    # the formats reference documents (docs/graphics-formats-reference, the
    # layout Tile Molester reads). Version-sensitive — hence the fallback tier.
    if not _ext_matches(ext, "zs", "t0123456789"):
        return None
    return PaletteRegion(1560, _SNES_ENTRIES, _SNES_PRESET)


def _locate_genesis(data: bytes, ext: str) -> PaletteRegion | None:
    # Gens/Kega/Genecyst .gs0-.gs9/.gsx states: VDP CRAM at a documented fixed
    # offset (as above). Extension-keyed fallback, version-sensitive.
    if not _ext_matches(ext, "gs", "x0123456789"):
        return None
    return PaletteRegion(274, _GENESIS_ENTRIES, _GENESIS_PRESET)


# --- Mesen / MesenCE -------------------------------------------------------
#
# A Mesen ".mss" state is a "MSS" header (emu + format versions, console type, a
# zlib-compressed screenshot, the ROM name) followed by the serialized machine.
# That serialized blob is a flat run of **labelled** records — a NUL-terminated
# ASCII key, a uint32 length, then the value — optionally zlib-compressed as a
# whole. So unlike the memory-image formats, CGRAM needs no scanning: it is the
# record keyed "ppu.cgram" (the console serializes under an empty prefix, its PPU
# under "ppu"). Reading it by name is robust to everything but a rename of that
# field. SNES only for now; other Mesen cores use different palette formats.

# ConsoleType enum order in the Mesen source; index by the header's console id.
_MESEN_CONSOLES = ("SNES", "Game Boy", "NES", "PC Engine", "SMS", "GBA", "WonderSwan")


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
        if console != 0:  # not SNES
            name = _MESEN_CONSOLES[console] if console < len(_MESEN_CONSOLES) else "?"
            raise StateError(f"Mesen {name} state: only SNES states are supported.")
        pos += 16  # video data: frame size, width, height, scale
        pos += 4 + _u32le(data, pos)  # compressed screenshot (length-prefixed)
        pos += 4 + _u32le(data, pos)  # ROM name (length-prefixed)
        blob = _mesen_state_blob(data[pos:])
    except IndexError as exc:
        raise StateError("Mesen state is truncated or unsupported.") from exc
    cgram = _mesen_record(blob, b"cgram", _SNES_ENTRIES * 2)
    if cgram is None:
        raise StateError("Mesen SNES state: no 'cgram' palette record found.")
    return PaletteRegion(0, _SNES_ENTRIES, _SNES_PRESET, data=cgram)


# Order matters: the signature-detected formats come first so they own their
# files before the extension-keyed fallbacks can claim them.
STATE_FORMATS: tuple[StateFormat, ...] = (
    StateFormat("mesen", "Mesen", "SNES", _locate_mesen),
    StateFormat("virtuanes", "VirtuaNES", "NES", _locate_virtuanes),
    StateFormat("snes9x-gt", "Snes9x-GT", "SNES", _locate_snes9x_gt),
    StateFormat("zsnes", "ZSNES", "SNES", _locate_zsnes),
    StateFormat("gens", "Gens / Kega / Genecyst", "Genesis", _locate_genesis),
)


def locate_palette(data: bytes, ext: str) -> tuple[StateFormat, PaletteRegion]:
    """Detect the emulator behind ``data`` and locate its palette.

    ``ext`` is the file's extension (with or without the leading dot, any case).
    Returns the matched :class:`StateFormat` and its :class:`PaletteRegion`.
    Raises :class:`StateError` if nothing matches, or a matched format's palette
    can't be located.
    """
    ext = ext.lower().lstrip(".")
    for fmt in STATE_FORMATS:
        region = fmt.locate(data, ext)
        if region is not None:
            return fmt, region
    raise StateError("Unrecognised save state — no known emulator format matched.")
