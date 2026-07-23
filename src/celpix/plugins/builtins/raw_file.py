"""Read/Write plugins for a plain binary file — the base case (no container quirks).

The reader slices ``[offset : offset+length]`` out of the file and records
**provenance** (path + offset) into the context, so the writer can default to
putting the bytes back exactly where they came from
(``docs/design/overview.md`` §5). The writer splices ``data`` in at ``offset``,
preserving surrounding bytes; for the common whole-file case (offset 0) it simply
replaces the file, which keeps an unedited round trip byte-identical.

Container handling (iNES header skip, ``.smd`` deinterleave, checksum repair) is a
later, separate reader/writer — not this one.
"""

from __future__ import annotations

from pathlib import Path

from celpix.core.context import KEY_SOURCE_OFFSET, KEY_SOURCE_PATH, PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import FileRef, PluginInfo


class RawFileReader:
    info = PluginInfo(id="read.raw-file", name="Raw binary file", stage=Stage.READ)

    def read(self, source: FileRef, ctx: PipelineContext) -> bytes:
        # In-memory source (a palette extracted from an emulator memory image, a
        # slice served from its dirty parent's buffer) reads from source.data;
        # otherwise slice the file on disk. Either way offset/length are
        # file-absolute and provenance records them as such — data_base rebases
        # them onto a buffer that starts part way into the file.
        in_memory = source.data is not None
        raw = source.data if in_memory else Path(source.path).read_bytes()
        start = max(0, source.offset - (source.data_base if in_memory else 0))
        end = len(raw) if source.length is None else start + source.length
        ctx.set(KEY_SOURCE_PATH, source.path)
        ctx.set(KEY_SOURCE_OFFSET, source.offset)
        return raw[start:end]


class RawFileWriter:
    info = PluginInfo(id="write.raw-file", name="Raw binary file", stage=Stage.WRITE)

    def write(self, data: bytes, dest: FileRef, ctx: PipelineContext) -> None:
        path = Path(dest.path)
        if dest.offset == 0 and dest.length is None:
            path.write_bytes(data)
            return
        existing = bytearray(path.read_bytes()) if path.exists() else bytearray()
        end = dest.offset + len(data)
        if len(existing) < end:
            existing.extend(b"\x00" * (end - len(existing)))
        existing[dest.offset : end] = data
        path.write_bytes(existing)
