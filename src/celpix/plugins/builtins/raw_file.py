"""The container for a plain binary file — the base case (no framing at all).

Reading hands back the requested window and records **provenance** (the offset
those bytes start at) into the context, so writing can default to putting them
back exactly where they came from (``docs/design/overview.md`` §5). Writing splices
``data`` in at ``offset``, preserving surrounding bytes; for the common whole-file
case it returns ``data`` as the file's new contents, which keeps an unedited round
trip byte-identical.

Real container handling (iNES header skip, ``.smd`` deinterleave, checksum repair)
is a separate plugin per format — not this one.
"""

from __future__ import annotations

from celpix.core.context import KEY_SOURCE_OFFSET, PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import (
    RAW_CONTAINER,
    PluginInfo,
    ReadSource,
    WriteTarget,
    splice,
)


class RawFileContainer:
    # No signature: this is where detection lands when nothing claims a file, so
    # claiming anything itself would only get in the way.
    info = PluginInfo(id=RAW_CONTAINER, name="Raw binary file", stage=Stage.CONTAINER)

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        # This container strips no framing, so the payload begins where the caller
        # asked rather than at an answer of the format's own — the offset is
        # published unchanged, and stays file-absolute even when the bytes came
        # from a buffer starting part way into the file (ReadSource.window).
        ctx.set(KEY_SOURCE_OFFSET, source.offset)
        return source.window()

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        return data if dest.whole_file else splice(dest.existing, dest.offset, data)
