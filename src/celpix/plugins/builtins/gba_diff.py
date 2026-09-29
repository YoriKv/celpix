"""GBA/NDS BIOS difference filter ("UnFilter") — both unit widths, both directions.

Not compression: the BIOS's ``Diff8bitUnFilter`` (`SWI 0x16` WRAM / `SWI 0x17`
VRAM) and ``Diff16bitUnFilter`` (`SWI 0x18`) undo a delta encoding, so the output
is exactly as long as the payload. A game filters smooth data (a ramp, a wave
form, a gradient bitmap) and then usually compresses the result with one of the
real codecs, so a filtered block is typically found *inside* an LZ77 or Huffman
output rather than loose in the ROM. Stream shape::

    byte0        0x81 / 0x82  high nibble 8 = difference filter;
                              low nibble = unit size in bytes (1 = 8-bit, 2 = 16-bit)
    bytes 1..3   uint24 le    decompressed size, in bytes
    then units (u8, or u16 little-endian):
      unit 0      the first value, as is
      unit i      value[i] - value[i-1], modulo 2**8 / 2**16

    output[i] = output[i-1] + input[i]   (wrapping), output[-1] taken as 0

**The arithmetic is the running-sum reshape's**
(:mod:`~celpix.plugins.builtins.running_sum`), and this module calls it rather
than repeating it: its ``sum_units`` on load and ``difference_units`` on save,
at the header's unit width. What separates the two is framing. The reshape is
**headerless** — length-preserving over whatever region it is handed, with no
extent of its own, so it has to be a Reshape stage (a scan would "find" it at
every offset). These plugins are a **headered stream**: the type byte names the
width, the declared size is the structure's end, and the stream sits 4-byte
aligned like every BIOS call's — which is what makes each a Compression plugin,
with a slot a save-back fits and a start the structure scan can find. The
16-bit width lives beside the byte sum there, for these plugins.

Traps:

- **The low nibble is the unit width and must match the call.** Diff8 on a
  0x82 stream (or the reverse) runs without complaint and produces noise, so
  the two are separate plugins here and each rejects the other's header.
- **Wrap-around is the arithmetic, not an error.** A delta of 0xFF is −1.
- **An odd size under the 16-bit filter** still reads whole halfwords; this
  decoder reads ``ceil(size / 2)`` units and returns exactly the declared size.
  The encoder refuses an odd length rather than invent the high byte of a last
  halfword the data does not have.
- **Scanning for it is almost meaningless**: any aligned 0x81/0x82 byte with a
  size that fits the buffer "decodes cleanly", and the output is never smaller
  than the input. Recognise filtered data by what it decodes to.

Sources: GBATEK, "BIOS Decompression Functions" (Diff8bitUnFilter /
Diff16bitUnFilter), and mGBA's HLE BIOS (``src/gba/bios.c``, ``_unFilter``). The
format's write-up is in
``docs/graphics-formats-reference/implementation-guide.md`` §7.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo
from celpix.plugins.builtins.gba_lz77 import ALIGNMENT, HEADER_SIZE, MAX_DECOMPRESSED
from celpix.plugins.builtins.running_sum import difference_units, sum_units

DIFF_TYPE = 0x80  # high nibble 8; the low nibble is the unit width in bytes
DIFF8_TYPE = DIFF_TYPE | 1
DIFF16_TYPE = DIFF_TYPE | 2
WIDTHS = (1, 2)


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt GBA diff-filter stream: {reason}")


def _check_width(width: int) -> None:
    if width not in WIDTHS:
        raise ValueError(f"unit width must be 1 or 2 bytes, not {width}")


def decompress(
    data: bytes, *, width: int = 1, partial: bool = False
) -> tuple[bytes, int, bool]:
    """Undo the filter; ``width`` is the unit size in bytes (1 or 2).

    Returns ``(output, consumed, complete)``. With ``partial`` a buffer that ends
    before the declared size yields the units decoded so far.
    """
    _check_width(width)
    kind = DIFF_TYPE | width
    if len(data) < HEADER_SIZE:
        raise _fail(f"shorter than the {HEADER_SIZE}-byte header")
    if data[0] != kind:
        raise _fail(
            f"type byte {data[0]:#04x} is not a {8 * width}-bit diff header "
            f"({kind:#04x}; the low nibble is the unit width)"
        )
    target = int.from_bytes(data[1:HEADER_SIZE], "little")
    if target == 0:
        raise _fail("declared decompressed size is zero")

    units = -(-target // width)
    count = min(units, (len(data) - HEADER_SIZE) // width)
    complete = count == units
    if not complete and not partial:
        # Before summing anything, so a scan's noise header costs nothing.
        raise _fail(f"source ended after {count * width:,} of {target:,} bytes")
    end = HEADER_SIZE + count * width
    out = sum_units(data[HEADER_SIZE:end], width)[:target]
    return out, end, complete


def compress(data: bytes, *, width: int = 1) -> bytes:
    """Delta-filter ``data`` into ``width``-byte units behind the BIOS header."""
    _check_width(width)
    n = len(data)
    if n > MAX_DECOMPRESSED:
        raise ValueError(
            f"input is {n:,} bytes; the 24-bit size field holds {MAX_DECOMPRESSED:,}"
        )
    if n % width:
        raise ValueError(
            f"input is {n:,} bytes, not a whole number of {width}-byte units"
        )
    out = bytearray([DIFF_TYPE | width])
    out += n.to_bytes(3, "little")
    out += difference_units(data, width)
    return bytes(out)


class _GbaDiffBase(PartialDecompression):
    """Both directions at one unit width; the header byte is what differs."""

    _width: int

    def _decode(self, data: bytes, *, partial: bool) -> tuple[bytes, int, bool]:
        return decompress(data, width=self._width, partial=partial)

    def _encode(self, data: bytes) -> bytes:
        return compress(data, width=self._width)


class GbaDiff8Compression(_GbaDiffBase):
    _width = 1
    info = PluginInfo(
        id="compression.gba-diff8",
        name="GBA/NDS BIOS 8-bit diff filter (SWI 0x16/0x17)",
        stage=Stage.COMPRESSION,
        # No end marker, but the header's 24-bit size bounds the structure.
        self_delimiting=True,
        alignment=ALIGNMENT,
        category="Nintendo",
    )


class GbaDiff16Compression(_GbaDiffBase):
    _width = 2
    info = PluginInfo(
        id="compression.gba-diff16",
        name="GBA/NDS BIOS 16-bit diff filter (SWI 0x18)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,
        alignment=ALIGNMENT,
        category="Nintendo",
    )
