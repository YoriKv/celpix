"""Sega's 32-byte tile mask fills — per tile, a few repeated bytes placed by mask.

A tile codec from Sega's first year of Mega Drive titles. It has no published
name; this one describes it, the 32-byte sibling of ``capcom-mask8``. Each
32-byte tile is coded on its own::

    per tile, repeating:
      n            0..127: how many fill groups this tile has
                   bit 7 set: end of stream (the packer writes $FF)
      n x {        value, then a big-endian 32-bit mask:
        value      bit 31 is byte 0 of the tile, bit 0 byte 31 --
        mask       every set bit's byte takes the value
      }
      literals     one byte for every position no mask covered, in order

A tile with ``n = 0`` is 32 literal bytes, and a tile whose masks cover all 32
positions carries no literals at all. The decoder builds each tile whole in a
32-byte buffer before writing it, so nothing carries from one tile to the next:
there is no XOR, no history and no run across tiles. It suits art in few colours,
where a two-colour tile is 11 bytes and a four-colour one 16 at worst.

**Where it is used.** Phantasy Star II (1989) unpacks almost all of its art
through it — portraits, the title, every enemy, the effects, the field sprites —
in ``DecompressArt`` (``$005E8E``, to the VDP) and ``DecompressArt2`` (``$005F04``,
to RAM), and Super League / Tommy Lasorda Baseball (1989) is documented with the
same tile format. Two first-party titles of the same year suggest a Sega library
routine rather than one game's invention. See
``docs/graphics-formats-reference/implementation-guide.md`` §7.

**What Sega's packer chose, and what this encoder therefore does.** A byte value
is given a group exactly when it fills six or more of the tile's 32 bytes — a
group costs five bytes, so six is the first count at which it saves one — and the
groups are written in the order each value first appears in the tile. Encoding
that way reproduces 261 of Phantasy Star II's 262 streams byte for byte, so a
re-encode of untouched art is a no-op on the ROM.

**Data-side detection works only with that rule.** Uncompressed art full of zero
bytes parses as a run of ``n = 0`` literal tiles, so a bare decode says little; a
stream that also *re-encodes byte for byte* is real, and over shuffled ROM bytes
no such stream of eight or more tiles turns up. The structure scan bounds a
stream by its end byte, but finding one is better done from its loader.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo

TILE_BYTES = 32
END = 0xFF
END_FLAG = 0x80
GROUP_BYTES = 5
MIN_FILL = 6  # the count at which a group first beats five literal bytes
FULL = 0xFFFFFFFF


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Decode one stream: ``(output, consumed, complete)``.

    Read as the 68000 routine reads it, without its own checks: masks may overlap
    (a later group wins) and a value may repeat, since the cartridge's decoder
    tests neither. A partial decode stops at the last whole tile.
    """
    out = bytearray()
    pos = 0
    size = len(data)
    while True:
        if pos >= size:
            if partial:
                return bytes(out), pos, False
            raise ValueError("tile mask stream ends before its end byte")
        n = data[pos]
        if n & END_FLAG:
            return bytes(out), pos + 1, True
        start = pos
        pos += 1
        if pos + n * GROUP_BYTES > size:
            if partial:
                return bytes(out), start, False
            raise ValueError("tile mask stream ends inside a fill group")
        tile = bytearray(TILE_BYTES)
        filled = 0
        for _ in range(n):
            value = data[pos]
            mask = int.from_bytes(data[pos + 1 : pos + GROUP_BYTES], "big")
            pos += GROUP_BYTES
            filled |= mask
            for i in range(TILE_BYTES):
                if mask >> (31 - i) & 1:
                    tile[i] = value
        if pos + TILE_BYTES - bin(filled).count("1") > size:
            if partial:
                return bytes(out), start, False
            raise ValueError("tile mask stream ends inside a tile's literals")
        for i in range(TILE_BYTES):
            if not filled >> (31 - i) & 1:
                tile[i] = data[pos]
                pos += 1
        out += tile


def _encode_tile(tile: bytes) -> bytes:
    counts: dict[int, int] = {}
    for b in tile:
        counts[b] = counts.get(b, 0) + 1
    # dicts keep insertion order, which is each value's first appearance
    grouped = [v for v, c in counts.items() if c >= MIN_FILL]
    out = bytearray([len(grouped)])
    filled = 0
    for value in grouped:
        mask = 0
        for i, b in enumerate(tile):
            if b == value:
                mask |= 1 << (31 - i)
        filled |= mask
        out.append(value)
        out += mask.to_bytes(4, "big")
    if filled != FULL:
        out += bytes(b for i, b in enumerate(tile) if not filled >> (31 - i) & 1)
    return bytes(out)


def compress(data: bytes) -> bytes:
    if len(data) % TILE_BYTES:
        raise ValueError(
            f"the tile mask codec packs whole {TILE_BYTES}-byte tiles: "
            f"{len(data):,} bytes is not a multiple of {TILE_BYTES}"
        )
    out = bytearray()
    for at in range(0, len(data), TILE_BYTES):
        out += _encode_tile(data[at : at + TILE_BYTES])
    out.append(END)
    return bytes(out)


class SegaMask32Compression(PartialDecompression):
    info = PluginInfo(
        id="compression.sega-mask32",
        name="Sega 32-byte tile mask fills (Mega Drive)",
        stage=Stage.COMPRESSION,
        # the end byte after the last tile bounds the stream
        self_delimiting=True,
        category="Sega",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)
