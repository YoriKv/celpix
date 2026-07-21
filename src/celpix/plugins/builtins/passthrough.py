"""Pass-through Decompress/Compress — the default when data is uncompressed.

These make the optional compression stages *first-class* in the pipeline without
implementing any codec yet: the pixel/palette bytes flow through unchanged. A real
compression scheme is a drop-in replacement plugin at the same stage — no pipeline
change (``docs/design/overview.md`` §2; MVP plan §1, "compression-ready").
"""

from __future__ import annotations

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo


class PassthroughDecompress:
    info = PluginInfo(
        id="decompress.none", name="None (uncompressed)", stage=Stage.DECOMPRESS
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return data


class PassthroughCompress:
    info = PluginInfo(
        id="compress.none", name="None (uncompressed)", stage=Stage.COMPRESS
    )

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return data
