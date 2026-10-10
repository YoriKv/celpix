"""The S-CG-CAD sprite members: the object (OBJ/OBX) and its transfer form (OBZ).

Both hand frames of subsprite records on to a sprite-object codec
(:mod:`~celpix.plugins.builtins.object_codec`) and publish the animation table
that follows them for playback, while a save writes that table back untouched
(``scgcad-formats.md`` §8, §9).
"""

from __future__ import annotations

from celpix.core.address import format_hex
from celpix.core.animation import read_sequences
from celpix.core.context import (
    KEY_TILEMAP_ANIMATIONS,
    KEY_TILEMAP_COLUMNS,
    KEY_TILEMAP_ENDIAN,
    KEY_TILEMAP_SUBSPRITES_PER_FRAME,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import (
    ContainerField,
    PluginInfo,
    ReadSource,
    WriteTarget,
    format_size,
    splice,
)

from .._container_fields import fixed_text
from ..object_codec import RECORD, SUBSPRITES_PER_FRAME
from ._common import (
    HEADER,
    SIGNATURE,
    _blank,
    _live_sequences,
    _metadata_fields,
    _payload,
)

# A sprite object's records, then its header: 32 frames of 64 six-byte subsprites
# in the ordinary form and **64 frames of 128** in the extended one. Which it is
# shows in where the signature sits, so these double as the detection offsets.
OBJ_PAYLOADS = (0x3000, 0xC000)
# How each form slots that payload. Not derivable from the size — 0xC000 is 64x128
# and 128x64 alike — so it is stated, and the pair is what the container publishes
# for the codec to frame by (``KEY_TILEMAP_SUBSPRITES_PER_FRAME``). The extended
# form's build converter declares `obj_data[64][128][6]` against the ordinary one's
# `[32][64][6]`, and the corpus agrees: over 301 extended objects the drawn records
# peak at slot 127 and fall away monotonically, with slot 63 at 6% of that — one
# period of 128, not two of 64 (``scgcad-formats.md`` §8.1).
OBJ_SUBSPRITES_PER_FRAME = {0x3000: SUBSPRITES_PER_FRAME, 0xC000: 128}
# One subsprite record, the codec's own. Named here because the frame division is
# reported as well as read, and a hard-coded "64 records" divisor is what let the
# info panel go on describing an extended object as 128 frames of 64 after the
# codec stopped.
SUBSPRITE_RECORD = RECORD
# Each form's whole length, keyed by its payload: 0x100 of header and then the
# animation table, which is twice the size in the extended form for the twice the
# frames it names. The write side needs the pair, an object saved to a
# path with no file at it having to be *built* at the form its records are in —
# the ordinary one holds a quarter of an extended object's frames, so writing one
# into the other is not a shorter file but three quarters of the art gone.
OBJ_SIZES = {0x3000: 0x3500, 0xC000: 0xC900}
OBJ_SIZE = OBJ_SIZES[OBJ_PAYLOADS[0]]  # the ordinary form
# How many animation sequences each form holds, and how many steps each of those
# has room for. One group per 64 bytes of table, which is what the sizes above
# already say — stated rather than divided out, because the step count is the same
# 32 in both forms and only the group count grows with the frames
# (``scgcad-formats.md`` §8.3).
OBJ_SEQUENCES = {0x3000: 16, 0xC000: 32}
OBJ_SEQUENCE_STEPS = 32

# The transfer form of a sprite object: 64 frames of 64 subsprites, then a 0xA00 tail
# holding the animation table. The one member of the family that carries **no
# signature at all** — none of the 148 in the corpus has one anywhere — so its
# length and its extension are the whole of what identifies it.
OBZ_SIZE = 0x6A00
OBZ_PAYLOAD = 0x6000
# Its animation table is the object's read at a different shape: 16 sequences of
# 64 steps in the 0x800 that follows the records, then a 0x200 tail nothing has
# decoded (``scgcad-formats.md`` §9).
OBZ_SEQUENCES = 16
OBZ_SEQUENCE_STEPS = 64
OBZ_TABLE = (OBZ_SEQUENCES, OBZ_SEQUENCE_STEPS)  # the pair both readers take
# How many frames the strip puts on a row to start with. A sprite object has no
# width of its own — its frames are separate pictures rather than one picture —
# so this is a legible default rather than the file's own answer.
OBJECT_COLUMNS = 8

# bytes: the object this pathway's records were read out of. A save to a path
# with no file at it splices into a copy of it rather than into a blank object:
# the blank's empty build marker reads as big-endian, while the codec encodes by
# the byte order the read published, so an `F`-build object's attribute words
# would reopen swapped - and its header and animation table would be lost.
KEY_OBJ_SOURCE = "scgcad-obj.source"


class ObjContainer:
    """Sprite object: subsprite records first, then the header and animation table.

    Payload-first like a screen, and the header's *offset* is what says which of
    the two sizes this is — 0x3000 for an object, 0xC000 for the extended form,
    which holds twice the frames at twice the subsprites each. Detection matches
    the signature at either.

    What follows the header is the animation table: runs of (duration, frame)
    naming frames in the payload. It is **read on the way in and preserved
    opaquely on the way out** — the sequences are published for the animation
    player (:data:`~celpix.core.context.KEY_TILEMAP_ANIMATIONS`), while a save
    writes the original bytes back untouched. Reading is therefore free to be
    wrong in a way writing is not: nothing downstream can make a save depend on
    it. The view itself draws the frames in file order regardless.

    The one thing here that is a *reading* rather than a cut: the build marker
    decides the attribute word's byte order, so it is published for the codec
    (:data:`~celpix.core.context.KEY_TILEMAP_ENDIAN`). 26 of the 1,341 objects in
    the corpus are the later build and every one of them says so.
    """

    info = PluginInfo(
        id="container.scgcad-obj",
        name="S-CG-CAD sprite object (OBJ/OBX)",
        stage=Stage.CONTAINER,
        extensions=(".obj", ".obx"),
        magic=tuple((at, SIGNATURE) for at in OBJ_PAYLOADS),
        short_name="OBJ",
        category="S-CG-CAD",
        preserves_offsets=True,
    )
    default_tilemap_preset = "format.tilemap.scgcad-object"

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        payload = _obj_payload(source.data)
        ctx.set(KEY_TILEMAP_COLUMNS, OBJECT_COLUMNS)
        # The two forms hold the same payload and divide it differently — 32
        # frames of 64 slots against 64 of 128 — so the stride is not derivable
        # from the size and has to come from *which form this is*, which is the
        # question already answered above. The extended object's own converter
        # declares `obj_data[64][128][6]` where the ordinary one declares
        # `[32][64][6]` (``scgcad-formats.md`` §8.1).
        ctx.set(KEY_TILEMAP_SUBSPRITES_PER_FRAME, OBJ_SUBSPRITES_PER_FRAME[payload])
        swapped = _obj_swapped(_obj_marker(source.data, payload))
        ctx.set(KEY_TILEMAP_ENDIAN, "little" if swapped else "big")
        ctx.set(KEY_OBJ_SOURCE, source.data)
        # The animation table, from the tail this container is about to cut away.
        # Read here rather than by the codec because the codec is handed the
        # payload alone and the table is past it — and read at all only because a
        # reader wants it; the write side still preserves these bytes opaquely,
        # so nothing downstream can make a save depend on this being right.
        ctx.set(
            KEY_TILEMAP_ANIMATIONS,
            read_sequences(
                source.data,
                payload + HEADER,
                OBJ_SEQUENCES[payload],
                OBJ_SEQUENCE_STEPS,
            ),
        )
        return _payload(source, ctx, 0, payload)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        # The form comes from the records being saved, not from the file already
        # at the path: splicing 64 frames into an ordinary object drops 32 of
        # them, and a path with no file at it has no form to offer at all. Where
        # the two agree the whole tail is preserved, animation table included —
        # and that covers the `F` build, whose extra 0x20 bytes ride along
        # untouched because the payload is where they are measured from. A path
        # with no file at it borrows the object the records were read from
        # (:data:`KEY_OBJ_SOURCE`) before falling back to a blank.
        existing = dest.existing or _stashed(ctx) or _blank_obj()
        payload = _obj_payload(existing)
        if payload == len(data):
            return splice(existing, 0, data)
        if len(data) in OBJ_SIZES:
            return splice(_blank_obj(len(data)), 0, data)
        # Records that are neither form's length: the destination decides, as it
        # did before either form was named here.
        return splice(existing, 0, data[:payload])

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        data = source.data
        payload = _obj_payload(data)
        marker = _obj_marker(data, payload)
        swapped = _obj_swapped(marker)
        extended = payload == OBJ_PAYLOADS[1]
        groups = OBJ_SEQUENCES[payload]
        slots = OBJ_SUBSPRITES_PER_FRAME[payload]
        return (
            *_metadata_fields(data, payload),
            ContainerField(
                "Form",
                f"{'extended' if extended else 'ordinary'} - "
                f"{payload // (slots * SUBSPRITE_RECORD)} frames"
                f" of {slots} subsprites",
                "Told by where the signature sits; the records stop there\n"
                "Not by file length, so a file with an extra tail reads",
            ),
            ContainerField(
                "Build marker",
                f"{marker or 'blank'} - attribute word "
                f"{'byte-swapped' if swapped else 'as stored'}",
                "A later build byte-swaps the attribute word and says so\n"
                "Published as the cell byte order, overriding the codec",
            ),
            ContainerField(
                "Animation table",
                f"{format_size(groups * OBJ_SEQUENCE_STEPS * 2)}"
                f" at {format_hex(payload + HEADER, 4)}, "
                f"{_live_sequences(data, payload + HEADER, groups, OBJ_SEQUENCE_STEPS)}"
                f" of {groups} sequences used",
                "Runs of (duration, frame) naming frames in the payload\n"
                "Read for playback only; frames draw in file order\n"
                "Written back exactly as read",
            ),
        )


class ObzContainer:
    """Transfer object: 0x6000 of subsprite records, then a 0xA00 tail.

    The same shape as a sprite object with the header taken away — the tool wrote
    these to ship a whole set of frames to the devkit rather than to reopen them,
    and none of the 148 in the corpus carries the family signature. So detection
    has only the extension and the length, and a write has only the tail to
    preserve — which holds the animation table, read here exactly as an object's
    is and at the same confidence (``scgcad-formats.md`` §9).

    The records inside are **not** an object's records. Its own codec says how
    (:class:`~celpix.plugins.builtins.object_codec.ObzCodec`).
    """

    info = PluginInfo(
        id="container.scgcad-obz",
        name="S-CG-CAD transfer object (OBZ)",
        stage=Stage.CONTAINER,
        extensions=(".obz",),
        exact_size=OBZ_SIZE,
        short_name="OBZ",
        category="S-CG-CAD",
        preserves_offsets=True,
    )
    default_tilemap_preset = "format.tilemap.scgcad-obz"

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        ctx.set(KEY_TILEMAP_COLUMNS, OBJECT_COLUMNS)
        # The table sits where the records stop, with no header between them —
        # the one difference from an object, whose 0x100 metadata block comes
        # first. Same steps, same terminator, wider groups.
        ctx.set(
            KEY_TILEMAP_ANIMATIONS,
            read_sequences(source.data, OBZ_PAYLOAD, OBZ_SEQUENCES, OBZ_SEQUENCE_STEPS),
        )
        return _payload(source, ctx, 0, OBZ_PAYLOAD)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        existing = dest.existing or bytes(OBZ_SIZE)
        return splice(existing, 0, data[:OBZ_PAYLOAD])

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        return (
            ContainerField(
                "Signature",
                "none - identified by length and suffix",
                "The one member of this family with no signature:\n"
                "written to be consumed by the devkit, not reopened\n"
                "Length and suffix are all there is to go on",
            ),
            ContainerField(
                "Payload",
                f"{format_size(OBZ_PAYLOAD)} at 0x000000 - "
                f"{OBZ_PAYLOAD // (SUBSPRITES_PER_FRAME * SUBSPRITE_RECORD)} frames"
                f" of {SUBSPRITES_PER_FRAME} subsprites",
                "A sprite object's shape with the header removed\n"
                "The records are not an object's records;\n"
                "they read through their own cell codec",
            ),
            ContainerField(
                "Animation table",
                f"{format_size(OBZ_SIZE - OBZ_PAYLOAD)} at "
                f"{format_hex(OBZ_PAYLOAD, 4)}, "
                f"{_live_sequences(source.data, OBZ_PAYLOAD, *OBZ_TABLE)}"
                f" of {OBZ_SEQUENCES} sequences used",
                f"{OBZ_SEQUENCES} sequences of {OBZ_SEQUENCE_STEPS}"
                " (duration, frame), read for playback\n"
                "The whole tail is preserved on save, timings included",
            ),
        )


def _stashed(ctx: PipelineContext) -> bytes:
    """The object the read stashed, or ``b""`` when there is none to borrow."""
    source = ctx.get(KEY_OBJ_SOURCE)
    return bytes(source) if isinstance(source, (bytes, bytearray)) else b""


def _obj_payload(data: bytes) -> int:
    """Where this object's records stop — which is where its signature starts.

    By signature position rather than by file length, so the two sizes are told
    apart by the same thing detection used and a file with an unexpected tail
    still reads.
    """
    for at in OBJ_PAYLOADS:
        if data[at : at + len(SIGNATURE)] == SIGNATURE:
            return at
    return OBJ_PAYLOADS[0]


def _obj_marker(data: bytes, payload: int) -> str:
    """The OBJ header's build marker, as text: cut at its first NUL, stripped."""
    return fixed_text(data[payload + 0x10 : payload + 0x20])


def _obj_swapped(marker: str) -> bool:
    """Whether the build ``marker`` names the build that byte-swaps the attribute word.

    That build ends its marker in ``F``. Keyed off the marker rather than the file
    size, which is the other signal and the indirect one: an ``F`` object happens
    to be 0x20 bytes longer, but that is its extra per-sequence positions, not the
    thing being asked about. One test for the read and the info popup both, read
    through :func:`_obj_marker`, so a marker padded with NULs rather than spaces
    cannot be decoded one way and described the other.
    """
    return marker.endswith("F")


def _blank_obj(payload: int = OBJ_PAYLOADS[0]) -> bytes:
    """An empty object of the form ``payload`` bytes of records names.

    The signature's *position* is the whole of what says which form a file is, so
    a blank has to be the right length with it in the right place; there is
    nowhere else in the header the choice is written down. The build marker is
    left blank, which reads as the common build and its big-endian attribute word
    — the only one celPix could claim to have written.
    """
    return _blank(OBJ_SIZES[payload], payload)
