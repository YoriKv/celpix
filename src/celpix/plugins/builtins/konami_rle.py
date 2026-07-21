"""Konami NES RLE decompressor — view-only compression stage.

Run-length-encoded NES CHR used by many Konami FDS/NES titles; decompresses to
ordinary NES 2bpp, which the normal pixel codec then interprets
(``docs/graphics-formats-reference/implementation-guide.md`` §6). Compression is a
distinct pipeline layer, so this is a Decompress-stage plugin, not a codec variant.

Control stream: a control byte ``c`` selects
- ``0x00``–``0x7E`` **fill**: read one value byte, output it ``c`` times;
- ``0x80``–``0xFE`` **literal**: copy the next ``c & 0x7F`` bytes verbatim;
- ``0x7F`` end of block ("more follows" — kept decompressing);
- ``0xFF`` end.

**View-only** — there is no recompressor, and the optional per-block 2-byte PPU
destination address is not modelled (the "exclude PPU address" case). Pair it with
the NES 2bpp pixel preset.
"""

from __future__ import annotations

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo


class KonamiNesRleDecompress:
    info = PluginInfo(
        id="decompress.konami-nes-rle",
        name="Konami NES RLE (view-only)",
        stage=Stage.DECOMPRESS,
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        out = bytearray()
        i, n = 0, len(data)
        while i < n:
            c = data[i]
            i += 1
            if c == 0xFF:  # end
                break
            if c == 0x7F:  # end of block, another follows immediately
                continue
            if c >= 0x80:  # literal copy of the next (c & 0x7F) bytes
                count = c & 0x7F
                out += data[i : i + count]
                i += count
            else:  # fill: output the next value byte c times
                if i >= n:
                    break
                out += bytes([data[i]]) * c
                i += 1
        return bytes(out)
