"""The NES PPU write list — "put these bytes at that PPU address", as a stream.

Most NES screens that are not built from level data are stored this way: a title
screen, a status bar, an ending message. The game hands the list to its NMI
routine, which replays each record through ``$2006``/``$2007`` while the screen is
blanked or during vblank. It is not compression and is read as compression for
the reason a stripe image is: what it produces is the bytes the next stage
interprets. Decoded, the records land in a **nametable page** — 960 cells and
their 64-byte attribute plane — so the entry reads straight through
``format.tilemap.nes-nametable`` and draws in its own colours.

A record, as the replay loop reads it (``docs/rom-mapping/console-nes.md`` §5):

    byte 0:  PPU address, high byte -- $00 ends the list, and so does bit 7 set
    byte 1:  PPU address, low byte
    byte 2:  VRLLLLLL   V = step 32 (down a column), R = one byte repeated,
                        L = how many bytes are written (1-63)
    then:    L bytes, or one byte when R is set

The layout is one routine shared by a whole family of first-party cartridges;
what varies between them is only the terminator — ``$00`` where the list is a
RAM buffer the game appends to, ``$FF`` where it is a ROM table walked until the
high byte goes negative — and both are accepted, since neither is a PPU address.
A high byte of ``$40``-``$7F`` is not an address either, and a count of 0 is one
no list emits and replay loops disagree about, so both refuse: that is what
keeps a scan over random bytes from matching everywhere.

**The window** is the nametable pages the records touch, from the first to the
last, each whole — ``$2000``, ``$2400``, ``$2800``, ``$2C00``. Writes anywhere else
(the palette at ``$3F00``, pattern memory below ``$2000``) are replayed into
nothing and left untouched on a save, since no cell holds them. Which way two
pages sit on screen is the cartridge's mirroring, not the stream's, so they come
out in address order, as ``format.tilemap.nes-nametable`` reads them.

**What the untouched cells hold** is whatever the previous screen left there,
which the list cannot say. They decode as the ``fill`` input — default tile 0;
bind the game's blank tile to see the picture on its own background — and an
attribute of 0. The optional ``page`` input narrows the window to one page, for
a list that writes two.

**Saving rewrites the stream in place.** A list's shape — which records exist,
where each starts, which repeat and which step down a column — is not a function
of the picture, so :meth:`compress` does not invent one: it re-reads the
original list from the bytes around the slot (``KEY_SURROUND``) and puts each
visible cell's value back into the payload byte that wrote it. The result is the
original length, record for record. Two edits have no byte to go into and are
refused by name instead of approximated: changing a cell no record writes, and
setting the cells of one repeated run to different values.
"""

from __future__ import annotations

from dataclasses import dataclass

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    KEY_INPUTS,
    KEY_SURROUND,
    KEY_SURROUND_START,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import InputKind, InputSpec, PluginInfo

INPUT_PAGE = "page"
INPUT_FILL = "fill"

PAGE_BYTES = 0x400  # one nametable: 960 cells, then the 64-byte attribute plane
CELL_BYTES = 960
NAMETABLES = range(0x2000, 0x3000)
ADDRESS_MASK = 0x3FFF  # the PPU's address space is 14 bits


@dataclass(frozen=True)
class _Record:
    address: int
    vertical: bool
    repeat: bool
    count: int
    payload_at: int  # offset of the first payload byte in the stream

    def writes(self):
        """``(ppu address, payload offset)`` for every byte the record writes."""
        step = 32 if self.vertical else 1
        for k in range(self.count):
            at = self.payload_at + (0 if self.repeat else k)
            yield (self.address + k * step) & ADDRESS_MASK, at


def _scan(data: bytes) -> tuple[list[_Record], int, bool]:
    """``(records, consumed, complete)`` for the list at ``data[0]``."""
    records: list[_Record] = []
    i, n = 0, len(data)
    while i < n:
        high = data[i]
        if high == 0 or high & 0x80:
            if not records:
                raise ValueError("an empty list — no PPU write list here")
            return records, i + 1, True
        if high >= 0x40:
            raise ValueError(f"${high:02X}xx is not a PPU address")
        if i + 3 > n:
            break
        control = data[i + 2]
        count = control & 0x3F
        if not count:
            raise ValueError(f"a zero-length record at stream byte {i}")
        repeat = bool(control & 0x40)
        size = 1 if repeat else count
        if i + 3 + size > n:
            break
        records.append(
            _Record(
                (high << 8) | data[i + 1], bool(control & 0x80), repeat, count, i + 3
            )
        )
        i += 3 + size
    return records, i, False


def _window(records: list[_Record], inputs: dict) -> tuple[int, int]:
    """``(first PPU address, byte length)`` of the pages the picture covers."""
    page = inputs.get(INPUT_PAGE)
    if page is not None:
        if page not in NAMETABLES or page % PAGE_BYTES:
            raise ValueError(f"${page:04X} is not a nametable page ($2000-$2C00)")
        return page, PAGE_BYTES
    pages = {
        address - address % PAGE_BYTES
        for record in records
        for address, _ in record.writes()
        if address in NAMETABLES
    }
    if not pages:
        return 0x2000, 0
    return min(pages), max(pages) + PAGE_BYTES - min(pages)


def _blank(base: int, length: int, fill: int) -> bytearray:
    out = bytearray()
    for _ in range(length // PAGE_BYTES):
        out += bytes([fill]) * CELL_BYTES + bytes(PAGE_BYTES - CELL_BYTES)
    return out


def _visible(
    records: list[_Record], base: int, length: int
) -> dict[int, tuple[int, int]]:
    """Window position → ``(record number, payload offset)`` of its **last** writer.

    The last because the replay is in order: a record overwritten by a later one
    (a backdrop run with a logo drawn over it) is invisible there, and a save
    must leave its byte alone rather than copy the logo into it.
    """
    last: dict[int, tuple[int, int]] = {}
    for number, record in enumerate(records):
        for address, at in record.writes():
            if base <= address < base + length:
                last[address - base] = (number, at)
    return last


class NesPpuListCompression:
    info = PluginInfo(
        id="compression.nes-ppu-list",
        name="NES PPU write list (address, count, bytes)",
        stage=Stage.COMPRESSION,
        # The terminator bounds the list, so tables of them sit back to back.
        self_delimiting=True,
        category="Nintendo",
        inputs=(
            InputSpec(
                INPUT_PAGE,
                "Nametable page",
                InputKind.INTEGER,
                required=False,
                minimum=0x2000,
                maximum=0x2C00,
                tooltip=(
                    "Replay only the writes into this nametable page\n"
                    "($2000, $2400, $2800 or $2C00),\n"
                    "for a list that draws on two."
                ),
            ),
            InputSpec(
                INPUT_FILL,
                "Blank tile",
                InputKind.INTEGER,
                required=False,
                minimum=0,
                maximum=0xFF,
                default=0,
                tooltip=(
                    "The tile a cell the list never writes shows.\n"
                    "The machine has the previous screen there,\n"
                    "so pick the game's blank tile."
                ),
            ),
        ),
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        records, consumed, complete = _scan(data)
        if not complete and not ctx.get(KEY_DECOMPRESS_PARTIAL):
            raise ValueError("no terminator — no PPU write list here")
        inputs = ctx.get(KEY_INPUTS) or {}
        base, length = _window(records, inputs)
        out = _blank(base, length, inputs.get(INPUT_FILL, 0))
        for position, (_, at) in _visible(records, base, length).items():
            out[position] = data[at]
        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        ctx.set(KEY_DECOMPRESS_COMPLETE, complete)
        return bytes(out)

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        surround = ctx.get(KEY_SURROUND)
        start = ctx.get(KEY_SURROUND_START)
        if surround is None or not isinstance(start, int):
            raise ValueError(
                "a PPU write list is saved into its own records, and the "
                "original list is not readable here"
            )
        original = bytes(surround[start:])
        records, consumed, complete = _scan(original)
        if not complete:
            raise ValueError("the list at this slot has no terminator")
        inputs = ctx.get(KEY_INPUTS) or {}
        base, length = _window(records, inputs)
        if len(data) != length:
            raise ValueError(
                f"the picture is {len(data)} bytes; the list draws {length}"
            )
        blank = _blank(base, length, inputs.get(INPUT_FILL, 0))
        visible = _visible(records, base, length)
        for position in range(length):
            if position not in visible and data[position] != blank[position]:
                raise ValueError(
                    f"${base + position:04X} is not written by this list, "
                    "so there is no byte to save it in"
                )

        out = bytearray(original[:consumed])
        repeated: dict[int, set[int]] = {}
        for position, (number, at) in visible.items():
            if records[number].repeat:
                repeated.setdefault(number, set()).add(data[position])
            else:
                out[at] = data[position]
        for number, values in repeated.items():
            if len(values) > 1:
                record = records[number]
                raise ValueError(
                    f"the {record.count} bytes from ${record.address:04X} are one "
                    "repeated byte in the list; give them all the same value"
                )
            out[records[number].payload_at] = values.pop()
        return bytes(out)
