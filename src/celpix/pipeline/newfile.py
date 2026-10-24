"""Creating and resizing a file: a blank payload, framed and sized in units.

Owns everything that answers "how big, in tiles, cells or colors" for a region
the user is sizing rather than reading — the arithmetic between those units and
bytes (:func:`blank_size`, :func:`blank_units`, :func:`region_units`), a new
file's bytes with its
stages' framing on (:func:`blank_file_bytes`, :func:`create_file`), and a
resize of an existing region, written in place (:func:`resize_file`) or packed
back into a compressed slice's slot (:func:`resized_slot_bytes`)
(``docs/design/new-file.md``). Every result is read back through the same
stages before it is handed over, so a container whose format fixes a length is
refused rather than trusted (:func:`_check_payload_held`).

**A new file and a resize are one operation at two sizes.** Both take the
:class:`~celpix.pipeline.pathway.PathwayConfig` the file is read through — its
container, reshape and compression, the same three a load uses — grow or cut a
payload to a count of units, and run it out through the save's write half; a
new file is the case where the payload starts empty and the destination is
treated as empty too. So a file created compressed is packed by the very stage
that will unpack it, and reopens as the blank sheet that was asked for.

A resize runs the load's read half and the save's write half, which live in
:mod:`celpix.pipeline.pipeline`. That module re-exports this one's public names,
so it imports this module first; the few functions here that need its byte
stages import them where they run, which keeps either module importable first.
"""

from __future__ import annotations

from dataclasses import replace

from celpix.core import fit_length
from celpix.core.capabilities import ContentKind
from celpix.core.context import (
    KEY_PALETTE_PRESET,
    KEY_PIXEL_PRESET,
    PipelineContext,
)
from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.pipeline._stage import run_stage
from celpix.pipeline.metrics import (
    palette_entry_capacity,
    palette_read_bytes,
    pixel_tile_bytes,
    resized_cells,
    tilemap_cell_bytes,
    tilemap_resizes_itself,
)
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import ContainerPlugin, TilemapCodecPlugin, WriteTarget
from celpix.plugins.registry import Registry


def _new_file_pathway(kind: ContentKind) -> Pathway:
    """The pathway a new file's failures are reported under."""
    if kind is ContentKind.PALETTE:
        return Pathway.PALETTE
    return Pathway.TILEMAP if kind is ContentKind.TILEMAP else Pathway.PIXEL


def blank_size(kind: ContentKind, codec_id: str, units: int, reg: Registry) -> int:
    """Bytes a blank file of ``units`` tiles, cells or colors needs.

    The one arithmetic behind creating a file (``docs/design/new-file.md``): a
    new file is stated in the units the user thinks in — tiles for a graphic,
    cells for a map, colors for a palette — and only the codec knows what one of
    them costs. Asked of the codec rather than of a document, because there is no
    document until this answer has produced a file to open.

    A palette rounds up to a whole read unit rather than multiplying, since a
    packed format holds several colors in one
    (:func:`~celpix.pipeline.metrics.palette_read_bytes`). A tilemap is cells
    back to back for nearly every format, so a multiplication; a codec with its
    own word on its runs (:func:`~celpix.pipeline.metrics.resized_cells`) — a
    counted sprite format, a frame header around the pieces — is asked for the
    blank run instead and measured, since only it knows what that costs.
    """
    if kind is ContentKind.PALETTE:
        return palette_read_bytes(units, codec_id, reg)
    if kind is ContentKind.TILEMAP:
        if tilemap_resizes_itself(codec_id, reg):
            return len(resized_cells(b"", units, codec_id, reg))
        return units * tilemap_cell_bytes(codec_id, reg)
    return units * pixel_tile_bytes(codec_id, reg)


def _resized_payload(
    kind: ContentKind, codec_id: str, current: bytes, units: int, reg: Registry
) -> bytes:
    """``current`` grown with zeroes or cut at the tail to hold ``units``, the
    way the codec stores them.

    **Growth is zero bytes** and a shrink is a plain truncation of the tail:
    zero is what an empty region reads as for every codec celPix carries, and
    the end of the region is the only place units can be added or removed
    without moving the ones already there. A tilemap codec may have its own
    word on both (:func:`~celpix.pipeline.metrics.resized_cells`) — a counted
    sprite format's run is frames under headers, where a zero byte is a frame
    of no pieces rather than an empty cell.
    """
    if kind is ContentKind.TILEMAP:
        return resized_cells(current, units, codec_id, reg)
    return fit_length(current, blank_size(kind, codec_id, units, reg))


def blank_file_bytes(
    cfg: PathwayConfig,
    *,
    kind: ContentKind,
    codec_id: str,
    units: int,
    reg: Registry,
) -> bytes:
    """A whole new file's bytes: a blank payload, packed and framed by ``cfg``'s
    stages.

    The payload is **zero bytes** — :func:`blank_size` of them. Zero is what an
    empty region reads as for every codec celPix carries: index 0 in a tile (the
    transparent entry by convention), cell 0 in a map, black in a palette. So it
    is produced by arithmetic rather than by encoding a list of blank tiles,
    which would need a decoded shape for a codec whose geometry the preset alone
    does not fix. The one exception is a tilemap codec with its own word on
    what a run of so many cells is (:func:`_resized_payload`): a counted sprite
    format's blank run is a frame header stating the count and the zero pieces
    under it, because zero bytes there would be a frame of no pieces.

    It then goes out through the save's write half — compressed and un-reshaped
    by the config's stages (:func:`_repack`), then framed by its container —
    which is what makes this more than ``bytes(n)``: a file asked for as a
    packed stream *is* that stream, and the tile-bank and screen formats build
    a header and a clear table around a payload that is not theirs to invent,
    and hand back a file their own reader recognises.

    **The destination is treated as empty.** A container's write is normally
    shown what is already there so it can keep what it did not decode; a new file
    has nothing to keep, and showing it a file about to be replaced would splice
    a blank payload into bytes the user asked to be rid of. So a container that
    can only *preserve* its framing returns the bare payload here, and the result
    being exactly the packed payload is how a caller can tell
    (:func:`frames_new_file`) — behaviourally, without any container declaring it.

    The context carries the codec the payload is in (:func:`_seed_codec`), for a
    container whose header has to *state* it and that has no file to copy it
    from.

    **The file is read back before it is handed over.** A format fixes what a
    payload may be — a screen is 0x2000 bytes, a copier image is whole 16 KiB
    blocks — and a container handed a length it does not have quietly makes the
    file it can: the screen padded or cut to its size, the copier image with the
    partial block dropped. Neither is the file that was asked for, so the result
    is read through the same stages and refused when it does not hold
    ``blank_size`` bytes of payload (:func:`_check_payload_held`). ``cfg``'s
    source is never opened: the read-back is of the bytes just built.
    """
    return _blank_file(cfg, kind, codec_id, units, reg)[0]


def _blank_file(
    cfg: PathwayConfig, kind: ContentKind, codec_id: str, units: int, reg: Registry
) -> tuple[bytes, bytes, bytes]:
    """:func:`blank_file_bytes`, with the payload it holds and that payload as
    the stages packed it — ``(file, payload, packed)`` — for the callers that
    need to measure what was built rather than build it again to find out.
    """
    pathway = _new_file_pathway(kind)
    payload = _resized_payload(kind, codec_id, b"", units, reg)
    ctx = PipelineContext()
    _seed_codec(ctx, kind, codec_id)
    shaped = _repack(_from_nothing(cfg), payload, ctx, reg, pathway)
    container = reg.plugin(Stage.CONTAINER, cfg.container_id, ContainerPlugin)

    def build() -> bytes:
        data = container.write(shaped, WriteTarget(b""), ctx)
        held = _payload_held(cfg, data, reg, pathway)
        _check_payload_held(container, kind, codec_id, held, payload, reg, "would hold")
        return data

    data = run_stage(Stage.CONTAINER, pathway, build, "write", plugin=cfg.container_id)
    return data, payload, shaped


def _from_nothing(cfg: PathwayConfig) -> PathwayConfig:
    """``cfg`` reading an empty buffer: a new file as the write half sees it.

    The save's write half reads around the region it packs — the bytes before a
    stream a scheme packs against — from the buffer the read was cut from, and
    for a new file that is nothing: the file at the path, if there is one, is
    the one being replaced, and reading it would be both wrong and, for a ROM
    picked by mistake, megabytes per dialog refresh.
    """
    return replace(cfg, source=replace(cfg.source, data=b"", data_base=0))


def _payload_held(
    cfg: PathwayConfig, data: bytes, reg: Registry, pathway: Pathway
) -> bytes:
    """The payload ``data`` — a file as a container just wrote it — holds once
    read back through every stage ``cfg`` reads through.

    The one check a new file and a resize both make before trusting a write:
    the bytes are read as the entry will read them, while they are still only
    bytes, so a container that kept the file at a length of its own is caught
    before anything reaches disk.
    """
    from celpix.pipeline.pipeline import (  # noqa: PLC0415 — circular by nature
        _read_reshape_decompress,
    )

    preview = replace(cfg, source=replace(cfg.source, data=data, data_base=0))
    return _read_reshape_decompress(preview, PipelineContext(), reg, pathway)


# The unit each kind is counted in, for a refusal that has to say how many of
# them a container would have framed: the byte count alone is the codec's
# arithmetic, and the user typed tiles.
_UNIT_NOUNS = {
    ContentKind.PIXELS: "tiles",
    ContentKind.TILEMAP: "cells",
    ContentKind.PALETTE: "colors",
}


def _seed_codec(ctx: PipelineContext, kind: ContentKind, codec_id: str) -> None:
    """Tell a container writing into nothing what the payload is encoded in.

    The same two keys a container *publishes* when its format states the codec
    (``KEY_PIXEL_PRESET``, ``KEY_PALETTE_PRESET``), set from the other side: a
    container that has to write that statement into a header it is building
    fresh — a TPL palette's format byte — has nowhere else to learn it, a blank
    payload saying nothing about its own encoding. Advisory in this direction
    too; a container whose header states no codec ignores it.
    """
    if kind is ContentKind.PALETTE:
        ctx.set(KEY_PALETTE_PRESET, codec_id)
    elif kind is ContentKind.PIXELS:
        ctx.set(KEY_PIXEL_PRESET, codec_id)


def _check_payload_held(
    container: ContainerPlugin | None,
    kind: ContentKind,
    codec_id: str,
    held: bytes,
    wanted: bytes,
    reg: Registry,
    verb: str,
    *,
    subject: str | None = None,
    why: str = "; a file of this format cannot be that size",
) -> None:
    """Refuse a write whose result reads back as the payload ``held``, not one
    the length of ``wanted``.

    A container's write is a byte transform that keeps the file well-formed for
    its format, and for a format with fixed lengths that means a payload of the
    wrong length comes out cut, padded or partly dropped rather than as an
    error — the right behaviour for a save, where the payload is whatever the
    read produced, and silently the wrong file for a size the user just typed.
    Reading the result back is the one check that holds for every container,
    including a third-party one, without any of them declaring what sizes they
    have.

    ``subject`` names what fell short when it is not the container itself — a
    re-packed slot, whose length the codec decides rather than a format — and
    ``why`` is the clause that says why that matters for it.

    Handed the bytes rather than their lengths so the unit counts in the
    message are counted from them (:func:`region_units`): a counted sprite
    run's length is headers as well as pieces, and dividing it would name a
    count the user never typed.
    """
    if len(held) == len(wanted):
        return
    if subject is None:
        assert container is not None
        subject = container.info.short_name or container.info.name
    raise ValueError(
        f"{subject} {verb} {len(held):,} bytes of payload"
        f"{_units_note(kind, codec_id, held, reg)} where {len(wanted):,} bytes"
        f"{_units_note(kind, codec_id, wanted, reg)} were asked for{why}"
    )


def _units_note(kind: ContentKind, codec_id: str, data: bytes, reg: Registry) -> str:
    """`` (N tiles)`` for a refusal's byte count, or nothing where ``data`` does
    not read as units at all — a counted run its container cut mid-frame, whose
    byte counts still say what went wrong."""
    try:
        units = region_units(kind, codec_id, data, reg)
    except PipelineError:
        return ""
    return f" ({units:,} {_UNIT_NOUNS[kind]})"


def frames_new_file(
    cfg: PathwayConfig,
    *,
    kind: ContentKind,
    codec_id: str,
    units: int,
    reg: Registry,
) -> bool:
    """Whether ``cfg``'s container builds framing around a payload it is handed
    fresh.

    Probed by running the write against an empty destination and seeing whether
    anything came back beyond the packed payload — behavioural, for the same
    reason :func:`~celpix.pipeline.metrics.palette_has_alpha` is: it then holds
    for a third-party container too, with nothing new to declare, and it
    reports what the container really does rather than what it claims.

    It depends on the size as well as the container, and has to: a bank format
    builds its header for the payload lengths its family has and passes anything
    else through. Measured against the payload as the stages pack it, so a
    compressed file is not mistaken for a framed one.
    """
    data, _payload, packed = _blank_file(cfg, kind, codec_id, units, reg)
    return len(data) != len(packed)


def create_file(
    cfg: PathwayConfig,
    *,
    kind: ContentKind,
    codec_id: str,
    units: int,
    reg: Registry,
) -> int:
    """Create the blank file ``cfg`` writes to; returns how many payload bytes it
    holds.

    :func:`blank_file_bytes` decides what goes in it — see there for the payload
    and the framing. The file is written whole rather than spliced into, so
    naming an existing path replaces it, which is what the picker's overwrite
    prompt already asked about. The same two refusals as :func:`resize_file`,
    for the same reasons: one file, and a pathway with a write half.
    """
    from celpix.pipeline.pipeline import (  # noqa: PLC0415 — circular by nature
        replace_file,
    )

    _check_resizable(cfg)
    data, payload, _packed = _blank_file(cfg, kind, codec_id, units, reg)
    # Outside :func:`~celpix.pipeline._stage.run_stage`, unlike the framing
    # above: a path that cannot be written is the operating system's answer
    # about a filename, not a stage failing, and the caller has a better message
    # for it than a pipeline report. Replaced whole, so a failure part way
    # leaves the file the overwrite prompt was about rather than half of one.
    replace_file(cfg.write_target().path, data)
    return len(payload)


def _check_resizable(cfg: PathwayConfig) -> None:
    """Refuse, before anything is read, a region whose size cannot be set.

    - A region of **several files** cannot change size. Its boundaries are the
      lengths the files have on disk, and they are the only thing that says which
      bytes belong to which chip — see :func:`~celpix.pipeline.pipeline._deposit`,
      which refuses the same thing at the deposit for the same reason. Refused
      here as well so the caller is told before a multi-megabyte read, and told
      what is wrong rather than that some buffer was the wrong length.
    - A **view-only** pathway has no write half to put the bytes back through,
      which is the same reason :func:`~celpix.pipeline.pipeline.save` skips it.
    """
    target = cfg.write_target()
    if len(target.paths) > 1:
        raise ValueError(
            f"this region is {len(target.paths)} files joined, and resizing it "
            "would move the boundaries between them; nothing was written"
        )
    if not cfg.write_enabled:
        raise ValueError(
            "this region is read-only, so its size cannot be changed; nothing "
            "was written"
        )


def blank_units(kind: ContentKind, codec_id: str, nbytes: int, reg: Registry) -> int:
    """How many whole tiles, cells or colors ``nbytes`` holds — :func:`blank_size`
    read backwards.

    What a caller that has to state an *existing* region's size in the units the
    user thinks in needs: the file has a byte length, and the size row asks for
    tiles (``docs/design/new-file.md`` §6). Creating a file goes the other way
    and has :func:`blank_size`; resizing one needs both, because the number it
    puts in the spins has to come back out as the same file.

    **Floored**, because a partial trailing unit is not one any codec can read —
    the same rounding :func:`~celpix.pipeline.metrics.palette_entry_capacity`
    already applies for a packed format, which is why that is what answers for
    a palette rather than a division here.

    A length alone cannot answer for a codec with its own word on its runs — a
    counted sprite format's length is frame headers as well as pieces — so a
    caller that has the region's bytes asks :func:`region_units` instead.
    """
    if kind is ContentKind.PALETTE:
        return palette_entry_capacity(nbytes, codec_id, reg)
    per_unit = (
        tilemap_cell_bytes(codec_id, reg)
        if kind is ContentKind.TILEMAP
        else pixel_tile_bytes(codec_id, reg)
    )
    return max(0, nbytes) // per_unit if per_unit > 0 else 0


def region_units(kind: ContentKind, codec_id: str, data: bytes, reg: Registry) -> int:
    """How many tiles, cells or colors the payload ``data`` holds.

    :func:`blank_units` of its length for every format but a tilemap codec with
    its own word on its runs (:func:`~celpix.pipeline.metrics.resized_cells`):
    a counted sprite format's frames each open with a header, so its length
    divided by a piece's size over-counts, and a resize dialog seeded with that
    would append a piece on an OK meant to keep the size. That codec's cells are
    counted by decoding them, exactly as a load would — with an empty context,
    which the decode tolerates, there being no document to borrow one from.
    """
    if kind is ContentKind.TILEMAP and tilemap_resizes_itself(codec_id, reg):
        engine, preset = reg.engine_for(codec_id, TilemapCodecPlugin)
        return run_stage(
            Stage.INTERPRET_TILEMAP,
            Pathway.TILEMAP,
            lambda: len(engine.decode(data, preset.params, PipelineContext())),
            "decode",
            plugin=preset.id,
        )
    return blank_units(kind, codec_id, len(data), reg)


def resize_file(
    cfg: PathwayConfig,
    *,
    kind: ContentKind,
    codec_id: str,
    units: int,
    reg: Registry,
) -> int:
    """Resize the region ``cfg`` reads so it holds ``units`` tiles/cells/colors.

    The counterpart of :func:`create_file` for a file that already exists
    (``docs/design/new-file.md`` §6). The **payload** is what is resized — the
    bytes the container yields, after any reshape — so the framing is rebuilt
    around the new length rather than being counted as part of it: growing a
    tile bank by eight tiles adds eight tiles' worth of payload and lets the
    container restate its own header, which is the only thing that keeps the
    file readable as that format afterwards.

    Read and write are the same two halves an ordinary load and save use, in
    that order, so a resize goes through exactly the stages the entry does and
    cannot disagree with them about what the region's bytes are.

    **Growth is zero bytes** and shrinking is a plain truncation of the tail
    (:func:`_resized_payload`), for the reason :func:`blank_file_bytes` gives:
    zero is what an empty region reads as for every codec celPix carries, and
    the end of the region is the only place a caller can add or remove units
    without moving the ones already there — unless the codec says otherwise,
    as a counted sprite format does. That is the *payload's* tail. A whole file
    whose scheme marks its own end is read as a stream at the front of a longer
    region, and the region keeps its length: the re-packed stream is filled
    back out with the bytes that followed it
    (:func:`~celpix.pipeline.pipeline._fill_region`), so a trailer — padding,
    or data carved in with the blob — stays where it was. Only a stream that
    outgrows the region makes the file longer.

    Two refusals before anything is read (:func:`_check_resizable`): a region of
    several files, and a view-only pathway.

    And one after the write has run but before it reaches disk: a container
    whose format fixes the payload's length — a screen, a tile bank outside its
    three sizes, a copier image short of a whole block — is handed the resized
    bytes and answers with a file of the length it has. The result is read back
    through the entry's own stages and refused unless it holds exactly ``units``
    (:func:`_check_payload_held`), so a resize either happens or reports why
    not; it never reports a size the file does not have.

    Returns the payload's new length in bytes.
    """
    from celpix.pipeline.pipeline import (  # noqa: PLC0415 — circular by nature
        _deposit,
    )

    _check_resizable(cfg)
    target = cfg.write_target()
    current, resized, ctx, pathway = _resize_read(cfg, kind, codec_id, units, reg)
    # Already that many units: a run of cells resized to its own count is the
    # same bytes, so there is nothing to write.
    if resized == current:
        return len(current)
    size = len(resized)
    shaped = _repack(cfg, resized, ctx, reg, pathway)
    container = reg.plugin(Stage.CONTAINER, cfg.container_id, ContainerPlugin)

    def produce(dest: WriteTarget) -> bytes:
        result = container.write(shaped, dest, ctx)
        # A container whose format fixes the payload's length keeps the file at
        # that length whatever it was handed, and the only honest answers are
        # the size that was asked for or nothing written.
        held = _payload_held(cfg, result, reg, pathway)
        _check_payload_held(container, kind, codec_id, held, resized, reg, "keeps")
        return result

    run_stage(
        Stage.CONTAINER,
        pathway,
        lambda: _deposit(target, produce),
        "write",
        plugin=cfg.container_id,
    )
    return size


def resized_slot_bytes(
    cfg: PathwayConfig,
    *,
    kind: ContentKind,
    codec_id: str,
    units: int,
    reg: Registry,
) -> bytes:
    """The bytes a compressed slice's slot holds once it unpacks to ``units``.

    :func:`resize_file` for a region that has **no file position of its own to
    write at**: a slice is part of its parent's region, and its bytes reach the
    disk only through the parent's write (``docs/design/slices-and-parents.md``
    §4), which is what runs the container's own repairs — a ROM's checksums
    among them. So nothing is written here. The payload is read, grown with
    zeroes or cut at the tail exactly as :func:`resize_file` does it, and packed
    back into the slot; the host splices the result into the parent's buffer at
    the slice's offset, as an ordinary edit.

    **The slot does not grow.** The re-packed stream must fit the slice's length
    and raises the slot-overflow refusal when it does not — widening the slice
    over free space after it is how a bigger stream gets room. A shorter stream
    is padded per the slice's ``slot_fill``, as any save of it would be.

    Read back before it is returned, like :func:`resize_file`'s result: the bytes
    have to unpack to exactly the size asked for, or the resize is refused rather
    than reported done.
    """
    from celpix.pipeline.pipeline import (
        _read_reshape_decompress,  # noqa: PLC0415 — circular by nature
    )

    _current, resized, ctx, pathway = _resize_read(cfg, kind, codec_id, units, reg)
    shaped = _repack(cfg, resized, ctx, reg, pathway)
    # A slot of these bytes and no others, where the slice's own offset still
    # names its first byte — so what is read back is this stream, not the one
    # still standing in the file past a short result.
    start = cfg.source.offset
    preview = replace(
        cfg,
        source=replace(cfg.source, length=len(shaped), data=shaped, data_base=start),
    )
    held = _read_reshape_decompress(preview, PipelineContext(), reg, pathway)
    _check_payload_held(
        None,
        kind,
        codec_id,
        held,
        resized,
        reg,
        "unpacks to",
        subject="the re-packed slot",
        why="",
    )
    return shaped


def resize_lengths(
    cfg: PathwayConfig, kind: ContentKind, codec_id: str, units: int, reg: Registry
) -> tuple[int, int, PipelineContext]:
    """The payload's length now and once resized to ``units``, with the context
    the region was read under — ``(before, after, ctx)``.

    What a resize dialog has to know before it writes: whether this is a shrink
    worth asking about, how many bytes it drops, and where the region will end.
    Measured by the very resize :func:`resize_file` runs (:func:`_resize_read`)
    rather than by :func:`blank_size`, which is the length of a *new* run of so
    many units: a counted sprite run that keeps several frames keeps each one's
    header, and a byte tail a region carried past its last frame stays too. The
    context is returned because only the container knows where the region
    starts, and it says so as ``KEY_SOURCE_OFFSET``.
    """
    current, resized, ctx, _pathway = _resize_read(cfg, kind, codec_id, units, reg)
    return len(current), len(resized), ctx


def _resize_read(
    cfg: PathwayConfig, kind: ContentKind, codec_id: str, units: int, reg: Registry
) -> tuple[bytes, bytes, PipelineContext, Pathway]:
    """The region's payload as it stands, and as ``units`` asks it to be.

    The read half both resizes and :func:`resize_lengths` share, returned with
    the context it was read under — the re-pack has to run against the same
    one, since the stages that read the payload are what published how to
    write it back.
    """
    from celpix.pipeline.pipeline import (
        _read_reshape_decompress,  # noqa: PLC0415 — circular by nature
    )

    pathway = _new_file_pathway(kind)
    ctx = PipelineContext()
    current = _read_reshape_decompress(cfg, ctx, reg, pathway)
    resized = _resized_payload(kind, codec_id, current, units, reg)
    return current, resized, ctx, pathway


def _repack(
    cfg: PathwayConfig,
    resized: bytes,
    ctx: PipelineContext,
    reg: Registry,
    pathway: Pathway,
) -> bytes:
    """``resized`` — a payload already at its new size
    (:func:`_resized_payload`) — compressed and un-reshaped: what a resize hands
    the container, or splices into a slot, and what a new file is made of
    (:func:`blank_file_bytes`).
    """
    from celpix.pipeline.pipeline import (
        _compress_unshape,  # noqa: PLC0415 — circular by nature
    )

    # The payload is what changes size, not the region: the read recorded the
    # region's length on ``ctx``, and the re-pack fills back up to it exactly as
    # a save does, so a whole compressed file keeps whatever followed its stream
    # where it was. A bounded slot is still the slot, which this does not touch.
    return _compress_unshape(cfg, resized, ctx, reg, pathway)
