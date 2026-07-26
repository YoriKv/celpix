"""Pass-through plugins — the defaults when a stage has nothing to do.

These are what the optional Reshape and Compression stages select when there is
nothing to undo or unpack: the pixel/palette bytes flow through unchanged, so
each stage stays *first-class* in the pipeline rather than being conditionally
skipped. Every real scheme (a split-ROM join, Konami RLE, the SNES LZ family) is
an ordinary drop-in plugin at the same stage — no pipeline change
(``docs/design/overview.md`` §2).
"""

from __future__ import annotations

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import NO_COMPRESSION, NO_RESHAPE, PluginInfo


class PassthroughCompression:
    info = PluginInfo(
        id=NO_COMPRESSION, name="None (uncompressed)", stage=Stage.COMPRESSION
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return data

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return data


class PassthroughReshape:
    info = PluginInfo(id=NO_RESHAPE, name="None (as stored)", stage=Stage.RESHAPE)

    def reshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return data

    def unshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return data
