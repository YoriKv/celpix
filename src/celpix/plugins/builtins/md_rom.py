"""Mega Drive / Genesis ROM container — checksum repair on write.

Like the Master System one, this container strips nothing: a ``.md``, ``.gen``
or ``.bin`` image *is* its bytes, since the 68000 addresses ROM linearly from
``$000000``. The write half is the point. The cartridge header at ``$100`` opens
with ``SEGA`` (the TMSS boot code insists on it, at ``$100`` or ``$101``) and
carries a big-endian **checksum word at ``$18E``**: the 16-bit sum of every
big-endian word from ``$200`` to the ROM end the header names at ``$1A4``.

The console itself never checks it, but many games do, in their own boot code,
and a mismatch sends them to a red screen or a lock-up rather than the title. The
fonts and graphics sit inside the summed range, so a tile edit written back
through a plain container leaves the sum stale
(``docs/rom-mapping/console-mega-drive.md`` §1.1).

The sum is recomputed after the edited bytes are spliced in, so what is summed
is the file as it will exist. A file with no ``SEGA`` at ``$100``/``$101`` is
written through untouched: there is no header to keep current, and inventing one
would corrupt whatever the file actually is.

So is a **Mega-CD disc image**. Its first sector carries the same header layout
at ``$100``, ``SEGA`` included, so detection, which matches on that magic,
claims a 2048-byte-sector image too; but no loop sums a disc, and ``$18E``
there is the disc's own data. The disc opens with a system identifier a
cartridge's vector table never spells (:func:`is_disc_image`), and a save
checks for it before touching anything.
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

MAGIC = b"SEGA"
# " SEGA MEGA DRIVE" is as common as "SEGA MEGA DRIVE " and TMSS accepts both.
MAGIC_AT = (0x100, 0x101)
CONSOLE_NAME = slice(0x100, 0x110)
CHECKSUM_AT = 0x18E  # big-endian word
ROM_END_AT = 0x1A4  # big-endian long: the last ROM address, inclusive
SUM_START = 0x200  # the vector table and the header itself are not summed

# The system identifiers a Mega-CD disc's first sector opens with. A cartridge
# starts with its initial stack pointer, which is never this text. ``SEGADISC``
# also covers ``SEGADISCSYSTEM``, the one every retail disc carries.
DISC_IDS = (b"SEGADISC", b"SEGABOOTDISC", b"SEGADATADISC")
# At the start of a 2048-byte-sector image, or past the 16-byte sync and sector
# header of a raw 2352-byte-sector one.
DISC_ID_AT = (0x00, 0x10)


def is_disc_image(rom: bytes) -> bool:
    """Whether ``rom`` is a Mega-CD disc image rather than a cartridge."""
    return any(rom.startswith(DISC_IDS, at) for at in DISC_ID_AT)


def has_header(rom: bytes) -> bool:
    """Whether ``rom`` is a cartridge carrying the ``SEGA`` marker the checksum
    lives beside."""
    if is_disc_image(rom):
        return False
    return any(rom[at : at + len(MAGIC)] == MAGIC for at in MAGIC_AT)


def summed_end(rom: bytes) -> int:
    """Where the checksum stops: the header's ROM end, bounded by the file.

    The games' own loops read the end from ``$1A4`` and sum through that byte
    inclusive, a word at a time — so an odd end address (the usual ``$7FFFF``)
    still takes the word it falls in, and an overdump padded past the end is not
    summed. A field that is zero, below ``$200`` or past the file is not one any
    loop could have run to, so the file's own length stands in.
    """
    end = int.from_bytes(rom[ROM_END_AT : ROM_END_AT + 4], "big")
    stop = (end | 1) + 1
    return stop if SUM_START < stop <= len(rom) else len(rom)


def checksum(rom: bytes) -> int:
    """The games' sum: big-endian words from ``$200`` to :func:`summed_end`,
    modulo 65536. An odd trailing byte is the high half of a word whose low
    half the 68000 would read as open bus; it counts as zero."""
    body = rom[SUM_START : summed_end(rom)]
    if len(body) % 2:
        body += b"\x00"
    evens = sum(body[0::2])
    odds = sum(body[1::2])
    return ((evens << 8) + odds) & 0xFFFF


def repair_checksum(rom: bytes) -> bytes:
    """``rom`` with the header checksum recomputed; a headerless file untouched."""
    if len(rom) < SUM_START or not has_header(rom):
        return rom
    out = bytearray(rom)
    out[CHECKSUM_AT : CHECKSUM_AT + 2] = checksum(rom).to_bytes(2, "big")
    return bytes(out)


class MdRomContainer:
    info = PluginInfo(
        id="container.md-rom",
        name="Mega Drive / Genesis ROM (checksum repair on write)",
        stage=Stage.CONTAINER,
        extensions=(".md", ".gen", ".bin"),
        magic=tuple((at, MAGIC) for at in MAGIC_AT),
        short_name="MD",
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
        if is_disc_image(raw):
            return (
                ContainerField(
                    "Header",
                    "Mega-CD disc image",
                    "A disc, not a cartridge: its header has no checksum a\n"
                    "game sums, and a save writes the bytes through untouched.",
                ),
            )
        if len(raw) < SUM_START or not has_header(raw):
            return (
                ContainerField(
                    "Header",
                    "no SEGA marker at $100",
                    "Without the marker there is no checksum to keep\n"
                    "current, and a save writes the bytes through untouched.",
                ),
            )
        name = raw[CONSOLE_NAME].decode("ascii", "replace").strip()
        stored = int.from_bytes(raw[CHECKSUM_AT : CHECKSUM_AT + 2], "big")
        computed = checksum(raw)
        end = summed_end(raw)
        return (
            ContainerField(
                "Header",
                f'"{name}" at $100',
                "The cartridge header: console name, titles, serial,\n"
                "the checksum below and the ROM and RAM ranges.",
            ),
            ContainerField(
                "Checksum",
                f"${stored:04X} stored, ${computed:04X} computed"
                + (" - matches" if stored == computed else " - stale"),
                "The sum of the big-endian words from $200 to the ROM end.\n"
                "The console never checks it, but many games do at boot\n"
                "and stop on a mismatch; a save recomputes it.",
            ),
            ContainerField(
                "Summed range",
                f"$200-${end - 1:X} ({format_size(end - SUM_START)})",
                "Up to the ROM end the header names at $1A4, so padding\n"
                "past it is not summed; the file's length when the field\n"
                "is missing or runs past the file.",
            ),
        )
