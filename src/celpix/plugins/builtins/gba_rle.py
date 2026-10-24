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

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    KEY_SURROUND,
    KEY_SURROUND_START,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo
from celpix.plugins.builtins._gba_bios import (
    ALIGNMENT,
    HEADER_SIZE,
    read_header,
    write_header,
)
from celpix.plugins.builtins._lz import corrupt
from celpix.plugins.builtins._rle import pack_runs

RLE_TYPE = 0x30  # high nibble 3 = run-length, low nibble reserved as 0

RUN_FLAG = 0x80
MIN_RUN = 3  # a run packet's stored length is biased by 3
MAX_RUN = 0x7F + MIN_RUN  # 130
MAX_LITERAL = 0x7F + 1  # 128


_fail = corrupt("GBA RLE")


def decompress(
    data: bytes, *, partial: bool = False, reach: int | None = None
) -> tuple[bytes, int, bool]:
    """Decode a BIOS RLE stream.

    Returns ``(output, consumed, complete)``; ``complete`` means the declared
    size was produced. With ``partial`` a buffer that ends mid-stream yields the
    prefix decoded so far instead of raising.

    ``reach`` is how many bytes exist from the stream's start to the end of the
    buffer ``data`` was cut from, when that is known; a lenient decode tests the
    declared size against it (see below). A strict one reads ``data`` alone.
    """
    target = read_header(
        data,
        types=(RLE_TYPE,),
        what="an RLE header (high nibble 3, low nibble reserved as 0)",
        fail=_fail,
    )

    # A two-byte run packet is the best expansion there is, so a declared size
    # past 65 times the bytes that could hold the packets is no stream. Checked
    # up front so a scan rejects a noise header's multi-megabyte size at once.
    # What could hold them is the mode's: a strict decode has `data` alone, but
    # a lenient one has been told its window may cut the structure short — the
    # preview's window, the scan's probe that it then widens — so the window is
    # no bound there (a real structure past 65 windows would lose its preview),
    # while the buffer the window was cut from is: no window over it reaches
    # further. With no buffer known, a lenient decode has nothing to test.
    if not partial:
        limit: int | None = len(data)
    elif reach is not None:
        limit = max(reach, len(data))
    else:
        limit = None
    if limit is not None:
        body = limit - HEADER_SIZE
        if target > body // 2 * MAX_RUN:
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
    out = write_header(RLE_TYPE, len(data), what="GBA BIOS RLE")
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

    _encode = staticmethod(compress)

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        # Spelled out rather than inherited for the surround, which is what
        # bounds a lenient decode's declared size (`decompress`'s `reach`).
        surround = ctx.get(KEY_SURROUND)
        reach = (
            None
            if surround is None
            else len(surround) - (ctx.get(KEY_SURROUND_START) or 0)
        )
        out, consumed, complete = decompress(
            data, partial=bool(ctx.get(KEY_DECOMPRESS_PARTIAL)), reach=reach
        )
        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        ctx.set(KEY_DECOMPRESS_COMPLETE, complete)
        return out
