"""Game Boy / Game Boy Color ROM container — checksum repair on write.

The odd one among the containers: it strips nothing. A ``.gb`` file *is* its
bytes, so Read is the plain reader with a signature attached. What makes it a
container is the write half — a GB ROM carries two checksums over its own
header, and editing tiles anywhere in the file invalidates the second of them:

- **Header checksum** at ``0x14D``: ``x = x - byte - 1`` over ``0x134..0x14C``
  (the title, cartridge type, ROM/RAM size and region fields). The boot ROM
  verifies this one and **refuses to run the cartridge** if it disagrees, which
  is why a hand-edited ROM can come back as a blank screen on hardware and on
  accurate emulators. Tile edits never touch that range, so this stays correct
  on its own — it is recomputed anyway, because a container that owns an
  invariant should leave it true rather than merely undamaged.
- **Global checksum** at ``0x14E`` (big-endian, and *excluding* its own two
  bytes): the sum of every byte in the ROM. Graphics live inside that sum, so
  this one genuinely does go stale on every edit. No boot ROM checks it, but
  cartridge tooling and ROM databases do.

Both are repaired after the edited bytes are spliced in, so what is checksummed
is the file as it will exist. A file too short to hold a header
(``< 0x150`` bytes) is written through untouched: there is nothing to repair and
inventing a header would corrupt whatever it actually is.

See ``docs/graphics-formats-reference/implementation-guide.md`` §5.
"""

from __future__ import annotations

from pathlib import Path

from celpix.core.context import KEY_SOURCE_OFFSET, KEY_SOURCE_PATH, PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import FileRef, PluginInfo

# The Nintendo logo the boot ROM compares against, at 0x104. Every ROM that runs
# on hardware carries it byte-for-byte, which makes its head a far better
# identifier than the file suffix (a ``.gb`` may be any dump at all).
_LOGO_AT = 0x104
_LOGO_HEAD = bytes([0xCE, 0xED, 0x66, 0x66, 0xCC, 0x0D, 0x00, 0x0B])

_HEADER_SUM_RANGE = (0x134, 0x14D)  # [start, end) — the fields the boot ROM sums
_HEADER_SUM_AT = 0x14D
_GLOBAL_SUM_AT = 0x14E
_HEADER_END = 0x150  # smallest file that has a header at all


def repair_checksums(rom: bytes) -> bytes:
    """``rom`` with both header checksums recomputed; short input untouched."""
    if len(rom) < _HEADER_END:
        return rom
    out = bytearray(rom)
    start, end = _HEADER_SUM_RANGE
    header_sum = 0
    for byte in out[start:end]:
        header_sum = (header_sum - byte - 1) & 0xFF
    out[_HEADER_SUM_AT] = header_sum
    # The global sum covers the whole ROM except the two bytes holding it, so
    # zero them first and sum what is left — self-reference resolved the way the
    # format defines it, rather than by subtracting the previous value (which
    # would carry a wrong one forward).
    out[_GLOBAL_SUM_AT : _GLOBAL_SUM_AT + 2] = b"\x00\x00"
    total = sum(out) & 0xFFFF
    out[_GLOBAL_SUM_AT : _GLOBAL_SUM_AT + 2] = total.to_bytes(2, "big")
    return bytes(out)


class GbRomReader:
    info = PluginInfo(
        id="read.gb-rom",
        name="Game Boy ROM (checksum repair on write)",
        stage=Stage.READ,
        extensions=(".gb", ".gbc"),
        magic=((_LOGO_AT, _LOGO_HEAD),),
        short_name="GB",
    )

    def read(self, source: FileRef, ctx: PipelineContext) -> bytes:
        # Identical to the raw reader: the container's whole job is on the write
        # side, and a GB ROM's bytes need no rearranging to be decoded.
        in_memory = source.data is not None
        raw = source.data if in_memory else Path(source.path).read_bytes()
        start = max(0, source.offset - (source.data_base if in_memory else 0))
        end = len(raw) if source.length is None else start + source.length
        ctx.set(KEY_SOURCE_PATH, source.path)
        ctx.set(KEY_SOURCE_OFFSET, source.offset)
        return raw[start:end]


class GbRomWriter:
    info = PluginInfo(
        id="write.gb-rom",
        name="Game Boy ROM (checksum repair on write)",
        stage=Stage.WRITE,
    )

    def write(self, data: bytes, dest: FileRef, ctx: PipelineContext) -> None:
        path = Path(dest.path)
        existing = bytearray(path.read_bytes()) if path.exists() else bytearray()
        end = dest.offset + len(data)
        if len(existing) < end:
            existing.extend(b"\x00" * (end - len(existing)))
        existing[dest.offset : end] = data
        # Whole-file write into nothing: the spliced buffer *is* the ROM. Either
        # way the checksums are computed over the final bytes.
        path.write_bytes(repair_checksums(bytes(existing)))
