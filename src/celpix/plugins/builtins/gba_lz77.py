"""GBA/NDS BIOS LZ77 — the handheld decompression ROM call, both directions.

The compression almost all Game Boy Advance and Nintendo DS graphics ship behind:
the console's own BIOS decompresses it (`SWI 0x11`, and the VRAM-safe `SWI 0x12`),
so a game gets it for free and nearly every title uses it. Stream shape::

    byte0        0x10        high nibble 1 = LZ77; the low nibble is reserved
    bytes 1..3   uint24 le   decompressed size, in bytes
    then, repeating:
      flags byte      8 op selectors, **MSB first**; 1 = back-reference, 0 = literal
      literal:        1 byte, copied out
      back-reference: 2 bytes  b0, b1
                      length   = (b0 >> 4) + 3                  (3..18)
                      disp     = ((b0 & 0x0F) << 8) | b1        (0..4095)
                      source   = output_position - disp - 1     (distance 1..4096)

Two things about that layout cost real work to get wrong, and both are silent:

- **The flag byte runs MSB first**, unlike the LSB-first ring LZSS next door
  (:mod:`~celpix.plugins.builtins.lzss_ring`), and a set bit selects the
  *back-reference* rather than the literal. Read the other way a stream still
  decodes to something the right length.
- **The displacement is stored one less than the distance.** A stored 0 means
  "copy from the byte just emitted", which is how a run of one repeated byte is
  encoded, so an off-by-one here shifts every match by a byte instead of failing.

**The declared size is the only terminator.** There is no end marker: a decode
stops the moment it has produced that many bytes, which may be **part way through
a back-reference**, and any remaining flag bits are never examined. So the size
has to be checked per output byte rather than per match — a decoder that finishes
each match before testing overruns the buffer on the last one.

**Copies are byte at a time, forward.** A match may be longer than its distance,
re-reading bytes it has just written, which is what gives the format run-length
encoding for free. A bulk copy is wrong for exactly the cases that matter most in
tile graphics.

**On the encode side this deliberately does not reproduce the reference tool.**
Round-tripping is the contract, not byte-identity with any particular encoder
(the same rule the ring LZSS keeps), and the widely used one leaves three things
on the table: it caps its search one short of the reachable distance, never
references the input's first byte, and always opens with two literals. None of
that is in the format.

**What this encoder does add is VRAM safety, and it is free of any correctness
trade-off.** The BIOS has two entry points. The one that writes VRAM 16 bits at a
time (`SWI 0x12`) accepts only a stored displacement of `0x001..0xFFF` — never 0 —
because a halfword write to `dest-1` cannot then be read back at `dest-1` before
it has landed. The byte-writing call (`SWI 0x11`) states no such restriction and
takes the whole `0x000..0xFFF` range, and the two share one stream format.

So the safe range is a strict **subset** of the permissive one: a stream that
never stores displacement 0 decodes correctly under *both* calls, and nothing has
to know which one a given ROM uses. Requiring a distance of at least 2
(:data:`MIN_DISTANCE`) buys that for one literal byte at the head of a run — the
match still covers the rest of it. The reference tool instead emits displacement 0
freely (about 3.5% of its matches), so its output is only valid for `SWI 0x11`,
and a game calling the other one gets corrupt graphics.

Format provenance, the reference tool's own quirks, and its ROM-scanning
heuristic are in
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

HEADER_SIZE = 4
# The type nibble the BIOS dispatches on. The low nibble is reserved and ignored
# by hardware, so it is masked off rather than required to be zero: real ROM data
# carries stray values there, and the widely used PC tool's exact-0x10 test is a
# narrowing of its own rather than something the format says.
LZ77_TYPE = 0x10
_TYPE_MASK = 0xF0

MIN_MATCH = 3
MAX_MATCH = 18  # 4-bit length field, biased by MIN_MATCH

# The displacement field is 12 bits and stores distance - 1, so the reachable
# distances are 1..4096.
MAX_DISTANCE = 4096
# The nearest distance this *writes*. Distance 1 is a stored displacement of 0,
# which the VRAM-safe BIOS call rejects and the byte-writing one accepts; keeping
# to 2 makes one stream valid for both. See the module docstring.
MIN_DISTANCE = 2

MAX_DECOMPRESSED = 0xFFFFFF  # the header's size field is 24 bits

# Compressor tuning: how many recent positions sharing a 3-byte prefix to test
# (see :class:`~celpix.plugins.builtins._lz.MatchFinder`).
_MAX_CANDIDATES = 96


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt LZ77 stream: {reason}")


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Decode a BIOS LZ77 stream.

    Returns ``(output, consumed, complete)``. ``complete`` is true when the full
    declared size was produced, making ``consumed`` the structure's true byte
    length — the slot a save-back must fit. With ``partial`` a buffer that ends
    mid-stream yields the prefix decoded so far instead of raising, which is what
    a bounded view window needs; a structurally invalid stream still raises.
    """
    if len(data) < HEADER_SIZE:
        raise _fail(f"shorter than the {HEADER_SIZE}-byte header")
    if data[0] & _TYPE_MASK != LZ77_TYPE:
        raise _fail(f"type byte {data[0]:#04x} is not an LZ77 header (high nibble 1)")
    target = int.from_bytes(data[1:HEADER_SIZE], "little")
    if target == 0:
        # Nothing to produce, and accepting it would make four bytes of noise a
        # valid structure wherever a 0x10 happened to land.
        raise _fail("declared decompressed size is zero")

    out = bytearray()
    src = HEADER_SIZE
    n = len(data)

    while len(out) < target and src < n:
        flags = data[src]
        src += 1
        for bit in range(8):
            if len(out) >= target:
                break
            if not flags & (0x80 >> bit):  # MSB first; a clear bit is a literal
                if src >= n:
                    break
                out.append(data[src])
                src += 1
                continue

            if src + 1 >= n:
                break
            b0, b1 = data[src], data[src + 1]
            src += 2
            start = len(out) - (((b0 & 0x0F) << 8) | b1) - 1
            if start < 0:
                # The reference decoder reads whatever precedes its buffer here.
                # There is no defensible output for it, and a stream that does
                # this is not one this format produced.
                raise _fail(
                    f"back-reference at output byte {len(out):,} reaches "
                    f"{-start} bytes before the start of the data"
                )
            # Clamped to what is still wanted: the size is the only terminator,
            # so the final match is routinely cut off part way through.
            length = min((b0 >> 4) + MIN_MATCH, target - len(out))
            copy_from(out, start, length)

    complete = len(out) == target
    if not complete and not partial:
        raise _fail(f"source ended after {len(out):,} of {target:,} bytes")
    return bytes(out), src, complete


# -- compression ------------------------------------------------------------


def compress(data: bytes) -> bytes:
    """Encode raw bytes into a BIOS LZ77 stream, safe for both BIOS entry points."""
    n = len(data)
    if n > MAX_DECOMPRESSED:
        raise ValueError(
            f"input is {n:,} bytes; the 24-bit LZ77 size field holds "
            f"{MAX_DECOMPRESSED:,}"
        )

    finder = MatchFinder(
        data, min_match=MIN_MATCH, window=MAX_DISTANCE, max_candidates=_MAX_CANDIDATES
    )
    out = bytearray([LZ77_TYPE])
    out += n.to_bytes(3, "little")
    # MSB first, and a set bit is the back-reference — both the opposite way round
    # from the ring LZSS next door (see the module docstring).
    group = FlagGroup(out, msb_first=True, set_means_match=True)
    for pos, length, candidate in parse_greedy(
        data,
        finder,
        min_match=MIN_MATCH,
        max_match=MAX_MATCH,
        # Never the nearest distance the index offers: distance 1 is the stored
        # displacement the VRAM-safe BIOS call rejects (see MIN_DISTANCE).
        min_distance=MIN_DISTANCE,
    ):
        group.select(length > 0)
        if length:
            disp = pos - candidate - 1  # stored one less than the distance
            out.append(((length - MIN_MATCH) << 4) | (disp >> 8))
            out.append(disp & 0xFF)
        else:
            out.append(data[pos])
    group.finish()

    return bytes(out)


class GbaLz77Compression(PartialDecompression):
    info = PluginInfo(
        id="compression.gba-lz77",
        name="GBA/NDS BIOS LZ77 (SWI 0x11/0x12)",
        stage=Stage.COMPRESSION,
        # The body has no end marker, but the 24-bit size field in the header
        # bounds it, so a decode does know where the structure ends.
        self_delimiting=True,
        category="Nintendo",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)
