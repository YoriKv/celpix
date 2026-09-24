"""Saxman — the ring LZSS behind a compressed-size header.

Sonic the Hedgehog 2 packs its Z80 sound driver and its music this way. The
body is exactly the 4 KiB ring LZSS of :mod:`~celpix.plugins.builtins.lzss_ring`
(flag bytes LSB first with a set bit for a literal, 12-bit absolute ring
positions, a ring whose cursor starts at 0xFEE); only the frame differs::

    +0  u16 LITTLE-endian   size of the body that follows, in bytes
    +2  body                flag groups and ops, with no end marker

Three things set the two apart, and a decoder built for one misreads the other
without complaint:

- **The header counts compressed bytes, not decoded ones.** Decoding stops when
  the source position reaches it, wherever the output has got to — so the
  final flag byte's unused bits are never examined, and nothing says how large
  the output will be until it is done.
- **It is two bytes where the ring LZSS prefix is four.** Read with the wrong
  width, a Saxman header runs into its first flag byte and still produces a
  plausible size.
- **A reference to before the first output byte is a run of zeros for its
  whole length**, even where it would reach into output that exists by the
  time the copy ends. Both known decoders test only where the copy *starts*,
  so a straddling reference is a zero fill, not a ring read — the one place
  the body grammar is not the ring LZSS's after all.

The sound driver itself is stored with no header at all: the loader is handed
the size in code. That is this body with no frame, and it wants the size
supplied from outside — not something a self-delimiting plugin can find.

Format detail and provenance are in
``docs/graphics-formats-reference/implementation-guide.md`` §7.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo

from ._lz import copy_back
from .lzss_ring import MIN_MATCH, RING_SIZE, compress_body

HEADER_BYTES = 2
SIZE_LIMIT = 0xFFFF
RING_START = 0xFEE


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt Saxman stream: {reason}")


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Unpack one Saxman stream from ``data[0]``.

    Returns ``(plain, consumed, complete)``. ``complete`` is true once the
    source position reaches the header's size; with ``partial`` a buffer that
    ends first yields what it decoded so far instead of raising.
    """
    if len(data) < HEADER_BYTES:
        raise _fail(f"shorter than the {HEADER_BYTES}-byte size header")
    end = HEADER_BYTES + int.from_bytes(data[:HEADER_BYTES], "little")
    limit = min(end, len(data))

    out = bytearray()
    src = HEADER_BYTES
    flags = left = 0
    while src < limit:
        if not left:
            flags, left = data[src], 8
            src += 1
            if src >= limit:
                break  # a flag byte with no op after it; nobody reads its bits
        literal = flags & 1
        flags >>= 1
        left -= 1
        if literal:
            out.append(data[src])
            src += 1
            continue
        if src + 1 >= limit:
            break
        low, high = data[src], data[src + 1]
        src += 2
        length = (high & 0x0F) + MIN_MATCH
        ring_pos = low | ((high & 0xF0) << 4)
        # The output position in the last ring's width congruent to the ring
        # position: output byte i sits at ring position RING_START + i.
        distance = (len(out) - ring_pos + RING_START) % RING_SIZE or RING_SIZE
        if distance > len(out):
            out += bytes(length)
        else:
            copy_back(out, distance, length)

    if src == end:
        return bytes(out), src, True
    if limit == end:
        raise _fail(f"its last op runs past the {end - HEADER_BYTES:,}-byte body")
    if not partial:
        raise _fail(f"source ended after {len(out):,} bytes")
    return bytes(out), src, False


def compress(data: bytes) -> bytes:
    """Encode ``data`` as one Saxman stream: the ring LZSS body, sized."""
    body = compress_body(data)
    if len(body) > SIZE_LIMIT:
        raise ValueError(
            f"input compresses to {len(body):,} bytes; the Saxman size header "
            f"holds {SIZE_LIMIT:,}"
        )
    return len(body).to_bytes(HEADER_BYTES, "little") + body


class SaxmanCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.saxman",
        name="Saxman (ring LZSS, compressed-size header)",
        stage=Stage.COMPRESSION,
        # No end marker, but the header's size is where the body ends.
        self_delimiting=True,
        category="Sega",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)
