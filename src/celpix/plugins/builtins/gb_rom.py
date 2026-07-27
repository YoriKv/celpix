"""Game Boy / Game Boy Color ROM container — checksum repair on write.

The one container that strips nothing: a ``.gb`` file *is* its bytes, so its read
is the plain one with a signature attached. The write half is what makes it a
container — a GB ROM carries two checksums over its own header:

- **Header checksum** at ``0x14D``: ``x = x - byte - 1`` over ``0x134..0x14C``
  (title, cartridge type, ROM/RAM size, region). The boot ROM verifies this and
  **refuses to run the cartridge** if it disagrees, which is why a hand-edited ROM
  can come back as a blank screen on hardware and accurate emulators. Tile edits
  never touch that range, but it is recomputed anyway: a container owning an
  invariant should leave it true rather than merely undamaged.
- **Global checksum** at ``0x14E``, big-endian and *excluding* its own two bytes:
  the sum of every byte in the ROM. Graphics live inside that sum, so this one
  goes stale on every edit. No boot ROM checks it, but cartridge tooling and ROM
  databases do.

Both are repaired after the edited bytes are spliced in, so what is checksummed is
the file as it will exist. A file too short to hold a header (``< 0x150`` bytes)
is written through untouched — there is nothing to repair, and inventing a header
would corrupt whatever it actually is.

See ``docs/graphics-formats-reference/implementation-guide.md`` §5.
"""

from __future__ import annotations

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import (
    PluginInfo,
    ReadSource,
    WriteTarget,
    plain_read,
    splice,
)

# The Nintendo logo the boot ROM compares against, at 0x104. Every ROM that runs
# on hardware carries it byte-for-byte, making its head a better identifier than
# the file suffix — a `.gb` may be any dump at all.
_LOGO_AT = 0x104
_LOGO_HEAD = bytes([0xCE, 0xED, 0x66, 0x66, 0xCC, 0x0D, 0x00, 0x0B])

_HEADER_SUM_RANGE = (0x134, 0x14D)  # [start, end) — the fields the boot ROM sums
_HEADER_SUM_AT = 0x14D
_GLOBAL_SUM_AT = 0x14E
_HEADER_END = 0x150  # smallest file that has a header at all


def repair_checksums(rom: bytes) -> bytes:
    """``rom`` with the header and global checksums recomputed; short input
    untouched."""
    if len(rom) < _HEADER_END:
        return rom
    out = bytearray(rom)
    start, end = _HEADER_SUM_RANGE
    header_sum = 0
    for byte in out[start:end]:
        header_sum = (header_sum - byte - 1) & 0xFF
    out[_HEADER_SUM_AT] = header_sum
    # The global sum covers the whole ROM except the two bytes holding it, so
    # zero them first and sum what is left. Subtracting the previous value
    # instead would carry a wrong one forward.
    out[_GLOBAL_SUM_AT : _GLOBAL_SUM_AT + 2] = b"\x00\x00"
    total = sum(out) & 0xFFFF
    out[_GLOBAL_SUM_AT : _GLOBAL_SUM_AT + 2] = total.to_bytes(2, "big")
    return bytes(out)


class GbRomContainer:
    info = PluginInfo(
        id="container.gb-rom",
        name="Game Boy ROM (checksum repair on write)",
        stage=Stage.CONTAINER,
        extensions=(".gb", ".gbc"),
        magic=((_LOGO_AT, _LOGO_HEAD),),
        short_name="GB",
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        # Identical to the raw container: a GB ROM's bytes need no rearranging to
        # be decoded, so this container's whole job is on the write side.
        return plain_read(source, ctx)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        # A whole-file write into nothing leaves the spliced buffer *as* the ROM.
        # Either way the checksums are computed over the final bytes.
        return repair_checksums(splice(dest.existing, dest.offset, data))
