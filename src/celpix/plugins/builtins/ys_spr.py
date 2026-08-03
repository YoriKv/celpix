"""The Yoshi's Island sprite-pattern container — 32 counted frames of subsprites.

A sprite map like the S-CG-CAD sprite object beside it (:mod:`.scgcad`), from a
different tool in the same devkit, and the one file in either family that is
**not a fixed-size grid of slots**. Its layout is a pure forward scan:

===================  ==========================================================
32 x                 one count byte, then that many 8-byte subsprite records
40 + 40 + 1 bytes    the animation blocks and a flag byte - preserved, not read
to end of file       a tool signature, or nothing at all
===================  ==========================================================

There is no header, no length field and no magic at a fixed offset, so the file's
size is determined entirely by its 32 count bytes and its extent is knowable only
by walking them. Byte-exact spec, the corpus it was checked against and what each
claim rests on are in ``docs/graphics-formats-reference/ys-sprite-patterns.md``.

Three things a reader has to get right and would not guess:

- **The count bytes are structure, not payload.** They sit *between* the records
  rather than before them, so a buffer that keeps them has no fixed cell stride
  and every byte offset downstream is wrong by however many frames have started.
  This container takes them out and states them
  (:data:`~celpix.core.context.KEY_TILEMAP_FRAME_SIZES`), which is what lets the
  codec keep a truthful 8 bytes per cell; :meth:`SprContainer.write` puts them
  back from the counts the read published.
- **There is no drawn bit.** A record that is present is drawn, and the count is
  the only thing saying how many there are. That is why the frames cannot be
  normalised to fixed slots on the way in: an absent subsprite has no spelling in
  this format, so padding one out would mean inventing a field.
- **The trailer is not always data.** Files from the earliest build carry 512
  bytes of uninitialised buffer where the later ones carry 81 and a signature —
  stale records from whatever file the tool had open before. Either way it is
  read as an opaque tail and written back exactly as found, so nothing has to
  decide which it is.

Detection has only the extension. The signature sits at the *end* of a
variable-length file, which ``PluginInfo.magic`` cannot express (it probes fixed
offsets, and detection is inert data by design), the sizes span an order of
magnitude so ``exact_size`` cannot narrow, and a third of the corpus carries no
signature at all. So a ``.spr`` from some other tool will land here; the scan
below is written to survive that rather than to reject it.
"""

from __future__ import annotations

from celpix.core.animation import read_parallel_sequences
from celpix.core.context import (
    KEY_SOURCE_OFFSET,
    KEY_TILEMAP_ANIMATIONS,
    KEY_TILEMAP_ANIMATIONS_INFERRED,
    KEY_TILEMAP_COLUMNS,
    KEY_TILEMAP_FRAME_SIZES,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import (
    ContainerField,
    PluginInfo,
    ReadSource,
    WriteTarget,
    format_size,
)
from celpix.plugins.builtins.object_codec import SPR_RECORD

FRAMES = 32  # every file has exactly this many, empty ones included
TRAILER = 81  # 40 bytes of frame numbers, 40 of durations, one flag byte
ANIMATION_STEPS = 40  # one of those blocks; the trailer holds one sequence
FRAMES_ACROSS = 8  # how many frames the sheet opens laid out at, as for an object


def _scan(data: bytes) -> tuple[tuple[int, ...], bytes, int]:
    """``(frame sizes, the records joined up, where the trailer starts)``.

    The whole of reading this format: walk the 32 counts, taking that many records
    after each. Everything else here is preserving what the walk did not reach.

    **A short or foreign file stops the walk rather than failing it.** Detection
    is extension-only (see the module docstring), so this runs on bytes that may
    not be a sprite pattern at all, and the honest result for those is the frames
    that did parse — an entry that opens showing something wrong is diagnosable
    where a refusal is not.
    """
    sizes: list[int] = []
    records = bytearray()
    at = 0
    for _ in range(FRAMES):
        if at >= len(data):
            break
        count = data[at]
        at += 1
        span = data[at : at + count * SPR_RECORD]
        # A count running past the end takes the **whole** records that are there
        # and stops the walk: the frames after it have no counts left to read, and
        # a trailing part-record is not one the codec could decode or the write
        # put back.
        whole = len(span) // SPR_RECORD * SPR_RECORD
        records += span[:whole]
        sizes.append(whole // SPR_RECORD)
        at += count * SPR_RECORD
        if whole < count * SPR_RECORD:
            break
    return tuple(sizes), bytes(records), at


def _signature(trailer: bytes) -> str:
    """The tool's name out of the tail, or ``""`` when the tail is not one.

    A signature is NUL-less ASCII running to the end of the file, so a tail
    holding anything else is the earliest build's uninitialised buffer instead of
    a name. Testing that rather than decoding whatever is there matters because
    the buffer is largely NULs, and those survive a ``strip()`` into a string
    that is non-empty, unprintable, and drawn as a blank field.
    """
    raw = trailer[TRAILER:].rstrip(b"\x00")
    if not raw or not all(0x20 <= byte <= 0x7E for byte in raw):
        return ""
    return raw.decode("ascii").strip()


class SprContainer:
    """Sprite pattern: counted frames of 8-byte records, then an opaque tail.

    The read hands on the records alone and states where the frames were cut; the
    write interleaves the count bytes back in and restores the tail.

    ``preserves_offsets`` is **False** because the payload is not a window into
    the file: dropping the counts moves every record down by one byte per frame
    that has started, so a position in the buffer names a different position in
    the file. Declaring it keeps a slice or an offset palette from resolving
    against coordinates this container does not preserve.
    """

    info = PluginInfo(
        id="container.ys-spr",
        name="Yoshi's Island sprite pattern (SPR)",
        stage=Stage.CONTAINER,
        extensions=(".spr",),
        short_name="SPR",
        preserves_offsets=False,
    )
    default_tilemap_preset = "preset.tilemap.ys-spr"

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        sizes, records, trailer_at = _scan(source.data)
        ctx.set(KEY_TILEMAP_COLUMNS, FRAMES_ACROSS)
        ctx.set(KEY_TILEMAP_FRAME_SIZES, sizes)
        # The one animation table in hand that is a *reading* rather than a spec,
        # and it says so: the two blocks are opaque byte arrays to the writer, so
        # which is frames and which durations comes off the corpus.
        ctx.set(
            KEY_TILEMAP_ANIMATIONS,
            read_parallel_sequences(source.data, trailer_at, ANIMATION_STEPS),
        )
        ctx.set(KEY_TILEMAP_ANIMATIONS_INFERRED, True)
        # Zero because the records *begin* at 1 and then drift: no single offset
        # describes where this payload came from, so the addresses beside the hex
        # dump are stated as positions in the record stream it is showing rather
        # than as file offsets they cannot all be.
        ctx.set(KEY_SOURCE_OFFSET, 0)
        return records

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        """The records re-interleaved with their counts, and the tail put back.

        The frame boundaries come from the **context** the read published, not
        from the encoded bytes: a count byte is not in them, and a flat run of
        records says nothing about where one frame ends. Falling back to the
        destination's own counts covers a Save As onto an existing pattern file;
        with neither, everything lands in one frame, which is the only reading
        left and is what a file written from nothing would have to be.

        Records past what the counts describe join the last frame rather than
        being dropped, so a save can never quietly lose one.
        """
        sizes = ctx.get(KEY_TILEMAP_FRAME_SIZES)
        tail = b""
        if dest.existing:
            existing_sizes, _, trailer_at = _scan(dest.existing)
            tail = dest.existing[trailer_at:]
            if not sizes:
                sizes = existing_sizes
        out = bytearray()
        available = len(data) // SPR_RECORD
        at = 0
        for index in range(FRAMES):
            size = sizes[index] if sizes and index < len(sizes) else 0
            # The last frame with anything left over takes the remainder, so a
            # longer buffer than the counts account for still round-trips.
            if index == FRAMES - 1:
                size = max(size, available - at)
            size = max(0, min(size, available - at, 0xFF))
            out.append(size)
            out += data[at * SPR_RECORD : (at + size) * SPR_RECORD]
            at += size
        return bytes(out) + (tail or bytes(TRAILER))

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        sizes, records, trailer_at = _scan(source.data)
        trailer = source.data[trailer_at:]
        signature = _signature(trailer)
        drawn = sum(1 for size in sizes if size)
        return (
            ContainerField(
                "Signature",
                f"{signature} at {trailer_at + TRAILER:#06x}"
                if signature
                else "none - identified by suffix alone",
                "This family puts its signature at the *end*, past a\n"
                "payload whose length varies, so detection cannot match\n"
                "on it and has only the .spr suffix to go on. The\n"
                "earliest build wrote none at all.",
            ),
            ContainerField(
                "Frames",
                f"{drawn} of {FRAMES} used, {len(records) // SPR_RECORD} subsprites",
                "Each frame states its own subsprite count, so the\n"
                "frames are different lengths and the file's size is\n"
                "whatever those counts add up to. The counts are held\n"
                "aside and put back on write.",
            ),
            ContainerField(
                "Trailer",
                f"{format_size(len(trailer))} at {trailer_at:#06x}, preserved",
                "40 frame numbers, then 40 durations, then a flag byte.\n"
                "That split is read off the corpus rather than off the\n"
                "writer, which emits both blocks opaquely - so the player\n"
                "says it is a reading. The earliest build leaves 512 bytes\n"
                "of uninitialised buffer here instead; both ride through\n"
                "a save intact.",
            ),
        )
