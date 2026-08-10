"""SLZ — the Mega Drive homebrew LZ77, in both its 16- and 24-bit framings.

A general-purpose sliding-window LZ77 written for Mega Drive homebrew and used
for data a title decompresses during loading: level layouts, tilesets, whole
asset blobs. Two variants differ in one field, the size prefix, which is what
caps how much a single stream can hold::

    header      uncompressed size, **big-endian**
                  SLZ16   2 bytes   (up to 0xFFFF)
                  SLZ24   3 bytes   (up to 0xFFFFFF)
    then, repeating:
      token byte      8 op selectors, **MSB first**; 1 = back-reference, 0 = literal
      literal:        1 byte, copied out
      back-reference: 2 bytes, read as one big-endian word ``info``
                        distance = (info >> 4) + 3      (3..4098, 12 bits)
                        length   = (info & 0x0F) + 3    (3..18)
                      source = output_position - distance

**The nibble roles are the trap, because the neighbouring format swaps them.**
GBA/NDS BIOS LZ77 (:mod:`~celpix.plugins.builtins.gba_lz77`) is also a 12-bit
distance and a 4-bit length in a two-byte reference, but it puts the *length* in
the high nibble and biases the distance by 1; this puts the *distance* in the
high twelve bits and biases both fields by 3. Neither reference is self-checking,
so reading one as the other yields plausible-looking garbage of about the right
length rather than an error.

**The size prefix is the only terminator.** There is no end marker, so a decode
stops when it has produced exactly the declared count, and the source position it
stopped at is the structure's true compressed length. Unlike the BIOS LZ77 next
door, a well-formed stream never overshoots — an encoder caps its last match at
what remains — so producing *more* than the declared size is corruption rather
than a match to be clipped, and is reported as such.

**Copies are byte at a time, forward.** A match may be longer than its distance
and re-read bytes it has just written, which is how the format run-length-encodes
a fill. A bulk copy is wrong for exactly the data that compresses best.

**A stream can end with a token byte nobody reads.** The known encoder flushes a
full group inside its loop and then flushes again unconditionally on the way out,
so a payload whose op count is an exact multiple of 8 gets a trailing empty token
byte. The decoder stops on the declared size and never reaches it, which is why
the byte is harmless — but it means a stream can *occupy* one byte more than it
consumes, and :data:`~celpix.core.context.KEY_COMPRESSED_SIZE` reports what was
read. The ring LZSS and Kosinski both have a version of this; treat the reported
size as the structure's content, not necessarily its slot.

**An empty payload is a size prefix of zero and nothing else** — the format's own
encoding of it, not a degenerate case, so it is accepted on the way in as well as
written on the way out. The size-prefixed ring LZSS rejects a zero there because
that reading would make any run of zero bytes a structure; here no amount of
strictness buys that, since the format carries no magic at all and any two bytes
are a legal header.

On the encode side, byte-identity with any particular tool is a non-goal (the
rule the ring LZSS and the BIOS LZ77 both keep) — round-tripping is the contract.
The known encoder walks candidate distances outward and keeps the last strict
improvement, so it breaks ties on the *farthest* match; distance costs nothing to
write, so that choice changes which stream comes out without changing its size.

Format detail and provenance are in
``docs/graphics-formats-reference/implementation-guide.md`` §7.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo
from celpix.plugins.builtins._lz import (
    FlagGroup,
    MatchFinder,
    copy_from,
    parse_greedy,
)

SIZE_BYTES_16 = 2
SIZE_BYTES_24 = 3

MIN_MATCH = 3
MAX_MATCH = 18  # 4-bit length field, biased by MIN_MATCH

# The distance field is 12 bits and stores distance - 3, so the reachable
# distances are 3..4098. The bias is the reason a nearer match cannot be written
# at all: the two bytes a reference costs would encode distance 3 whatever was
# meant, so positions 1 and 2 back stay literals.
MIN_DISTANCE = 3
MAX_DISTANCE = 0xFFF + MIN_DISTANCE

# Compressor tuning: how many recent positions sharing a 3-byte prefix to test
# (see :class:`~celpix.plugins.builtins._lz.MatchFinder`).
_MAX_CANDIDATES = 96


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt SLZ stream: {reason}")


def _max_size(size_bytes: int) -> int:
    return (1 << (size_bytes * 8)) - 1


def decompress(
    data: bytes, *, size_bytes: int, partial: bool = False
) -> tuple[bytes, int, bool]:
    """Decode an SLZ stream whose size prefix is ``size_bytes`` wide.

    Returns ``(output, consumed, complete)``. ``complete`` is true when the full
    declared size was produced, making ``consumed`` the structure's true byte
    length — the slot a save-back must fit. With ``partial`` a buffer that ends
    mid-stream yields the prefix decoded so far instead of raising, which is what
    a bounded view window needs; a structurally invalid stream still raises.
    """
    if len(data) < size_bytes:
        raise _fail(f"shorter than the {size_bytes}-byte size prefix")
    target = int.from_bytes(data[:size_bytes], "big")
    if target == 0:
        # The format's encoding of an empty payload; see the module docstring.
        return b"", size_bytes, True

    out = bytearray()
    src = size_bytes
    n = len(data)

    while len(out) < target and src < n:
        tokens = data[src]
        src += 1
        for bit in range(8):
            if len(out) >= target:
                break
            if not tokens & (0x80 >> bit):  # MSB first; a clear bit is a literal
                if src >= n:
                    break
                out.append(data[src])
                src += 1
                continue

            if src + 1 >= n:
                break
            info = (data[src] << 8) | data[src + 1]
            src += 2
            distance = (info >> 4) + MIN_DISTANCE
            length = (info & 0x0F) + MIN_MATCH
            start = len(out) - distance
            if start < 0:
                # The 68000 decoder reads whatever precedes its buffer here.
                # There is no defensible output for it, and a stream that does
                # this is not one this format produced.
                raise _fail(
                    f"back-reference at output byte {len(out):,} reaches "
                    f"{-start} bytes before the start of the data"
                )
            copy_from(out, start, length)

    if len(out) > target:
        # Not a match to clip: the format lands on the declared size exactly, so
        # overshoot means the tokens were not the ones this stream was written
        # with. Raised even under `partial`, which forgives a short buffer only.
        raise _fail(f"produced {len(out):,} bytes against a declared {target:,}")
    complete = len(out) == target
    if not complete and not partial:
        raise _fail(f"source ended after {len(out):,} of {target:,} bytes")
    return bytes(out), src, complete


# -- compression ------------------------------------------------------------


def compress(data: bytes, *, size_bytes: int) -> bytes:
    """Encode raw bytes into an SLZ stream with a ``size_bytes``-wide prefix."""
    n = len(data)
    limit = _max_size(size_bytes)
    if n > limit:
        raise ValueError(
            f"input is {n:,} bytes; the {size_bytes * 8}-bit SLZ size field holds "
            f"{limit:,}"
        )

    out = bytearray(n.to_bytes(size_bytes, "big"))
    if n == 0:
        return bytes(out)

    finder = MatchFinder(
        data, min_match=MIN_MATCH, window=MAX_DISTANCE, max_candidates=_MAX_CANDIDATES
    )
    tokens = FlagGroup(out, msb_first=True, set_means_match=True)
    for pos, length, candidate in parse_greedy(
        data,
        finder,
        min_match=MIN_MATCH,
        max_match=MAX_MATCH,
        # Never the nearest two distances the index offers: the biased distance
        # field cannot name them at all (see MIN_DISTANCE).
        min_distance=MIN_DISTANCE,
    ):
        tokens.select(length > 0)
        if length:
            info = ((pos - candidate - MIN_DISTANCE) << 4) | (length - MIN_MATCH)
            out.append(info >> 8)
            out.append(info & 0xFF)
        else:
            out.append(data[pos])
    tokens.finish()

    return bytes(out)


class _SlzBase(PartialDecompression):
    """Both directions of one SLZ variant; the size prefix's width is all that
    differs, and it is what caps the payload the variant can carry."""

    _size_bytes: int

    def _decode(self, data: bytes, *, partial: bool) -> tuple[bytes, int, bool]:
        return decompress(data, size_bytes=self._size_bytes, partial=partial)

    def _encode(self, data: bytes) -> bytes:
        return compress(data, size_bytes=self._size_bytes)


class Slz16Compression(_SlzBase):
    _size_bytes = SIZE_BYTES_16
    info = PluginInfo(
        id="compression.slz16",
        name="SLZ16 (Mega Drive, 64 KB payload)",
        stage=Stage.COMPRESSION,
        # The body has no end marker, but the size prefix bounds it, so a decode
        # does know where the structure ends.
        self_delimiting=True,
        category="Sega",
    )


class Slz24Compression(_SlzBase):
    _size_bytes = SIZE_BYTES_24
    info = PluginInfo(
        id="compression.slz24",
        name="SLZ24 (Mega Drive, 16 MB payload)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,
        category="Sega",
    )
