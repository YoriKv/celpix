"""The container for a plain binary file — the base case, no framing at all.

Reading hands back the requested window and records the offset those bytes start
at on the context, so writing can put them back where they came from
(``docs/design/overview.md`` §5). Writing splices ``data`` in at ``offset``,
preserving surrounding bytes; the whole-file case returns ``data`` as the file's
new contents, keeping an unedited round trip byte-identical.

Framing of any kind — an iNES header skip, a ``.smd`` deinterleave, a checksum
repair — is a separate plugin per format.
"""

from __future__ import annotations

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import (
    RAW_CONTAINER,
    PluginInfo,
    ReadSource,
    WriteTarget,
    plain_read,
    splice,
)


class RawFileContainer:
    # No signature: this is where detection lands when nothing claims a file, so
    # claiming anything itself would only get in the way.
    info = PluginInfo(id=RAW_CONTAINER, name="Raw binary file", stage=Stage.CONTAINER)

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        return plain_read(source, ctx)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        return data if dest.whole_file else splice(dest.existing, dest.offset, data)
