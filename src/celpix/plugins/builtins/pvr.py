"""PowerVR (`PVRT`) textures — header, twiddling, VQ and the LZSS wrapper.

The texture container Dreamcast-era titles ship graphics in. This is a
**Compression-stage** plugin rather than a container plus a reshape, because a
PVR texture is *structure-scoped*, not region-scoped: the header declares its own
byte length, so one blob can hold several textures of differing sizes and
layouts, and each is its own structure. Setting ``KEY_COMPRESSED_SIZE`` from that
length is what lets the host's structure scan and Jump-to-Next walk from one
texture to the next (``docs/graphics-formats-reference/implementation-guide.md``
§7).

A chunk is an optional `GBIX` global-index block followed by::

    +0   4  "PVRT"
    +4   4  length_field   -> chunk ends at pvrt + 8 + length_field
    +8   1  pixel format   (ARGB1555 / RGB565 / ARGB4444 / YUV422 / bump /
    +9   1  data format     RGB555 / YUV420 / ARGB8888)
    +10  2  reserved       (twiddled / VQ / palettised / rectangle / stride...)
    +12  2  width
    +14  2  height
    +16  .. payload

**Output is the payload de-twiddled into linear row-major order, in its own
texel format** — never converted to a canonical one. Round-tripping a texture
has to be byte-exact, and normalising to ARGB8888 would re-quantise on the way
back out. So the view still needs the matching pixel preset chosen by hand; the
decode records a notice naming it, since a codec is handed a fresh context
(``PixelCodecPlugin.decode`` is buffer-relative and stateless) and cannot be told
what it is looking at.

**Twiddling** is Morton order over square blocks: `side = min(width, height)`,
the texture is a row (or column) of `side x side` blocks along its longer axis,
and within a block the texel index interleaves the low bits of x and y. Both
dimensions must be powers of two. A rectangle's aspect therefore changes the
address map, not just its extent.

**The LZSS wrapper is folded in.** A "CPR" texture is the same chunk behind the
size-prefixed ring LZSS (:mod:`~celpix.plugins.builtins.lzss_ring`), and the host
has one Compression slot per pathway, so the two cannot be stacked. The decode
sniffs for the wrapper and unwraps it first; ``KEY_COMPRESSED_SIZE`` then reports
the *compressed* member's length, which is the slot a save-back has to fit.

**What round-trips.** Twiddled, linear/stride and palettised (PAL4/PAL8)
textures re-encode. VQ is decode-only for now — rebuilding a codebook is a
quantisation problem, not a byte transform — and mipmapped textures expose their
**base level** only. Both stay editable: the whole original chunk is kept on the
context and the re-encoded base level is spliced back into it, so the GBIX, the
header, the smaller mip levels and a VQ codebook all survive untouched.
"""

from __future__ import annotations

import struct
from functools import lru_cache

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.core.notices import inform, warn
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins import lzss_ring

PLUGIN_ID = "compression.pvr"

# bytes: the whole chunk exactly as it was read (GBIX + PVRT + payload), kept so
# a save can splice the edited level back in and leave everything it did not
# decode — header, mip chain, VQ codebook — byte-identical.
KEY_PVR_CHUNK = "pvr.chunk"
# dict: the layout the decode resolved. Read back by `compress`, so the save side
# never re-derives what the load already worked out.
KEY_PVR_LAYOUT = "pvr.layout"
# str: "none" or "lzss" — whether the chunk was behind the CPR wrapper.
KEY_PVR_WRAPPER = "pvr.wrapper"

_HEADER_SIZE = 16
_GBIX_SIZES = (16, 12)  # standard, and the trimmed form some titles ship

PIXEL_FORMATS = {
    0x00: "argb1555",
    0x01: "rgb565",
    0x02: "argb4444",
    0x03: "yuv422",
    0x04: "bump",
    0x05: "rgb555",
    0x06: "yuv420",
    0x07: "argb8888",
}

DATA_FORMATS = {
    0x01: "twiddled",
    0x02: "twiddled + mipmaps",
    0x03: "vq",
    0x04: "vq + mipmaps",
    0x05: "pal4 twiddled",
    0x06: "pal4 twiddled + mipmaps",
    0x07: "pal8 twiddled",
    0x08: "pal8 twiddled + mipmaps",
    0x09: "linear rectangle",
    0x0A: "linear rectangle + mipmaps",
    0x0B: "linear stride",
    0x0C: "linear stride + mipmaps",
    0x0D: "twiddled rectangle",
    0x0E: "abgr8888",
    0x0F: "abgr8888 + mipmaps",
    0x10: "small vq",
    0x11: "small vq + mipmaps",
    0x12: "twiddled alias + mipmaps",
}

MIPMAPPED_FORMATS = frozenset({0x02, 0x04, 0x06, 0x08, 0x0A, 0x0C, 0x0F, 0x11, 0x12})
VQ_FORMATS = frozenset({0x03, 0x04, 0x10, 0x11})
INDEXED_FORMATS = frozenset({0x05, 0x06, 0x07, 0x08})
PAL4_FORMATS = frozenset({0x05, 0x06})
TWIDDLED_FORMATS = frozenset({0x01, 0x02, 0x05, 0x06, 0x07, 0x08, 0x0D, 0x12})
LINEAR_FORMATS = frozenset({0x09, 0x0A, 0x0B, 0x0C})
# Stored linearly despite naming a 32-bit texel — not in either set above.
ABGR_FORMATS = frozenset({0x0E, 0x0F})

# Which shipped preset reads this plugin's output, per pixel format. Named in a
# notice because the codec cannot be told directly.
_PRESET_FOR_PIXEL_FORMAT = {
    0x00: "Direct ARGB1555 (8x8)",
    0x01: "Direct RGB565 (8x8)",
    0x02: "Direct ARGB4444 (8x8)",
    0x05: "Direct RGB555 (8x8)",
    0x07: "Direct ARGB8888 (8x8)",
}


def _fail(reason: str) -> ValueError:
    return ValueError(f"PVR: {reason}")


def _is_power_of_two(value: int) -> bool:
    return value > 0 and value & (value - 1) == 0


# -- twiddling ---------------------------------------------------------------


@lru_cache(maxsize=32)
def _twiddle_map(width: int, height: int) -> tuple[int, ...]:
    """Linear position -> stored position, for a whole twiddled texture.

    Built once per size and cached: a texture is decoded on every view refresh,
    and recomputing a million Morton indices per repaint is the difference
    between instant and unusable. The bit-spread table costs `side` entries and
    turns each index into two lookups and an or.
    """
    if not (_is_power_of_two(width) and _is_power_of_two(height)):
        raise _fail(
            f"twiddled textures must have power-of-two dimensions; got {width}x{height}"
        )
    side = min(width, height)
    spread = [0] * side
    for value in range(side):
        bits = 0
        for bit in range(value.bit_length()):
            bits |= ((value >> bit) & 1) << (2 * bit)
        spread[value] = bits

    block = side * side
    wide = width >= height
    out = [0] * (width * height)
    for y in range(height):
        row = y * width
        # The long axis is cut into `side`-wide square blocks; Morton order runs
        # inside one block, and whole blocks follow each other in sequence.
        tile = 0 if wide else (y // side) * block
        odd_bits = spread[y if wide else y % side] << 1
        for x in range(width):
            if wide:
                tile = (x // side) * block
            out[row + x] = tile + (spread[x % side if wide else x] | odd_bits)
    return tuple(out)


def untwiddle(values: list[int], width: int, height: int) -> list[int]:
    """Stored (Morton) order -> linear row-major order."""
    stored = values
    return [stored[i] for i in _twiddle_map(width, height)]


def twiddle(values: list[int], width: int, height: int) -> list[int]:
    """Linear row-major order -> stored (Morton) order."""
    out = [0] * (width * height)
    for linear, target in enumerate(_twiddle_map(width, height)):
        out[target] = values[linear]
    return out


# -- header ------------------------------------------------------------------


class PvrChunk:
    """One parsed `PVRT` chunk: where it sits, what it declares, and its payload."""

    __slots__ = (
        "start",
        "pvrt",
        "end",
        "pixel_format",
        "data_format",
        "width",
        "height",
        "payload_start",
    )

    def __init__(self, data: bytes, start: int, pvrt: int) -> None:
        length_field = int.from_bytes(data[pvrt + 4 : pvrt + 8], "little")
        self.start = start
        self.pvrt = pvrt
        self.end = pvrt + 8 + length_field
        self.pixel_format = data[pvrt + 8]
        self.data_format = data[pvrt + 9]
        self.width = int.from_bytes(data[pvrt + 12 : pvrt + 14], "little")
        self.height = int.from_bytes(data[pvrt + 14 : pvrt + 16], "little")
        self.payload_start = pvrt + _HEADER_SIZE

    @property
    def is_mipmapped(self) -> bool:
        return self.data_format in MIPMAPPED_FORMATS

    @property
    def is_vq(self) -> bool:
        return self.data_format in VQ_FORMATS

    @property
    def is_indexed(self) -> bool:
        return self.data_format in INDEXED_FORMATS

    @property
    def is_pal4(self) -> bool:
        return self.data_format in PAL4_FORMATS

    @property
    def texel_bytes(self) -> int:
        """Bytes per stored texel, for the non-indexed non-VQ layouts."""
        if self.data_format in ABGR_FORMATS or self.pixel_format == 0x07:
            return 4
        return 2

    def describe(self) -> str:
        pixel = PIXEL_FORMATS.get(self.pixel_format, f"0x{self.pixel_format:02X}")
        if self.is_indexed:
            pixel = "pal4" if self.is_pal4 else "pal8"
        data = DATA_FORMATS.get(self.data_format, f"0x{self.data_format:02X}")
        return f"{self.width}x{self.height} {pixel} / {data}"


def find_chunk(data: bytes) -> PvrChunk:
    """Parse the chunk at the start of ``data``, stepping over a `GBIX` if present.

    Only the leading chunk: a later `PVRT` in the same buffer is the *next*
    structure, which the host reaches by advancing past this one's recorded size,
    not something this decode should silently skip to.
    """
    if data[:4] == b"PVRT":
        pvrt, start = 0, 0
    elif data[:4] == b"GBIX":
        declared = int.from_bytes(data[4:8], "little")
        pvrt = 8 + declared
        if data[pvrt : pvrt + 4] != b"PVRT" or 8 + declared not in _GBIX_SIZES:
            raise _fail("GBIX block is not followed by a PVRT header")
        start = 0
    else:
        raise _fail("data does not begin with a PVRT or GBIX header")
    if len(data) < pvrt + _HEADER_SIZE:
        raise _fail("buffer ends inside the PVRT header")
    chunk = PvrChunk(data, start, pvrt)
    if chunk.width == 0 or chunk.height == 0:
        raise _fail(f"header declares a {chunk.width}x{chunk.height} texture")
    return chunk


# -- payload geometry --------------------------------------------------------


def _level_bytes(chunk: PvrChunk, width: int, height: int) -> int:
    """Stored byte size of one mip level at ``width`` x ``height``."""
    pixels = width * height
    if chunk.is_vq:
        return max(1, width // 2) * max(1, height // 2)
    if chunk.is_indexed:
        return (pixels + 1) // 2 if chunk.is_pal4 else pixels
    return pixels * chunk.texel_bytes


def _vq_codebook_bytes(chunk: PvrChunk, payload_len: int) -> int:
    """Size of the codebook at the front of a VQ payload.

    Full VQ is a fixed 256 entries. Small VQ scales its codebook with the
    texture, and for the non-mipmapped case the payload's own size gives it away
    exactly (everything that is not the index map); mipmapped payloads carry the
    smaller levels in between, so there the entry count is derived instead.
    """
    blocks = max(1, chunk.width // 2) * max(1, chunk.height // 2)
    if chunk.data_format in (0x03, 0x04):
        return 256 * 8
    if chunk.data_format == 0x10:
        remainder = payload_len - blocks
        if remainder > 0 and remainder % 8 == 0:
            return remainder
    return min(256, max(1, (chunk.width * chunk.height) // 32)) * 8


def base_level_range(chunk: PvrChunk, payload_len: int) -> tuple[int, int]:
    """Where the **base** (full-size) level sits inside the payload.

    Mip chains are stored **smallest first**, so the full-size level occupies the
    payload's tail, not its head — a decoder reading forward from zero gets the
    1x1 level. For VQ the codebook sits at the front and the index map at the
    back, which is why the floor is the codebook's end rather than zero.
    """
    if not chunk.is_mipmapped:
        start = _vq_codebook_bytes(chunk, payload_len) if chunk.is_vq else 0
        return start, payload_len
    floor = _vq_codebook_bytes(chunk, payload_len) if chunk.is_vq else 0
    size = _level_bytes(chunk, chunk.width, chunk.height)
    start = payload_len - size
    if start < floor:
        raise _fail("mipmapped payload does not hold a complete base level")
    return start, payload_len


# -- decode ------------------------------------------------------------------


def _unpack_texels(payload: bytes, count: int, texel_bytes: int) -> list[int]:
    code = "I" if texel_bytes == 4 else "H"
    return list(struct.unpack_from(f"<{count}{code}", payload, 0))


def _pack_texels(values: list[int], texel_bytes: int) -> bytes:
    code = "I" if texel_bytes == 4 else "H"
    return struct.pack(f"<{len(values)}{code}", *values)


def _unpack_indices(payload: bytes, count: int, pal4: bool) -> list[int]:
    if not pal4:
        return list(payload[:count])
    out: list[int] = []
    for byte in payload[: (count + 1) // 2]:
        out.append(byte & 0x0F)  # low nibble is the earlier pixel
        out.append(byte >> 4)
    return out[:count]


def _pack_indices(values: list[int], pal4: bool) -> bytes:
    if not pal4:
        return bytes(values)
    out = bytearray((len(values) + 1) // 2)
    for i in range(0, len(values), 2):
        high = values[i + 1] if i + 1 < len(values) else 0
        out[i // 2] = (values[i] & 0x0F) | ((high & 0x0F) << 4)
    return bytes(out)


def _decode_vq(chunk: PvrChunk, payload: bytes) -> bytes:
    """Expand a VQ index map through its codebook into full 16-bit texels.

    Each codebook entry is one 2x2 texel block stored TL, TR, BL, BR; the index
    map carries one byte per block, itself twiddled over the half-size block
    grid.
    """
    codebook_bytes = _vq_codebook_bytes(chunk, len(payload))
    block_w = max(1, chunk.width // 2)
    block_h = max(1, chunk.height // 2)
    blocks = block_w * block_h
    if len(payload) < codebook_bytes + blocks:
        raise _fail(
            f"VQ payload holds {len(payload)} bytes; "
            f"{codebook_bytes + blocks} are required"
        )
    entries = codebook_bytes // 8
    codebook = struct.unpack_from(f"<{entries * 4}H", payload, 0)
    indices = untwiddle(list(payload[-blocks:]), block_w, block_h)

    out = [0] * (chunk.width * chunk.height)
    for block in range(blocks):
        entry = indices[block] * 4
        if entry + 4 > len(codebook):
            raise _fail(
                f"VQ index {indices[block]} is outside a {entries}-entry codebook"
            )
        x = (block % block_w) * 2
        y = (block // block_w) * 2
        top = y * chunk.width + x
        out[top] = codebook[entry]
        out[top + 1] = codebook[entry + 1]
        bottom = top + chunk.width
        out[bottom] = codebook[entry + 2]
        out[bottom + 1] = codebook[entry + 3]
    return _pack_texels(out, 2)


def decode_chunk(data: bytes, chunk: PvrChunk) -> bytes:
    """The base level's texels, de-twiddled into linear row-major order."""
    payload = data[chunk.payload_start : chunk.end]
    count = chunk.width * chunk.height

    if chunk.is_vq:
        # VQ spans the whole payload: codebook at the front, base index map at
        # the back, so it is not a slice of one level.
        return _decode_vq(chunk, payload)

    start, end = base_level_range(chunk, len(payload))
    level = payload[start:end]

    if chunk.pixel_format == 0x06 and not chunk.is_indexed:
        raise _fail(
            "YUV420 textures are planar and chroma-subsampled; not supported "
            "(no celPix codec reads them)"
        )

    if chunk.is_indexed:
        needed = _level_bytes(chunk, chunk.width, chunk.height)
        if len(level) < needed:
            raise _fail(f"indexed payload holds {len(level)} bytes; {needed} required")
        values = _unpack_indices(level, count, chunk.is_pal4)
        return _pack_indices(
            untwiddle(values, chunk.width, chunk.height), chunk.is_pal4
        )

    texel_bytes = chunk.texel_bytes
    needed = count * texel_bytes
    if len(level) < needed:
        raise _fail(f"payload holds {len(level)} bytes; {needed} are required")
    values = _unpack_texels(level, count, texel_bytes)
    if chunk.data_format in TWIDDLED_FORMATS:
        values = untwiddle(values, chunk.width, chunk.height)
    elif (
        chunk.data_format not in LINEAR_FORMATS
        and chunk.data_format not in ABGR_FORMATS
    ):
        raise _fail(f"unhandled data format 0x{chunk.data_format:02X}")
    return _pack_texels(values, texel_bytes)


def encode_level(data: bytes, layout: dict) -> bytes:
    """The inverse of :func:`decode_chunk`: linear texels back to stored order."""
    width, height = layout["width"], layout["height"]
    count = width * height
    if layout["vq"]:
        raise _fail(
            "VQ textures are view-only: rebuilding a codebook needs "
            "quantisation, which this plugin does not do"
        )
    if layout["indexed"]:
        values = _unpack_indices(data, count, layout["pal4"])
        if len(values) < count:
            raise _fail(f"edited buffer holds {len(values)} indices; {count} required")
        return _pack_indices(twiddle(values, width, height), layout["pal4"])

    texel_bytes = layout["texel_bytes"]
    if len(data) < count * texel_bytes:
        raise _fail(
            f"edited buffer holds {len(data)} bytes; {count * texel_bytes} required"
        )
    values = _unpack_texels(data, count, texel_bytes)
    if layout["twiddled"]:
        values = twiddle(values, width, height)
    return _pack_texels(values, texel_bytes)


# -- wrapper -----------------------------------------------------------------


# Ceiling on a wrapper's declared size, so a buffer of noise whose first four
# bytes read as a huge length is rejected before anything tries to expand it.
_MAX_UNWRAPPED = 0x4000000


def _unwrap(data: bytes) -> tuple[bytes, int] | None:
    """``(chunk bytes, source bytes consumed)`` if ``data`` is a wrapped chunk.

    A chunk announces itself in its first four bytes, so anything else is a
    candidate for the wrapper. The test is then to **expand it and look for the
    magic** rather than to reason about sizes: a texture that happens not to
    compress leaves a stream no larger than its contents, so the "declared size
    exceeds the buffer" shortcut would quietly refuse to open it. Expanding
    non-LZSS bytes fails fast and cheaply, and the magic check is what actually
    settles it.
    """
    if data[:4] in (b"PVRT", b"GBIX") or len(data) < 8:
        return None
    declared = int.from_bytes(data[0:4], "little")
    if not _HEADER_SIZE < declared <= _MAX_UNWRAPPED:
        return None
    try:
        raw, consumed, complete = lzss_ring.decompress(data)
    except ValueError:
        return None
    if not complete or raw[:4] not in (b"PVRT", b"GBIX"):
        return None
    return raw, consumed


class PvrCompression:
    info = PluginInfo(
        id=PLUGIN_ID,
        name="PVR texture (PowerVR, incl. LZSS-wrapped)",
        stage=Stage.COMPRESSION,
        # The PVRT header declares the chunk's own byte length.
        self_delimiting=True,
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        unwrapped = _unwrap(data)
        wrapper = "lzss" if unwrapped else "none"
        chunk_bytes, consumed = unwrapped if unwrapped else (data, None)

        chunk = find_chunk(chunk_bytes)
        if chunk.end > len(chunk_bytes):
            raise _fail(
                f"header declares {chunk.end - chunk.start} bytes but only "
                f"{len(chunk_bytes) - chunk.start} are available"
            )

        out = decode_chunk(chunk_bytes, chunk)
        # Where an edited level goes back. Resolved here rather than on save, so
        # the arithmetic runs once against the bytes it was read from. VQ has no
        # single level slot, hence the empty one.
        payload_len = chunk.end - chunk.payload_start
        level = (0, 0) if chunk.is_vq else base_level_range(chunk, payload_len)

        ctx.set(KEY_PVR_CHUNK, chunk_bytes[: chunk.end])
        ctx.set(KEY_PVR_WRAPPER, wrapper)
        ctx.set(
            KEY_PVR_LAYOUT,
            {
                "width": chunk.width,
                "height": chunk.height,
                "pixel_format": chunk.pixel_format,
                "data_format": chunk.data_format,
                "texel_bytes": chunk.texel_bytes,
                "twiddled": chunk.data_format in TWIDDLED_FORMATS,
                "indexed": chunk.is_indexed,
                "pal4": chunk.is_pal4,
                "vq": chunk.is_vq,
                "payload_start": chunk.payload_start,
                "level_start": level[0],
                "level_end": level[1],
            },
        )
        # For a wrapped chunk the structure's extent in the *file* is the
        # compressed member, not the chunk it expands to.
        ctx.set(KEY_COMPRESSED_SIZE, consumed if consumed is not None else chunk.end)
        ctx.set(KEY_DECOMPRESS_COMPLETE, True)
        self._describe(ctx, chunk, wrapper)
        return out

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        layout = ctx.get(KEY_PVR_LAYOUT)
        original = ctx.get(KEY_PVR_CHUNK)
        if not isinstance(layout, dict) or not isinstance(original, (bytes, bytearray)):
            raise _fail(
                "no PVR header on this pathway: a texture can only be written "
                "back through the chunk it was loaded from"
            )
        payload_start = layout["payload_start"]
        start, end = layout["level_start"], layout["level_end"]
        rebuilt = bytearray(original)
        # Splice the edited level into the chunk as it was read, so the GBIX,
        # the header, the smaller mip levels and any VQ codebook stay untouched.
        level = encode_level(bytes(data), layout)
        if len(level) != end - start:
            raise _fail(
                f"re-encoded level is {len(level)} bytes; the slot in the "
                f"chunk is {end - start}"
            )
        rebuilt[payload_start + start : payload_start + end] = level
        out = bytes(rebuilt)
        if ctx.get(KEY_PVR_WRAPPER) == "lzss":
            return lzss_ring.compress(out)
        return out

    def _describe(self, ctx: PipelineContext, chunk: PvrChunk, wrapper: str) -> None:
        """Record what the view is showing and which preset reads it."""
        wrapped = " (LZSS-wrapped)" if wrapper == "lzss" else ""
        if chunk.is_indexed:
            reads = (
                "Pair with a packed 4bpp preset (low nibble first)"
                if chunk.is_pal4
                else "Pair with an 8bpp chunky preset"
            )
        elif chunk.data_format in ABGR_FORMATS:
            reads = "Select the 'Direct ABGR8888 (8x8)' pixel preset"
        else:
            preset = _PRESET_FOR_PIXEL_FORMAT.get(chunk.pixel_format)
            reads = (
                f"Select the '{preset}' pixel preset"
                if preset
                else "No shipped pixel preset reads this texel format"
            )
        inform(
            ctx,
            f"PVR texture: {chunk.describe()}{wrapped}",
            f"Decoded to linear order in its own texel format.\n{reads}.",
            source=PLUGIN_ID,
        )
        if chunk.is_mipmapped:
            warn(
                ctx,
                "Only the base mip level is shown",
                "The smaller levels are preserved on save but not editable.",
                source=PLUGIN_ID,
            )
        if chunk.is_vq:
            warn(
                ctx,
                "VQ texture is view-only",
                "Rebuilding a VQ codebook needs quantisation; saving will fail.",
                source=PLUGIN_ID,
            )
