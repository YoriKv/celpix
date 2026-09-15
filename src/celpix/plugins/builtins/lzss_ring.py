"""LZSS over a 4 KiB ring buffer, with a 32-bit uncompressed-size prefix.

The most widely copied LZSS framing in console software: a 4096-byte history
ring, matches of 3..18 bytes, and back-references that name an **absolute ring
position** rather than a distance. Sega CD/Saturn/Dreamcast titles wrap
compressed asset archives in it, and the same stream grammar turns up throughout
the era wherever a decompressor was lifted from the reference publication of the
scheme (details and provenance in
``docs/graphics-formats-reference/implementation-guide.md`` §7).

Stream shape::

    uint32 le   uncompressed size, in bytes
    then, repeating:
      flags byte      8 op selectors, LSB first; 1 = literal, 0 = back-reference
      literal:        1 byte, copied out and into the ring
      back-reference: 2 bytes  b0, b1
                      ring position = b0 | ((b1 & 0xF0) << 4)   (12 bits)
                      length        = (b1 & 0x0F) + 3           (3..18)

The ring starts zero-filled with its write cursor at **0xFEE**, so output byte
*i* lands at ring position ``(0xFEE + i) & 0xFFF``. That offset is not
decoration: a reference reaching back past the start of the output reads the
zero fill, which some encoders lean on for leading runs. The decoder here keeps a
4096-byte zero prefix in front of the output instead of a live ring, which is the
same window seen from the other side and spares a modulo per byte.

**The size prefix is the terminator.** The stream carries no end marker, so a
decode stops when it has produced exactly that many bytes; the source position it
stopped at is the structure's true compressed length. That makes the scheme
self-delimiting even though the compressed body is not.

Other lineages wrap the same body differently, so :func:`decompress` and
:func:`compress` take the prefix's width and byte order and the ring's size as
keywords, defaulting to this framing. Namco's Mega Drive LZSS and its 2 KiB-ring
variant are that (:mod:`~celpix.plugins.builtins.namco_lz`).

The compressor is a greedy parse with a one-step lazy deferral, over a
3-byte-prefix index of recent positions. Byte-identity with a particular
original blob is a non-goal; round-tripping is the contract.
"""

from __future__ import annotations

from typing import Literal

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo
from celpix.plugins.builtins._lz import (
    FlagGroup,
    MatchFinder,
    copy_from,
    parse_greedy,
)

SIZE_BYTES = 4
RING_SIZE = 4096

MIN_MATCH = 3
MAX_MATCH = 18  # 4-bit length field, biased by MIN_MATCH


def _ring_start(ring_size: int) -> int:
    """Where the ring's write cursor sits before the first output byte.

    0xFEE for the 4 KiB ring: the reference implementation's way of leaving the
    encoder's lookahead room ahead of the cursor, which carries over to a smaller
    ring as the same distance from its end.
    """
    return ring_size - MAX_MATCH

# Compressor tuning: how many recent positions sharing a 3-byte prefix to test
# (see :class:`~celpix.plugins.builtins._lz.MatchFinder`).
_MAX_CANDIDATES = 96


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt LZSS stream: {reason}")


def decompress(
    data: bytes,
    *,
    partial: bool = False,
    size_bytes: int = SIZE_BYTES,
    byteorder: Literal["little", "big"] = "little",
    ring_size: int = RING_SIZE,
) -> tuple[bytes, int, bool]:
    """Decode a size-prefixed LZSS stream.

    Returns ``(output, consumed, complete)``. ``complete`` is true when the full
    declared size was produced, making ``consumed`` the structure's true byte
    length — the slot a save-back must fit. With ``partial`` a buffer that ends
    mid-stream yields the prefix decoded so far instead of raising, which is what
    a bounded view window needs; a structurally invalid stream still raises.

    The keywords are the framings other lineages put around the same body: the
    size prefix's width and byte order, and a smaller ring
    (:mod:`~celpix.plugins.builtins.namco_lz`).
    """
    if len(data) < size_bytes:
        raise _fail(f"shorter than the {size_bytes}-byte size prefix")
    target = int.from_bytes(data[:size_bytes], byteorder)
    if target == 0:
        raise _fail("declared uncompressed size is zero")

    ring_mask = ring_size - 1
    ring_start = _ring_start(ring_size)
    # `win` is the output preceded by the ring's zero fill, so a reference that
    # reaches back before the first output byte reads zeros exactly as the ring
    # would. Output byte i is win[ring_size + i].
    win = bytearray(ring_size)
    src = size_bytes
    n = len(data)
    produced = 0

    while produced < target and src < n:
        flags = data[src]
        src += 1
        for bit in range(8):
            if produced >= target:
                break
            if flags & (1 << bit):
                if src >= n:
                    break
                win.append(data[src])
                src += 1
                produced += 1
                continue

            if src + 1 >= n:
                break
            # A ring narrower than the 12-bit field ignores the field's top bits.
            ring_pos = (data[src] | ((data[src + 1] & 0xF0) << 4)) & ring_mask
            length = min((data[src + 1] & 0x0F) + MIN_MATCH, target - produced)
            src += 2
            # Resolve the absolute ring position to the output position it names:
            # the one in the last ring_size bytes congruent to it modulo the ring.
            base = produced - ring_size
            start = ring_size + base + ((ring_pos - ring_start - base) % ring_size)
            copy_from(win, start, length)
            produced += length

    out = bytes(win[ring_size:])
    complete = len(out) == target
    if not complete and not partial:
        raise _fail(f"source ended after {len(out):,} of {target:,} bytes")
    return out, src, complete


# -- compression ------------------------------------------------------------


def compress(
    data: bytes,
    *,
    size_bytes: int = SIZE_BYTES,
    byteorder: Literal["little", "big"] = "little",
    ring_size: int = RING_SIZE,
    window: int | None = None,
) -> bytes:
    """Encode raw bytes into a size-prefixed LZSS stream.

    ``window`` caps the distance a reference may reach, below ``ring_size``, for
    a framing whose true ring size is in doubt: see
    :mod:`~celpix.plugins.builtins.namco_lz`.
    """
    n = len(data)
    if n >= 1 << (size_bytes * 8):
        raise ValueError(
            f"input is {n:,} bytes; the {size_bytes * 8}-bit LZSS size prefix "
            f"holds {(1 << (size_bytes * 8)) - 1:,}"
        )

    ring_start = _ring_start(ring_size)
    # The longest match wins outright here: a back-reference costs two bytes
    # whatever its distance, so there is nothing to trade off against length.
    finder = MatchFinder(
        data,
        min_match=MIN_MATCH,
        window=window or ring_size,
        max_candidates=_MAX_CANDIDATES,
    )

    out = bytearray(n.to_bytes(size_bytes, byteorder))
    # LSB first, and a set bit is the *literal* — both the opposite way round from
    # the BIOS LZ77 next door (:mod:`~celpix.plugins.builtins.gba_lz77`).
    group = FlagGroup(out, msb_first=False, set_means_match=False)
    for pos, length, candidate in parse_greedy(
        data, finder, min_match=MIN_MATCH, max_match=MAX_MATCH
    ):
        group.select(length > 0)
        if length:
            ring_pos = (ring_start + candidate) & (ring_size - 1)
            out.append(ring_pos & 0xFF)
            out.append(((ring_pos >> 4) & 0xF0) | (length - MIN_MATCH))
        else:
            out.append(data[pos])
    group.finish()

    return bytes(out)


class LzssRingCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.lzss-ring",
        name="LZSS (4 KiB ring, size-prefixed)",
        stage=Stage.COMPRESSION,
        # The body has no end marker, but the 32-bit size prefix bounds it, so a
        # decode does know where the structure ends.
        self_delimiting=True,
        category="Generic",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)
