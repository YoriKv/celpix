"""SNES / Super Famicom ROM container — checksum repair on write.

Like the Game Boy and Master System ones, this container's point is its write
half. A SNES cartridge carries a 64-byte internal header at the top of its first
bank — CPU ``$00:FFC0``, which is image offset ``$7FC0`` on a LoROM board,
``$FFC0`` on HiROM and ``$40FFC0`` on ExHiROM — holding a 16-bit little-endian
**checksum** at ``+$1E`` and its **complement** at ``+$1C``, the two always
XORing to ``$FFFF``. The checksum is the sum of every byte in the image, so the
graphics sit inside it and a tile edit written back through a plain container
leaves it stale. No console verifies it, but emulators flag a cartridge whose
copy disagrees, and ROM databases and patch tooling identify one by it
(``docs/rom-mapping/console-snes.md`` §1).

**The header is found by scoring**, there being no marker to look for: each of
the three positions is judged on whether its checksum pair is complementary,
its map-mode byte names the board that position belongs to, its title is
printable and its ROM-size byte fits the image, and a candidate whose reset
vector points below ``$8000`` — where no cartridge has code — is thrown out
(:func:`header_offset`). An image where nothing scores adequately is written
through untouched: there is no header to keep current, and writing four bytes
into whatever the file actually is would corrupt it.

**The sum mirrors an odd-sized image.** A ROM that is not a power of two long
is split into its largest power-of-two part and the remainder, and the
remainder counts as many times as it takes to fill out the first part — which
is how the address decoder repeats the smaller chip on a two-chip board
(:func:`checksum`). The pair itself is summed as ``$FFFF`` and ``$0000``, the
values it cannot contradict.

**A copier header is this container's to handle**, containers not stacking: a
file 512 bytes over a whole number of KiB is read past its first 512 bytes
exactly as ``container.copier-header`` reads it, and a save preserves them. The
checksum never covers them.

**It is picked by hand**, in Edit File Container, and claims no file on its own.
The header has no constant bytes for detection to match — the title, the board
and the sizes all vary — and a suffix alone would take every headered dump away
from ``container.copier-header``, which is the right answer for a file that is
being read and not rebuilt. Rewriting four bytes of a cartridge on save is also
something to ask for rather than to be given.

The sum is recomputed after the edited bytes are spliced in, so what is summed
is the file as it will exist. That holds for a file whose stored checksum was
already wrong — many hacks and prototypes — which opens like any other and gets
a correct one on its first save, edited or not.
"""

from __future__ import annotations

from dataclasses import replace

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

from .containers import snes_copier_header_len

# Where the internal header may sit in the headerless image, and the low nibbles
# of the map-mode byte that belong at each: the board decides both, so a header
# that is really there says so itself. $x2 and $x3 are the LoROM-layout boards
# with a decompression or co-processor chip, $xA the HiROM-layout one.
HEADER_AT = {
    0x7FC0: ("LoROM", frozenset({0x0, 0x2, 0x3})),
    0xFFC0: ("HiROM", frozenset({0x1, 0xA})),
    0x40FFC0: ("ExHiROM", frozenset({0x5})),
}
HEADER_LEN = 0x40
TITLE_LEN = 21
MAP_MODE = 0x15  # within the header
ROM_SIZE = 0x17  # log2 of the ROM's size in KiB
COMPLEMENT = 0x1C  # little-endian word, checksum ^ $FFFF
CHECKSUM = 0x1E  # little-endian word
RESET_VECTOR = 0x3C  # little-endian word, CPU $00:FFFC

# What a candidate earns for each thing it gets right, and what it needs to be
# believed. The pair is worth most because nothing but a header has one, yet it
# is not required: a hack that scribbled over it still has a board, a title and
# a size, and those three together are as good. Any single one is not — data
# passes one of these tests by accident all the time.
_PAIR, _MAP, _TITLE, _SIZE = 3, 2, 1, 1
_ADEQUATE = 4


def _word(image: bytes, at: int) -> int:
    return int.from_bytes(image[at : at + 2], "little")


def _score(image: bytes, at: int) -> int:
    """How much the 64 bytes at ``at`` look like this image's internal header."""
    if len(image) < at + HEADER_LEN:
        return 0
    header = image[at : at + HEADER_LEN]
    # The one disqualifier rather than a point lost: the vector is where the CPU
    # starts, the cartridge is only mapped from $8000 up, and a ROM that boots
    # cannot break that — so bytes that do are not a header, whatever else fits.
    if _word(header, RESET_VECTOR) < 0x8000:
        return 0
    score = 0
    if _word(header, COMPLEMENT) ^ _word(header, CHECKSUM) == 0xFFFF:
        score += _PAIR
    mode = header[MAP_MODE]
    if mode & 0xE0 == 0x20 and mode & 0x0F in HEADER_AT[at][1]:
        score += _MAP
    # ASCII, or the half-width katakana a Japanese title is spelled in.
    if all(0x20 <= c <= 0x7E or 0xA1 <= c <= 0xDF for c in header[:TITLE_LEN]):
        score += _TITLE
    # The size byte rounds up to a power of two, so it may name up to twice
    # what an odd-sized image holds and never less than it.
    if len(image) <= (1024 << min(header[ROM_SIZE], 16)) < 2 * len(image):
        score += _SIZE
    return score


def header_offset(image: bytes) -> int | None:
    """Where the internal header sits in the headerless ``image``, or None when
    nothing there is convincingly one.

    The best-scoring position wins and the earlier one takes a tie, the order
    being commonest board first.
    """
    best, best_score = None, _ADEQUATE - 1
    for at in HEADER_AT:
        score = _score(image, at)
        if score > best_score:
            best, best_score = at, score
    return best


def _mirrored_sum(image: memoryview) -> tuple[int, int]:
    """The byte sum of ``image`` mirrored out to a power of two, and that length.

    Recursive because the remainder follows the same rule: a 6 MiB image is
    4 + 2, a 2.5 MiB one is 2 + (0.5 mirrored to 2) — each level repeats what is
    left until it fills the part before it.
    """
    size = len(image)
    first = 1 << (size.bit_length() - 1)
    total = sum(image[:first])
    if size == first:
        return total, first
    rest, rest_len = _mirrored_sum(image[first:])
    return total + rest * (first // rest_len), 2 * first


def checksum(image: bytes, header: int) -> int:
    """The internal header's checksum over the headerless ``image``.

    The pair is summed as ``$FFFF`` and ``$0000`` rather than as found. Those are
    the values the sum is defined over, and substituting them — instead of
    correcting for the four bytes afterwards — stays right when the header lies
    in the mirrored remainder and so counts more than once.
    """
    summed = bytearray(image)
    summed[header + COMPLEMENT : header + CHECKSUM + 2] = b"\xff\xff\x00\x00"
    return _mirrored_sum(memoryview(summed))[0] & 0xFFFF


def repair_checksum(rom: bytes) -> bytes:
    """``rom`` with the checksum and its complement recomputed; a file with no
    recognisable header untouched. A copier header is skipped and preserved."""
    skip = snes_copier_header_len(len(rom))
    image = rom[skip:]
    header = header_offset(image)
    if header is None:
        return rom
    total = checksum(image, header)
    out = bytearray(rom)
    at = skip + header + COMPLEMENT
    out[at : at + 4] = (total ^ 0xFFFF).to_bytes(2, "little") + total.to_bytes(
        2, "little"
    )
    return bytes(out)


class SnesRomContainer:
    info = PluginInfo(
        id="container.snes-rom",
        name="SNES ROM (checksum repair on write)",
        stage=Stage.CONTAINER,
        short_name="SNES",
        category="Nintendo",
        preserves_offsets=True,
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        # The plain read, begun past a copier header when there is one — the same
        # 512 bytes by the same rule as the copier-header container, so an address
        # quoted against the cartridge lands identically under either.
        skip = snes_copier_header_len(len(source.data))
        if source.offset >= skip:
            return plain_read(source, ctx)
        length = source.length
        if length is not None:
            length = max(0, length - (skip - source.offset))
        return plain_read(replace(source, offset=skip, length=length), ctx)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        # The destination's own length decides, as the read's did, so the two
        # cannot disagree about where the image starts. With nothing there yet
        # the payload becomes a headerless ROM, which is a whole file.
        at = max(dest.offset, snes_copier_header_len(len(dest.existing)))
        return repair_checksum(splice(dest.existing, at, data))

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        raw = source.data
        skip = snes_copier_header_len(len(raw))
        image = raw[skip:]
        copier = ContainerField(
            "Copier header",
            f"{skip} bytes, skipped" if skip else "none",
            "Spotted by the file being 512 bytes over a whole number\n"
            "of KiB. Skipped on read so offsets are the cartridge's,\n"
            "preserved on write, and never part of the checksum.",
        )
        header = header_offset(image)
        if header is None:
            return (
                copier,
                ContainerField(
                    "Header",
                    "no internal header found",
                    "Nothing at $7FC0, $FFC0 or $40FFC0 reads as a cartridge\n"
                    "header, so there is no checksum to keep current and a\n"
                    "save writes the bytes through untouched.",
                ),
            )
        stored = _word(image, header + CHECKSUM)
        complement = _word(image, header + COMPLEMENT)
        computed = checksum(image, header)
        # Shift-JIS rather than ASCII: its single-byte range is the half-width
        # katakana a Japanese cartridge spells its title in.
        title = bytes(image[header : header + TITLE_LEN]).decode("shift_jis", "replace")
        mirrored = _mirrored_sum(memoryview(image))[1]
        return (
            copier,
            ContainerField(
                "Header",
                f"{HEADER_AT[header][0]}, at ${header:04X}: {title.strip()}",
                "The internal header at the top of the first bank. Its\n"
                "position is the board's, and is settled by scoring the\n"
                "three candidates: checksum pair, map mode, title, size.",
            ),
            ContainerField(
                "Checksum",
                f"${stored:04X} stored, ${computed:04X} computed"
                + (" - matches" if stored == computed else " - stale"),
                "The sum of every byte in the image. Graphics live inside\n"
                "it, so every tile edit makes it stale; emulators and ROM\n"
                "databases check it, and a save recomputes it.",
            ),
            ContainerField(
                "Complement",
                f"${complement:04X}"
                + (
                    " - matches"
                    if complement == computed ^ 0xFFFF
                    else f", ${computed ^ 0xFFFF:04X} correct"
                ),
                "The checksum with every bit flipped, stored beside it.\n"
                "A save rewrites the two together.",
            ),
            ContainerField(
                "Summed size",
                format_size(len(image))
                + (
                    f", mirrored to {format_size(mirrored)}"
                    if mirrored != len(image)
                    else ""
                ),
                "An image that is not a power of two long is summed as\n"
                "the hardware maps it: the part past the largest power\n"
                "of two repeats until it fills out the first part.",
            ),
        )
