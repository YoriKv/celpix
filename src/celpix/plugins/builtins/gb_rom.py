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
    ContainerField,
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
        category="Nintendo",
        preserves_offsets=True,
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        # Identical to the raw container: a GB ROM's bytes need no rearranging to
        # be decoded, so this container's whole job is on the write side.
        return plain_read(source, ctx)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        # A whole-file write into nothing leaves the spliced buffer *as* the ROM.
        # Either way the checksums are computed over the final bytes.
        return repair_checksums(splice(dest.existing, dest.offset, data))

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        # Every field here is one the *write* half acts on: the read strips
        # nothing, so what is worth reporting is what a save would change.
        raw = source.data
        logo = raw[_LOGO_AT : _LOGO_AT + len(_LOGO_HEAD)] == _LOGO_HEAD
        fields = [
            ContainerField(
                "Boot logo",
                "matches" if logo else "does not match",
                "The bitmap at 0x104 the boot ROM compares against. It\n"
                "identifies the file as a real cartridge dump, which the\n"
                "suffix alone does not; nothing is read past it.",
            ),
            ContainerField(
                "Payload",
                "the whole file, unchanged",
                "A Game Boy ROM needs no unwrapping - its bytes decode\n"
                "where they lie. This container exists for its write\n"
                "half, which repairs the two checksums below.",
            ),
        ]
        if len(raw) < _HEADER_END:
            fields.append(
                ContainerField(
                    "Checksums",
                    "no header to repair",
                    f"A file under {_HEADER_END:#x} bytes holds no cartridge\n"
                    "header, so a save writes it through untouched rather\n"
                    "than inventing one.",
                )
            )
            return tuple(fields)
        repaired = repair_checksums(bytes(raw))
        stored_header = raw[_HEADER_SUM_AT]
        stored_global = int.from_bytes(raw[_GLOBAL_SUM_AT : _GLOBAL_SUM_AT + 2], "big")
        fields.append(
            ContainerField(
                "Header checksum",
                _sum_value(stored_header, repaired[_HEADER_SUM_AT], "02X"),
                "The byte at 0x14D, summed over the title and cartridge\n"
                "fields. The boot ROM refuses to run a cartridge whose\n"
                "copy disagrees, so a save always recomputes it.",
            )
        )
        fields.append(
            ContainerField(
                "Global checksum",
                _sum_value(
                    stored_global,
                    int.from_bytes(
                        repaired[_GLOBAL_SUM_AT : _GLOBAL_SUM_AT + 2], "big"
                    ),
                    "04X",
                ),
                "The word at 0x14E, the sum of every other byte in the\n"
                "ROM. Graphics live inside it, so every tile edit makes\n"
                "it stale; no boot ROM checks it, but cartridge tooling\n"
                "and ROM databases do.",
            )
        )
        return tuple(fields)


def _sum_value(stored: int, computed: int, spec: str) -> str:
    """``0xNN`` when the file's copy is right, both values when it is not.

    Which it is, is the only question worth answering here: a dump whose
    checksums already disagree was edited by something that did not repair them,
    and a save through celPix will quietly correct it.
    """
    if stored == computed:
        return f"0x{stored:{spec}} (correct)"
    return f"0x{stored:{spec}} in file, 0x{computed:{spec}} correct"
