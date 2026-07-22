"""SNES Mode 7 VRAM split — separate the interleaved tile pixels and BG map.

Mode 7 VRAM interleaves two byte streams within each 16-bit word: the 128x128
BG map's char numbers in the low (even-address) bytes and the 8bpp tile pixels
in the high (odd-address) bytes — an 8x8 tile is 64 odd bytes inside a 128-byte
span (``docs/graphics-formats-reference/snes-hardware-notes.md``). ROMs usually
store Mode 7 graphics already split, but VRAM dumps and savestates are
interleaved, so neither half is viewable as-is.

Decompress reorders the data to *odd bytes then even bytes*: the pixel bytes
come first (view with the 8bpp chunky preset), the BG map's char numbers fill
the second half. Compress re-interleaves exactly, so the round trip is
byte-exact and write-back preserves both halves. This is a Decompress-stage
plugin rather than a codec stride parameter because a codec that skipped the
map bytes could not reproduce them on encode — a lossless reorder keeps every
byte in the document.
"""

from __future__ import annotations

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo


class M7VramDecompress:
    info = PluginInfo(
        id="decompress.snes-m7-vram",
        name="SNES Mode 7 VRAM (split pixels/map)",
        stage=Stage.DECOMPRESS,
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return data[1::2] + data[0::2]


class M7VramCompress:
    info = PluginInfo(
        id="compress.snes-m7-vram",
        name="SNES Mode 7 VRAM (split pixels/map)",
        stage=Stage.COMPRESS,
    )

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        # Odd length is fine either way: the odd-byte half is the shorter one
        # (len // 2), matching what the decompress slices produced.
        half = len(data) // 2
        out = bytearray(len(data))
        out[1::2] = data[:half]
        out[0::2] = data[half:]
        return bytes(out)
