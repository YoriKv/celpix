"""The "Sonic 2" tile codec — Aspect's Master System / Game Gear tile packer.

About 34 titles, all apparently developed by Aspect or Arc System Works: Sonic
the Hedgehog 2, Sonic Chaos, Sonic Triple Trouble, Sonic Blast, Deep Duck
Trouble, Legend of Illusion, Batman Returns and Baku Baku among them (the list
is in ``docs/graphics-formats-reference/implementation-guide.md`` §7). It packs
whole 32-byte interleaved 4bpp tiles, and its loader writes them straight to
VRAM, so the output is ``preset.pixel.sms-4bpp`` as decoded::

    +0  $01 $00        unused by the loader; every documented stream has it
    +2  u16 le         tile count
    +4  u16 le         offset of the type bitstream, from +0
    +6  raw data       read front to back as the tiles need it
    at the offset:     2 bits per tile, low pair first within each byte

    %00  32 zero bytes
    %01  32 raw bytes
    %10  masked: a 32-bit little-endian mask, bit i for output byte i
         (1 = the next raw byte, 0 = zero)
    %11  masked, then the XOR pass below

**The XOR pass is order-dependent.** Seven steps, ``j = 0, 2, … 12``, each
``t[j+2] ^= t[j]; t[j+3] ^= t[j+1]; t[j+18] ^= t[j+16]; t[j+19] ^= t[j+17]`` —
so every step XORs in a byte an earlier step already changed: a running XOR
along four chains of alternate bytes, the even and the odd ones of the tile's top
four rows and of its bottom four. It is meant to turn repeated bytes into zeros,
but alternate bytes of an interleaved row are *different* bitplanes (0 and 2, 1
and 3), which is why few tiles gain from it. The encoder undoes it by running the
same seven steps backwards (``j = 12 … 0``).

Edge cases, settled from the game's Z80 loader:

- **The header bytes are not read**, but this decoder requires ``$01 $00``:
  every stream the survey documents carries it, and without it any two bytes
  would be taken for a header by a scan.
- **A tile count of 0 is refused.** The loader decrements before testing, so it
  would draw 65,536 tiles — two megabytes into 16 KB of VRAM, never a real stream.
- **The mask is consumed low bit first**: the loader reads the four bytes into
  ``e, d, c, b`` and shifts ``b:c:d:e`` right, so byte 0's bit 0 is output byte 0.
- **The structure's extent is whichever of its two blocks ends last.** The
  loader reads the raw data forward from +6 and the bitstream forward from the
  offset, and nothing ties them together. In every stream laid out as documented
  the bitstream follows the data, so that is its end; the decoder reports the
  maximum either way.

**No plugin inputs.** The count, the layout and every tile's type are in the
stream, and the output is already interleaved, so nothing about decoding it lives
outside its own bytes.

The tile count field is the memory guard: at most 65,535 tiles, and a strict
decode checks the bitstream fits in the buffer before producing any of them.
Byte-identity with the original packer is a non-goal; round-tripping is the
contract. The encoder picks the cheapest of the four types per tile.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo

TILE_BYTES = 32
HEADER = b"\x01\x00"
HEADER_BYTES = 6
_MAX_WORD = 0xFFFF

ZERO, RAW, MASKED, XORED = range(4)


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt Sonic 2 tile stream: {reason}")


def _xor_pass(tile: bytearray, steps: range) -> None:
    for j in steps:
        tile[j + 2] ^= tile[j]
        tile[j + 3] ^= tile[j + 1]
        tile[j + 18] ^= tile[j + 16]
        tile[j + 19] ^= tile[j + 17]


_FORWARD = range(0, 14, 2)  # the loader's order
_BACKWARD = range(12, -1, -2)  # its inverse


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Decode one tile stream at ``data[0]``.

    Returns ``(output, consumed, complete)``. ``complete`` is true when every
    tile was decoded inside ``data``, making ``consumed`` the structure's true
    byte length. With ``partial`` a buffer that ends early yields the whole tiles
    decoded before it did; a bad header raises either way.
    """
    n = len(data)
    if n < HEADER_BYTES:
        raise _fail(f"shorter than the {HEADER_BYTES}-byte header")
    if data[:2] != HEADER:
        raise _fail(f"header starts {data[0]:02X} {data[1]:02X}, not 01 00")
    count = int.from_bytes(data[2:4], "little")
    if count == 0:
        raise _fail("tile count is 0")
    offset = int.from_bytes(data[4:6], "little")
    bits_end = offset + (count + 3) // 4
    if bits_end > n and not partial:
        raise _fail(
            f"type bitstream ends at {bits_end:#x}, past the {n:#x}-byte buffer"
        )

    out = bytearray()
    pos = HEADER_BYTES
    done = 0
    for t in range(count):
        at = offset + t // 4
        if at >= n:
            break
        kind = data[at] >> (2 * (t % 4)) & 3
        if kind == ZERO:
            out += bytes(TILE_BYTES)
        elif kind == RAW:
            if pos + TILE_BYTES > n:
                break
            out += data[pos : pos + TILE_BYTES]
            pos += TILE_BYTES
        else:
            if pos + 4 > n:
                break
            mask = int.from_bytes(data[pos : pos + 4], "little")
            take = mask.bit_count()
            if pos + 4 + take > n:
                break
            literals = iter(data[pos + 4 : pos + 4 + take])
            tile = bytearray(
                next(literals) if mask >> i & 1 else 0 for i in range(TILE_BYTES)
            )
            pos += 4 + take
            if kind == XORED:
                _xor_pass(tile, _FORWARD)
            out += tile
        done += 1

    complete = done == count
    if not complete and not partial:
        raise _fail(f"raw data ends after {done:,} of {count:,} tiles")
    consumed = max(pos, min(n, offset + (done + 3) // 4))
    return bytes(out), consumed, complete


def _masked(tile: bytes) -> bytes:
    mask = sum(1 << i for i, value in enumerate(tile) if value)
    return mask.to_bytes(4, "little") + bytes(value for value in tile if value)


def compress(data: bytes) -> bytes:
    """Encode whole 32-byte tiles, each as the cheapest of the four types.

    A tile of zeros costs nothing in the data block, a raw tile 32 bytes, and a
    masked one 4 plus its non-zero bytes — counted before and after undoing the
    XOR pass, whichever leaves fewer. Ties go to the simpler type.
    """
    if not data or len(data) % TILE_BYTES:
        raise ValueError(
            f"the Sonic 2 codec packs whole {TILE_BYTES}-byte tiles: "
            f"{len(data):,} bytes is not a positive whole number of them"
        )
    count = len(data) // TILE_BYTES
    if count > _MAX_WORD:
        raise ValueError(f"{count:,} tiles; the tile count field holds {_MAX_WORD:,}")

    body = bytearray()
    kinds: list[int] = []
    for start in range(0, len(data), TILE_BYTES):
        tile = data[start : start + TILE_BYTES]
        if not any(tile):
            kinds.append(ZERO)
            continue
        unxored = bytearray(tile)
        _xor_pass(unxored, _BACKWARD)
        options = (
            (TILE_BYTES, RAW, tile),
            (4 + TILE_BYTES - tile.count(0), MASKED, _masked(tile)),
            (4 + TILE_BYTES - unxored.count(0), XORED, _masked(unxored)),
        )
        _, kind, encoded = min(options, key=lambda option: option[:2])
        kinds.append(kind)
        body += encoded

    offset = HEADER_BYTES + len(body)
    if offset > _MAX_WORD:
        raise ValueError(
            f"the raw data ends at {offset:#x}, past the 16-bit bitstream offset"
        )
    out = bytearray(HEADER)
    out += count.to_bytes(2, "little") + offset.to_bytes(2, "little") + body
    for group in range(0, count, 4):
        out.append(
            sum(kind << (2 * i) for i, kind in enumerate(kinds[group : group + 4]))
        )
    return bytes(out)


class Sonic2TilesCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.sonic2-tiles",
        name='"Sonic 2" tiles (Aspect, Master System / Game Gear)',
        stage=Stage.COMPRESSION,
        # The tile count and the bitstream offset bound the structure exactly.
        self_delimiting=True,
        category="Sega",
    )
    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)
