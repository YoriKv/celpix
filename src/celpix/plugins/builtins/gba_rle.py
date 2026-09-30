"""GBA/NDS BIOS run-length — the handheld RLUnComp ROM call, both directions.

The BIOS's byte RLE (`SWI 0x14` writes 8-bit units for WRAM, `SWI 0x15` 16-bit
units for VRAM; on the DS `SWI 0x15` is the read-by-callback variant). Stream
shape::

    byte0        0x30        high nibble 3 = run-length; the low nibble is reserved (0)
    bytes 1..3   uint24 le   decompressed size, in bytes
    then, repeating until the size is reached:
      flag byte   bit 7 = 1  run:     length = (flag & 0x7F) + 3   (3..130),
                                      then one data byte, repeated
                  bit 7 = 0  literal: length = (flag & 0x7F) + 1   (1..128),
                                      then that many data bytes

**The declared size is the only terminator**, exactly as for the BIOS LZ77
(:mod:`~celpix.plugins.builtins.gba_lz77`): the unpacker counts output bytes and
stops mid-packet when the count runs out, so a final run or literal block may be
cut short and the bytes after it are never read. ``consumed`` therefore ends at
the last byte the BIOS actually read, which inside a literal block that the size
cut short is *not* the packet's end.

**The two biases differ** — a run stores length − 3, a literal block length − 1.
Read a run with the literal bias and a stream still decodes, two bytes short per
run; the declared size then makes the decode run on into whatever follows.

**The header is read with one 32-bit load**, so the stream starts 4-byte aligned
(the same rule as LZ77; :data:`ALIGNMENT`). The VRAM entry point writes
halfwords, and mGBA's HLE BIOS rounds the *written* extent up to a
multiple of 4 with zero bytes — that padding is output, not stream, and this
decoder returns exactly the declared size.

**The header byte is the whole signature**, so a scan is far noisier than
LZ77's, whose flag bytes and back-references also have to hold together: any
aligned ``0x30`` whose size the following bytes happen to satisfy decodes
cleanly. Measured on one 16 MiB cartridge that uses this call, about one clean
start in thirteen was a real stream — the ones a cart pointer targets. The
smart scan's whole-tiles-and-ratio test is what narrows it.

The encoder is the shared run/literal packer
(:func:`~celpix.plugins.builtins._rle.pack_runs`): runs of 3 or more become run
packets, everything else rides in literal blocks. Both packet kinds are capped at
128 bytes (the packer takes one cap), so a run of 129 or 130 costs one extra
packet over the format's maximum — a size trade, not a correctness one. Like the
LZ77 encoder it writes no trailing padding: aligning the next structure is the
caller's job.

Sources: GBATEK, "BIOS Decompression Functions" (RLUnComp), and mGBA's HLE BIOS
(``src/gba/bios.c``, ``_unRl``), which agree on every field above. The format's
write-up is in ``docs/graphics-formats-reference/implementation-guide.md`` §7.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo
from celpix.plugins.builtins._rle import pack_runs
from celpix.plugins.builtins.gba_lz77 import ALIGNMENT, HEADER_SIZE, MAX_DECOMPRESSED

RLE_TYPE = 0x30  # high nibble 3 = run-length, low nibble reserved as 0

RUN_FLAG = 0x80
MIN_RUN = 3  # a run packet's stored length is biased by 3
MAX_RUN = 0x7F + MIN_RUN  # 130
MAX_LITERAL = 0x7F + 1  # 128


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt GBA RLE stream: {reason}")


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Decode a BIOS RLE stream.

    Returns ``(output, consumed, complete)``; ``complete`` means the declared
    size was produced. With ``partial`` a buffer that ends mid-stream yields the
    prefix decoded so far instead of raising.
    """
    if len(data) < HEADER_SIZE:
        raise _fail(f"shorter than the {HEADER_SIZE}-byte header")
    if data[0] != RLE_TYPE:
        raise _fail(
            f"type byte {data[0]:#04x} is not an RLE header "
            "(high nibble 3, low nibble reserved as 0)"
        )
    target = int.from_bytes(data[1:HEADER_SIZE], "little")
    if target == 0:
        raise _fail("declared decompressed size is zero")

    body = len(data) - HEADER_SIZE
    if not partial and target > body // 2 * MAX_RUN:
        # A two-byte run packet is the best expansion there is. Checked up front
        # so a scan rejects a noise header's multi-megabyte size at once.
        raise _fail(f"{target:,} bytes cannot fit in {body:,} bytes of packets")

    out = bytearray()
    src = HEADER_SIZE
    n = len(data)
    while len(out) < target and src < n:
        flag = data[src]
        src += 1
        want = target - len(out)
        if flag & RUN_FLAG:
            if src >= n:
                break
            out += bytes([data[src]]) * min((flag & 0x7F) + MIN_RUN, want)
            src += 1
        else:
            # The BIOS reads a literal byte only while output is still wanted,
            # so a block the size cuts short ends the stream part way through.
            take = min((flag & 0x7F) + 1, want, n - src)
            out += data[src : src + take]
            src += take

    complete = len(out) == target
    if not complete and not partial:
        raise _fail(f"source ended after {len(out):,} of {target:,} bytes")
    return bytes(out), src, complete


def compress(data: bytes) -> bytes:
    """Encode raw bytes as a BIOS RLE stream (header + packets, no padding)."""
    n = len(data)
    if n == 0:
        # A zero size is what decompress refuses, so writing one would save a
        # stream this plugin cannot open again.
        raise ValueError("GBA BIOS RLE has no encoding for an empty payload")
    if n > MAX_DECOMPRESSED:
        raise ValueError(
            f"input is {n:,} bytes; the 24-bit size field holds {MAX_DECOMPRESSED:,}"
        )
    out = bytearray([RLE_TYPE])
    out += n.to_bytes(3, "little")
    pack_runs(
        data,
        out,
        literal_header=lambda count: count - 1,
        run_header=lambda count: RUN_FLAG | (count - MIN_RUN),
        max_packet=MAX_LITERAL,
        min_run=MIN_RUN,
        # A pair can never be a run here (the shortest run is 3).
        spill_pair_as_run=False,
    )
    return bytes(out)


class GbaRleCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.gba-rle",
        name="GBA/NDS BIOS RLE (SWI 0x14/0x15)",
        stage=Stage.COMPRESSION,
        # No end marker, but the header's 24-bit size bounds the structure.
        self_delimiting=True,
        alignment=ALIGNMENT,
        category="Nintendo",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)
