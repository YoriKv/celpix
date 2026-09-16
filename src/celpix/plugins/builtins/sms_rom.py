"""Master System / Game Gear ROM container — checksum repair on write.

Like the Game Boy one, this container strips nothing: a ``.sms`` or ``.gg`` file
*is* its bytes, so the read is the plain one with a signature attached. The
write half is the point. A Sega 8-bit cartridge carries a 16-byte header —
``TMR SEGA`` at ``$7FF0`` (``$1FF0`` or ``$3FF0`` on the smallest ROMs) — with a
16-bit little-endian checksum at ``+$0A``: the sum of every byte in the ROM
*except the header's own sixteen*, over the range the size nibble at ``+$0F``
names. The BIOS of an export Master System verifies it and refuses a cartridge
whose copy disagrees; the graphics sit inside the summed range, so a tile edit
written back through a plain container leaves the sum stale
(``docs/rom-mapping/console-master-system.md`` §1).

The sum is recomputed after the edited bytes are spliced in, so what is summed
is the file as it will exist. A file with no ``TMR SEGA`` at any of the three
offsets is written through untouched: there is no header to keep current, and
inventing one would corrupt whatever the file actually is.
"""

from __future__ import annotations

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import (
    ContainerField,
    PluginInfo,
    ReadSource,
    WriteTarget,
    format_size,
    plain_read,
    splice,
)

MAGIC = b"TMR SEGA"
# Where the header may sit: $7FF0 on every cartridge of 32 KiB or more, the
# two lower slots on the 8 and 16 KiB ROMs the BIOS also checks.
HEADER_AT = (0x7FF0, 0x3FF0, 0x1FF0)
HEADER_LEN = 16
CHECKSUM = 0x0A  # within the header, little-endian word
SIZE_BYTE = 0x0F  # low nibble: how much of the ROM the BIOS sums
# The size nibble names the summed length; the header's own region is skipped
# whatever the size.
SUMMED_BYTES = {
    0xA: 8 * 1024,
    0xB: 16 * 1024,
    0xC: 32 * 1024,
    0xD: 48 * 1024,
    0xE: 64 * 1024,
    0xF: 128 * 1024,
    0x0: 256 * 1024,
    0x1: 512 * 1024,
    0x2: 1024 * 1024,
}


def header_offset(rom: bytes) -> int | None:
    """Where the ``TMR SEGA`` header sits, or None when the file has none."""
    for at in HEADER_AT:
        if rom[at : at + len(MAGIC)] == MAGIC:
            return at
    return None


def summed_length(rom: bytes, header: int) -> int:
    """How many bytes the header says the checksum covers, bounded by the file."""
    nibble = rom[header + SIZE_BYTE] & 0x0F
    return min(len(rom), SUMMED_BYTES.get(nibble, len(rom)))


def checksum(rom: bytes, header: int) -> int:
    """The BIOS's sum: every byte outside the 16-byte header, modulo 65536."""
    end = summed_length(rom, header)
    return (sum(rom[:header]) + sum(rom[header + HEADER_LEN : end])) & 0xFFFF


def repair_checksum(rom: bytes) -> bytes:
    """``rom`` with the header checksum recomputed; a headerless file untouched."""
    header = header_offset(rom)
    if header is None or len(rom) < header + HEADER_LEN:
        return rom
    out = bytearray(rom)
    total = checksum(rom, header)
    out[header + CHECKSUM : header + CHECKSUM + 2] = total.to_bytes(2, "little")
    return bytes(out)


class SmsRomContainer:
    info = PluginInfo(
        id="container.sms-rom",
        name="Master System / Game Gear ROM (checksum repair on write)",
        stage=Stage.CONTAINER,
        extensions=(".sms", ".gg"),
        magic=tuple((at, MAGIC) for at in HEADER_AT),
        short_name="SMS",
        category="Sega",
        preserves_offsets=True,
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        # Identical to the raw container: the bytes decode where they lie, so
        # this container's whole job is on the write side.
        return plain_read(source, ctx)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        return repair_checksum(splice(dest.existing, dest.offset, data))

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        raw = source.data
        header = header_offset(raw)
        if header is None:
            return (
                ContainerField(
                    "Header",
                    "no TMR SEGA marker",
                    "Without the marker there is no checksum to keep\n"
                    "current, and a save writes the bytes through untouched.",
                ),
            )
        stored = int.from_bytes(
            raw[header + CHECKSUM : header + CHECKSUM + 2], "little"
        )
        computed = checksum(raw, header)
        return (
            ContainerField(
                "Header",
                f"TMR SEGA at ${header:04X}",
                "The 16-byte cartridge header the BIOS reads: product\n"
                "code, version, region and size, and the checksum below.",
            ),
            ContainerField(
                "Checksum",
                f"${stored:04X} stored, ${computed:04X} computed"
                + (" - matches" if stored == computed else " - stale"),
                "The sum of every byte outside the header, over the range\n"
                "the size nibble names. An export Master System refuses a\n"
                "cartridge whose copy disagrees; a save recomputes it.",
            ),
            ContainerField(
                "Summed range",
                f"{format_size(summed_length(raw, header))} less the header",
                "From the size nibble: $C = 32 KiB up to $0 = 256 KiB,\n"
                "$1 and $2 the 512 KiB and 1 MiB extensions.",
            ),
        )
