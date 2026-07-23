"""Pass-through Decompress/Compress — the default when data is uncompressed.

These are what the optional compression stages select when there is nothing to
unpack: the pixel/palette bytes flow through unchanged, so the stages stay
*first-class* in the pipeline rather than being conditionally skipped. Every real
scheme (Konami RLE, the SNES LZ family) is an ordinary drop-in plugin at the same
stage — no pipeline change (``docs/design/overview.md`` §2).
"""

from __future__ import annotations

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import NO_COMPRESS, NO_DECOMPRESS, PluginInfo


class PassthroughDecompress:
    info = PluginInfo(
        id=NO_DECOMPRESS, name="None (uncompressed)", stage=Stage.DECOMPRESS
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return data


class PassthroughCompress:
    info = PluginInfo(id=NO_COMPRESS, name="None (uncompressed)", stage=Stage.COMPRESS)

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return data
