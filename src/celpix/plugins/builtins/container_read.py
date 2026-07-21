"""Container-aware Read plugins: iNES header skip and Sega ``.smd`` deinterleave.

Both acquire raw bytes like :class:`~celpix.plugins.builtins.raw_file.RawFileReader`
but apply a container transform first, so the pixel codec downstream sees contiguous
tile data (``docs/graphics-formats-reference/implementation-guide.md`` §5). They
record provenance into the context like the raw reader does.
"""

from __future__ import annotations

from pathlib import Path

from celpix.core.context import KEY_SOURCE_OFFSET, KEY_SOURCE_PATH, PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import FileRef, PluginInfo

_INES_MAGIC = b"NES\x1a"


class INesReader:
    """Read a ``.nes`` file, auto-skipping the iNES header to the CHR ROM.

    If bytes 0–3 are ``NES\\x1a``, the 16-byte header (plus a 512-byte trainer when
    present) is skipped; the CHR ROM starts after the PRG banks. When the cart uses
    CHR-RAM (0 CHR banks) there is no CHR ROM, so the bytes after the header are
    returned. A file without the magic is read like a plain binary.
    """

    info = PluginInfo(
        id="read.ines", name="iNES file (auto-skip header)", stage=Stage.READ
    )

    def read(self, source: FileRef, ctx: PipelineContext) -> bytes:
        raw = Path(source.path).read_bytes()
        ctx.set(KEY_SOURCE_PATH, source.path)
        if raw[:4] == _INES_MAGIC and len(raw) >= 16:
            prg_banks, chr_banks = raw[4], raw[5]
            header_end = 16 + (512 if raw[6] & 0x04 else 0)
            if chr_banks > 0:
                start = header_end + prg_banks * 16384
                ctx.set(KEY_SOURCE_OFFSET, start)
                return raw[start : start + chr_banks * 8192]
            ctx.set(KEY_SOURCE_OFFSET, header_end)  # CHR-RAM: no CHR ROM to isolate
            return raw[header_end:]
        # Not an iNES file — behave like the raw reader.
        start = source.offset
        end = len(raw) if source.length is None else start + source.length
        ctx.set(KEY_SOURCE_OFFSET, source.offset)
        return raw[start:end]


class SmdReader:
    """Read a Sega ``.smd`` (Genesis) file, deinterleaving to contiguous ROM bytes.

    ``.smd`` has a 512-byte header, then 16 KB blocks storing all the odd bytes
    first, then all the even bytes. Each block is reconstructed by interleaving the
    two halves back together. Plain ``.md``/``.bin`` need no transform (use the raw
    reader); this reader always deinterleaves.
    """

    info = PluginInfo(id="read.smd", name="Sega .smd (deinterleave)", stage=Stage.READ)

    _HEADER = 512
    _BLOCK = 16384
    _HALF = 8192

    def read(self, source: FileRef, ctx: PipelineContext) -> bytes:
        raw = Path(source.path).read_bytes()
        ctx.set(KEY_SOURCE_PATH, source.path)
        ctx.set(KEY_SOURCE_OFFSET, self._HEADER)
        body = raw[self._HEADER :]
        blocks = len(body) // self._BLOCK
        out = bytearray(blocks * self._BLOCK)
        for i in range(blocks):
            src = i * self._BLOCK
            dst = i * self._BLOCK
            for j in range(self._HALF):
                out[dst + j * 2 + 1] = body[src + j]  # first half → odd positions
                out[dst + j * 2] = body[src + self._HALF + j]  # second half → even
        return bytes(out)
