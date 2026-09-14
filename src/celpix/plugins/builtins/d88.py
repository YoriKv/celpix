"""The ``.d88`` floppy disk image, read as the flat sector data a loader addresses.

The image format of the Japanese home-computer emulators — PC-8801, PC-9801,
FM-7 (``.d77``), X1 — and a *sector* container rather than a byte image. Each
disk is a header and a table of track pointers, and each track a run of sectors,
every one behind a 16-byte ID field::

    disk    00h name (16 chars + 00h)   1Ah write protect   1Bh media
            1Ch disk size, header included
            20h track table: 164 pointers (688-byte header), or 160 from
                older tools (672) — 0 for a track never formatted
    sector  00h C  01h H  02h R  03h N (128 << N bytes)
            04h sectors in this track   06h density   07h deleted   08h status
            09h reserved (5)            0Eh stored data length
            10h the data

A game on these machines usually has no filesystem and reads absolute sectors, so
the read strips every ID field and hands on the disk in the order a loader counts
it: tracks in table order, each track's sectors by R. On a regular disk that makes
``flat offset = (track × sectors per track + R − 1) × sector size``, and every
offset a project quotes is one the game's own load tables can be checked against
(``docs/rom-mapping/console-pc-8801.md``).

**The format is honoured loosely in the wild, and the read is built for that:**

* **Sectors are stored in the order they were read**, which need not be R order,
  so each track is sorted by R. The sort is stable, so the duplicate IDs of a
  copy-protected track keep their stored order.
* **The data length at 0Eh is often garbage**, and ``128 << N`` is the length the
  images observed in the wild are padded to — yet a sector may legitimately be
  stored with no data at all. Neither is trusted blindly: a length is accepted
  only if it lands on the next sector's header (or exactly on the next track),
  with ``128 << N`` tried first. A track where neither does is cut short there and
  reported, rather than walked on through its own data.
* **The header is 688 or 672 bytes**, told by the first non-zero track pointer,
  which is the only thing that can be: the table's length is not stored.
* **Every sector reads as its full ``128 << N`` bytes**, and a track never
  formatted inside the formatted range reads as zeroes the size of a typical
  track. Both keep a later sector at the offset the arithmetic above gives it.
  A single-sided image, which fills only every other table entry, is left
  unpadded: its gaps are the head it does not have, not a missing track.
* **Several disks may be concatenated in one file**, the only sign being a file
  longer than the first disk's size field. They are read one after another into
  a single flat image.

**Write puts every byte back into its own sector**, re-parsing the destination by
the same rule the read used (:func:`parse_image`), so ID fields, headers, the
stored bytes past a sector's ``128 << N`` and anything the read did not reach stay
untouched. A byte that lands on padding has nowhere in the file to go; unless it
is still zero the write says so. A destination that is not a D88 image — a new
file among them — gets the payload plainly, as the read's fallback reads one.
"""

from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass, field

from celpix.core.context import KEY_SOURCE_OFFSET, PipelineContext
from celpix.core.errors import Stage
from celpix.core.notices import inform, warn
from celpix.plugins.base import (
    ContainerField,
    PluginInfo,
    ReadSource,
    WriteTarget,
    format_size,
    plain_read,
    splice,
)

PROTECT_FIELD = 0x1A
MEDIA_FIELD = 0x1B
SIZE_FIELD = 0x1C
TRACK_TABLE = 0x20
# The two lengths a disk header has: 164 track pointers, or 160 from older tools.
HEADER_SIZES = (0x2B0, 0x2A0)
SECTOR_HEADER = 0x10
# N is a shift, and past 7 (16 KiB) it describes no floppy sector; such an ID
# field is damage, so the stored length is all there is to go on.
MAX_SIZE_CODE = 7
STATUS_OK = 0x00
DELETED_FLAG = 0x10

MEDIA_NAMES = {0x00: "2D", 0x10: "2DD", 0x20: "2HD", 0x30: "1D", 0x40: "1DD"}


@dataclass(frozen=True)
class Span:
    """One run of the flat image: a sector's data, or padding with no file bytes.

    ``size`` is what the run occupies in the flat image, ``stored`` how many of
    those bytes the file holds at ``data`` (the rest read as zero). Padding has
    ``data`` of -1 and nothing stored.
    """

    size: int
    data: int = -1
    stored: int = 0


@dataclass
class Disk:
    """One disk of an image, as parsed: what the container info reports."""

    start: int
    declared: int
    header: int
    name: str
    media: int
    protected: bool
    spans: list[Span] = field(default_factory=list)
    formatted_tracks: int = 0
    sectors: int = 0
    reordered_tracks: int = 0
    length_disagreements: int = 0
    padded_tracks: list[int] = field(default_factory=list)
    damaged_tracks: list[int] = field(default_factory=list)
    track_sizes: set[int] = field(default_factory=set)
    deleted: int = 0
    bad_status: int = 0

    @property
    def payload_size(self) -> int:
        return sum(span.size for span in self.spans)


@dataclass
class Image:
    """Every disk found in a file, and what the parse could not account for."""

    disks: list[Disk] = field(default_factory=list)
    # Bytes after the last disk that do not parse as another one.
    trailing: int = 0
    # How far the last disk's size field reaches past the end of the file.
    missing: int = 0

    @property
    def spans(self) -> list[Span]:
        return [span for disk in self.disks for span in disk.spans]

    def payload(self, raw: bytes) -> bytes:
        out = bytearray()
        for span in self.spans:
            held = raw[span.data : span.data + span.stored] if span.stored else b""
            out += held
            out += bytes(span.size - len(held))
        return bytes(out)

    def place(self, out: bytearray, offset: int, data: bytes) -> int:
        """Lay ``data`` over the payload at ``offset``; returns bytes that had no
        home — non-zero ones landing on padding, which the file does not have."""
        dropped = 0
        end = offset + len(data)
        pos = 0
        for span in self.spans:
            lo, hi = max(pos, offset), min(pos + span.size, end)
            if lo < hi:
                held = min(hi, pos + span.stored)
                if lo < held:
                    at = span.data + lo - pos
                    out[at : at + held - lo] = data[lo - offset : held - offset]
                tail = data[max(lo, held) - offset : hi - offset]
                dropped += len(tail) - tail.count(0)
            pos += span.size
            if pos >= end:
                break
        return dropped


def _word(raw: bytes, at: int) -> int:
    return struct.unpack_from("<H", raw, at)[0]


def _dword(raw: bytes, at: int) -> int:
    return struct.unpack_from("<I", raw, at)[0]


def header_size(raw: bytes, at: int = 0) -> int:
    """The header length of a disk starting at ``at``; 0 if none starts there.

    The first non-zero track pointer is the track right after the header, so it
    is the header's length — and it must be one of the two there are. Only the
    first 160 entries are looked at, those being in the table either way: on a
    672-byte header the last four would be the first sector's ID field.
    """
    shorter = min(HEADER_SIZES)
    if at + shorter > len(raw):
        return 0
    for entry in range((shorter - TRACK_TABLE) // 4):
        pointer = _dword(raw, at + TRACK_TABLE + 4 * entry)
        if pointer:
            return pointer if pointer in HEADER_SIZES else 0
    return 0


def _stride(
    raw: bytes, pos: int, lengths: list[int], count: int, last: bool, bound: int
) -> int | None:
    """The data length of the sector at ``pos``: the first of ``lengths`` that
    lands where the next thing starts; None when none of them does.

    Inside a track the next thing is a sector header naming the same sector
    count. After a track's last sector it is the next track, but a tool may leave
    a gap there, so an exact landing is preferred and a short one accepted.
    """
    if last:
        ends = [n for n in lengths if pos + SECTOR_HEADER + n <= bound]
        exact = [n for n in ends if pos + SECTOR_HEADER + n == bound]
        return (exact or ends or [None])[0]
    for n in lengths:
        nxt = pos + SECTOR_HEADER + n
        if nxt + SECTOR_HEADER <= bound and _word(raw, nxt + 4) == count:
            return n
    return None


def _parse_disk(raw: bytes, start: int, header: int) -> Disk:
    declared = _dword(raw, start + SIZE_FIELD)
    end = min(start + declared, len(raw))
    disk = Disk(
        start=start,
        declared=declared,
        header=header,
        name=raw[start : start + 16].split(b"\0", 1)[0].decode("cp932", "replace"),
        media=raw[start + MEDIA_FIELD],
        protected=raw[start + PROTECT_FIELD] != 0,
    )
    table = [
        _dword(raw, start + TRACK_TABLE + 4 * i)
        for i in range((header - TRACK_TABLE) // 4)
    ]
    # A pointer at or past the end is a tool marking the entry unused, and one
    # inside the header is damage; either way there is no track there.
    usable = {
        i: start + p
        for i, p in enumerate(table)
        if p >= header and start + p + SECTOR_HEADER <= end
    }
    starts = sorted(set(usable.values()))
    tracks: dict[int, list[Span]] = {}
    for index, pos in usable.items():
        at = starts.index(pos)
        bound = starts[at + 1] if at + 1 < len(starts) else end
        count = _word(raw, pos + 4)
        found: list[tuple[int, Span]] = []
        for i in range(count):
            if pos + SECTOR_HEADER > bound:
                break
            code, stored = raw[pos + 3], _word(raw, pos + 0x0E)
            size = 128 << code if code <= MAX_SIZE_CODE else stored
            lengths = list(dict.fromkeys((size, stored)))
            length = _stride(raw, pos, lengths, count, i == count - 1, bound)
            if length is None:
                break
            disk.length_disagreements += stored != size
            disk.deleted += raw[pos + 7] == DELETED_FLAG
            disk.bad_status += raw[pos + 8] != STATUS_OK
            data = pos + SECTOR_HEADER
            found.append((raw[pos + 2], Span(size, data, min(length, size))))
            pos = data + length
        if len(found) < count:
            disk.damaged_tracks.append(index)
        if [r for r, _ in found] != sorted(r for r, _ in found):
            disk.reordered_tracks += 1
        found.sort(key=lambda pair: pair[0])
        tracks[index] = [span for _, span in found]
        disk.sectors += len(found)
    disk.formatted_tracks = len(tracks)
    disk.track_sizes = {sum(s.size for s in spans) for spans in tracks.values()}

    # A single-sided image fills only every other table entry; its gaps are the
    # head it does not have, and padding them would push every track after the
    # first a whole blank track further along than a loader counts it.
    single_sided = len(tracks) > 1 and all(i % 2 == 0 for i in tracks)
    typical = Counter(
        sum(s.size for s in spans) for spans in tracks.values()
    ).most_common(1)
    for index in range(max(tracks, default=-1) + 1):
        if index in tracks:
            disk.spans += tracks[index]
        elif not single_sided and typical:
            disk.padded_tracks.append(index)
            disk.spans.append(Span(typical[0][0]))
    return disk


def parse_image(raw: bytes) -> Image:
    """Every disk in ``raw``, one after another; no disks if it is not a D88.

    Shared by both directions so they cannot disagree about which file bytes a
    payload byte came from — a drift there writes tiles over sector headers.
    """
    image = Image()
    at = 0
    while at < len(raw):
        header = header_size(raw, at)
        declared = _dword(raw, at + SIZE_FIELD) if header else 0
        if declared < header or not header:
            break
        image.disks.append(_parse_disk(raw, at, header))
        at += declared
    if image.disks:
        image.trailing = max(0, len(raw) - at)
        image.missing = max(0, at - len(raw))
    return image


class D88Container:
    """A ``.d88`` floppy image, unwrapped to its flat sector data and back.

    Detected on the extension alone: the format has no magic, and the header's
    only structural check — the first track pointer being one of two header
    lengths — is too weak to claim a file of any other name by.
    """

    info = PluginInfo(
        id="container.d88",
        name="D88 floppy disk image (sector data)",
        stage=Stage.CONTAINER,
        extensions=(".d88", ".d77", ".d68", ".d98", ".88d", ".98d"),
        short_name="D88",
        category="Generic",
        # Dropping a 16-byte ID field in front of every sector — and sorting and
        # padding tracks — moves every payload byte, so a position in the payload
        # names nothing in the file. The default, spelled out because it is what
        # makes a slice read through this payload instead of seeking into the
        # file, where it would land among sector headers.
        preserves_offsets=False,
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        raw = source.data
        image = parse_image(raw)
        if not image.disks:
            warn(
                ctx,
                "Not a D88 disk image: read as plain bytes",
                "No disk header was found: the first track pointer is\n"
                "neither 688 nor 672. Nothing was unwrapped; the whole\n"
                "file is shown as-is.",
                self.info.id,
            )
            return plain_read(source, ctx)
        self._report(image, ctx)
        payload = image.payload(raw)
        # The offset addresses the *payload*, so the window is cut from it rather
        # than from the file; returning the whole payload instead would hand every
        # slice the entire disk to decode into plausible garbage.
        ctx.set(KEY_SOURCE_OFFSET, source.offset)
        start = source.start
        end = len(payload) if source.length is None else start + source.length
        return payload[start:end]

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        image = parse_image(dest.existing)
        if not image.disks:
            return splice(dest.existing, dest.offset, data)
        out = bytearray(dest.existing)
        dropped = image.place(out, dest.offset, data)
        if dropped:
            warn(
                ctx,
                f"{dropped} edited byte(s) not saved: they have no sector",
                "They fall on a track that was never formatted or past\n"
                "the data a sector stores, which the image has no bytes\n"
                "for. Everything else was written to its own sector.",
                self.info.id,
            )
        return bytes(out)

    def _report(self, image: Image, ctx: PipelineContext) -> None:
        source = self.info.id
        if image.missing:
            warn(
                ctx,
                f"Disk image is cut short by {image.missing} bytes",
                "The last disk's size field reaches past the end of the\n"
                "file, so the sectors it would hold there are missing.",
                source,
            )
        if image.trailing:
            warn(
                ctx,
                f"Ignored {image.trailing} trailing byte(s) after the last disk",
                "They do not start another disk header, so they are not\n"
                "shown here, and a save leaves them as they are.",
                source,
            )
        damaged = sum(len(disk.damaged_tracks) for disk in image.disks)
        if damaged:
            warn(
                ctx,
                f"{damaged} track(s) end early: sector lengths do not add up",
                "Neither the stored length nor the size code of a sector\n"
                "leads to the next sector's header, so the rest of that\n"
                "track is not shown and every later offset moves up.",
                source,
            )
        padded = sum(len(disk.padded_tracks) for disk in image.disks)
        if padded:
            inform(
                ctx,
                f"{padded} unformatted track(s) read as zeroes",
                "Padded to the size of a typical track, so the tracks\n"
                "after each gap keep the offsets a loader counts.",
                source,
            )
        if any(len(disk.track_sizes) > 1 for disk in image.disks):
            inform(
                ctx,
                "Tracks differ in size",
                "Not every track holds the same number of bytes, so an\n"
                "absolute sector number does not convert to an offset\n"
                "by one multiplication past the first irregular track.",
                source,
            )
        bad = sum(disk.bad_status for disk in image.disks)
        if bad:
            inform(
                ctx,
                f"{bad} sector(s) were dumped with an FDC error",
                "Their status byte is not 00h - a CRC error, typically -\n"
                "so the bytes read for them may not be what is on the\n"
                "original disk.",
                source,
            )

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        image = parse_image(source.data)
        if not image.disks:
            return (
                ContainerField(
                    "Disk header",
                    "not a D88 image",
                    "The first track pointer is neither 688 nor 672, the\n"
                    "only two header lengths there are, so the file is\n"
                    "handed on whole as a plain binary would be.",
                ),
            )
        fields: list[ContainerField] = []
        if len(image.disks) > 1:
            fields.append(
                ContainerField(
                    "Disks",
                    f"{len(image.disks)} in one file",
                    "Concatenated one after another, each with its own\n"
                    "header. Read into one flat image in file order.",
                )
            )
        for number, disk in enumerate(image.disks, 1):
            label = f"Disk {number} " if len(image.disks) > 1 else ""
            fields += self._disk_fields(disk, label)
        return tuple(fields)

    @staticmethod
    def _disk_fields(disk: Disk, label: str) -> list[ContainerField]:
        quirks = [
            f"{disk.reordered_tracks} track(s) not in R order"
            if disk.reordered_tracks
            else "",
            f"{disk.length_disagreements} length field(s) disagree with N"
            if disk.length_disagreements
            else "",
            f"{len(disk.padded_tracks)} unformatted track(s) padded"
            if disk.padded_tracks
            else "",
            f"{len(disk.damaged_tracks)} track(s) cut short"
            if disk.damaged_tracks
            else "",
            f"{disk.deleted} deleted sector(s)" if disk.deleted else "",
            f"{disk.bad_status} FDC error(s)" if disk.bad_status else "",
        ]
        header_tracks = (disk.header - TRACK_TABLE) // 4
        return [
            ContainerField(
                f"{label}Name",
                disk.name or "(none)",
                "The 16-character label at the head of the disk. Written\n"
                "by the dumping tool rather than the game, so a raw game\n"
                "disk usually carries whatever the blank was called.",
            ),
            ContainerField(
                f"{label}Media",
                MEDIA_NAMES.get(disk.media, f"unknown ({disk.media:02X}h)")
                + (", write protected" if disk.protected else ""),
                "The media byte at 1Bh and the protect flag at 1Ah.\n"
                "Informational: the geometry is read from the sectors\n"
                "themselves, and a save writes regardless of the flag.",
            ),
            ContainerField(
                f"{label}Header",
                f"{disk.header} bytes, {header_tracks} track pointers",
                "Told by the first non-zero track pointer, since the\n"
                "table's length is not stored: 688 bytes is the current\n"
                "layout, 672 the one older tools wrote.",
            ),
            ContainerField(
                f"{label}Size",
                f"{disk.declared} ({format_size(disk.declared)})",
                "The size field at 1Ch, header included. A file longer\n"
                "than this holds another disk after it.",
            ),
            ContainerField(
                f"{label}Sectors",
                f"{disk.sectors} in {disk.formatted_tracks} tracks, "
                f"{format_size(disk.payload_size)} flat",
                "Every sector's data with its ID field removed, each\n"
                "track sorted by R and every sector at its full size -\n"
                "what a slice offset in this entry addresses.",
            ),
            ContainerField(
                f"{label}Irregularities",
                "; ".join(q for q in quirks if q) or "none",
                "What the read had to work around: out-of-order sectors,\n"
                "garbage length fields, gaps in the track table, and\n"
                "sectors dumped deleted or with a read error.",
            ),
        ]
