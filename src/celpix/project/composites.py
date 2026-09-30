"""Composite entries and swatch views (``docs/design/composite-entry.md``).

A composite owns no file: its buffer is other entries' decoded bytes joined in
order. Here is which entries may be its pieces (:func:`can_compose`) or supply a
palette (:func:`can_supply_palette`), the pixel format it is read at
(:func:`composite_preset_id`), how its buffer is assembled and where each piece
landed (:func:`composite_layout`, :func:`record_composite_layout`), and its
pathway config (:func:`composite_config`) — plus the swatch presets a palette
or a composite viewed as colours is read through (:func:`swatch_preset_id`).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from celpix.core import ceil_div
from celpix.core.address import format_hex
from celpix.core.capabilities import ContentKind
from celpix.core.errors import PipelineError, Stage
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import (
    DEFAULT_PIXEL_PRESET,
    NO_COMPRESSION,
    PALETTE_SWATCH_ENGINE,
    FileRef,
)
from celpix.plugins.registry import Registry
from celpix.project.configs import entry_view_bytes, interpret_params_for
from celpix.project.entry import (
    CompositeLayout,
    CompositePiece,
    Entry,
    EntryKind,
    EntrySession,
    PaletteMode,
    PieceSpan,
)

if TYPE_CHECKING:
    from celpix.project.workspace import Workspace


def is_swatch_preset(preset_id: str, registry: Registry) -> bool:
    """Whether ``preset_id`` reads bytes through the **palette-swatch** engine.

    Decided by the engine rather than the shipped preset's id, so a user's own
    preset over the same engine counts too. A format this build hasn't got is
    not one: nothing can be said about bytes read through it.
    """
    return (
        bool(preset_id)
        and registry.has_preset(preset_id)
        and registry.preset(preset_id).engine_id == PALETTE_SWATCH_ENGINE
    )


def composite_preset_id(entry: Entry, registry: Registry) -> str:
    """The pixel format a new composite view **starts** on: its first source's.

    A seed, not a rule. The first source's format is the best guess available
    when the entry is created, and it is usually right — but it is only a guess,
    because **which depth a composite is read at belongs to whatever consumes it,
    not to its pieces.** A console's tile window is depth-agnostic: the same
    bytes are 4bpp to one background layer and 2bpp to another, and a corpus of
    seventeen assembled windows has nine that are read at a depth none of their
    sources use. A composite that could not disagree with its pieces could not
    express those at all.

    So the picker stays the user's, exactly as it is on any other pixel entry,
    and this only decides where it starts (``docs/design/composite-entry.md``).

    Falls back to the stage's default when nothing resolves — an empty composite,
    or one whose every source is closed — since *something* has to say how many
    bits a pixel is before anything can be drawn, and this is the same stand-in a
    missing preset gets (:data:`~celpix.plugins.base.DEFAULT_PIXEL_PRESET`).
    """
    for piece in entry.pieces:
        source = piece.entry
        if source is None:
            continue
        if source.session is None:
            # A registered palette nobody has opened has no session yet, and
            # the one it will get reads its bytes as swatches — so a composite
            # of colour tables starts as one (:func:`swatch_session_for`).
            if source.kind is EntryKind.PALETTE:
                return swatch_preset_id(registry)
            continue
        return registry.resolve_preset(
            Stage.INTERPRET_PIXEL, source.session.pixel_preset_id
        )
    return DEFAULT_PIXEL_PRESET


#: The shipped palette-swatch preset — what a composite is switched to when it
#: is made a colour table and nothing else names one.
VIEW_AS_PALETTE_PRESET = "preset.pixel.view-as-palette"


def composite_format_for(entry: Entry, registry: Registry, *, palette: bool) -> str:
    """The pixel format ``entry`` would need to read as swatches — or as pixels.

    A composite's **Pixel / Palette** choice is not a field of its own: it *is* whether
    its format is over the palette-swatch engine, since that is what every other
    question — its section in the Files list
    (:func:`~celpix.project.workspace.section_kind`), its unit, its bar — already asks.
    So the choice is carried out by picking a format, and this is the one place that
    picks it.

    Empty when the format ``entry`` reads at already answers ``palette``, so a
    choice that changes nothing costs nothing — above all, not the depth a user
    chose for a pixel composite. Otherwise a palette is the shipped swatch preset
    (or any over the same engine, if a build has dropped it), and pixels are the
    seed :func:`composite_preset_id` would give — unless that seed is itself a
    source read as swatches, where the stage default stands in, because a
    composite asked to be pixels must not come back as a colour table.
    """
    session = entry.session
    current = (
        session.pixel_preset_id
        if session is not None
        else composite_preset_id(entry, registry)
    )
    if is_swatch_preset(current, registry) == palette:
        return ""
    if palette:
        return swatch_preset_id(registry)
    seed = composite_preset_id(entry, registry)
    if is_swatch_preset(seed, registry):
        return DEFAULT_PIXEL_PRESET
    return seed


def swatch_preset_id(registry: Registry) -> str:
    """The pixel format that reads bytes as palette swatches.

    The shipped *View as Palette* preset, or any other over the same engine if
    a build has dropped it; ``""`` where there is none at all. What a swatch
    composite is switched to, and what a palette file opens on.
    """
    if registry.has_preset(VIEW_AS_PALETTE_PRESET):
        return VIEW_AS_PALETTE_PRESET
    return next(
        (
            preset.id
            for preset in registry.presets(Stage.INTERPRET_PIXEL)
            if preset.engine_id == PALETTE_SWATCH_ENGINE
        ),
        "",
    )


def swatch_session_for(
    entry: Entry, registry: Registry, palette_preset_id: str
) -> EntrySession:
    """``entry``'s session as a palette file's swatch view needs it.

    A palette file opens as swatches, in the colour format its palette is
    decoded with — the one fact seen from both sides
    (:attr:`Entry.palette_preset_id`), which is why the session's
    ``palette_view_preset_id`` is written from the entry here rather than
    remembered on its own. The session the entry already has is kept for
    everything else (its selection, the format it was last on), and only put
    right where it disagrees: a project written before palettes opened may
    carry any pixel format, and the dock's mode is **File** on the entry's own
    file, since its colours are its own (``docs/design/palette-editing.md`` §2).

    ``palette_preset_id`` seeds the dock's format for a session built here; the
    dock in File mode shows the entry's own format, so it is only a fallback.

    Installs the session on ``entry`` — an existing one is corrected in place,
    so whatever already holds it sees the correction. :func:`swatch_session`
    is the same answer without touching the entry.
    """
    entry.session = _as_swatches(entry, entry.session, registry, palette_preset_id)
    return entry.session


def swatch_session(
    entry: Entry, registry: Registry, palette_preset_id: str
) -> EntrySession:
    """The session :func:`swatch_session_for` would install, left uninstalled.

    For a caller that needs to know what a palette file *will* open on without
    opening it — a slice cut from a palette nobody has opened yet seeds its
    session from this — since a palette's session is saved with the project,
    and asking the question must not rewrite the answer on disk.
    """
    current = replace(entry.session) if entry.session is not None else None
    return _as_swatches(entry, current, registry, palette_preset_id)


def _as_swatches(
    entry: Entry,
    session: EntrySession | None,
    registry: Registry,
    palette_preset_id: str,
) -> EntrySession:
    """``session`` put right for ``entry``'s swatch view — or built, if None."""
    if session is None:
        session = EntrySession(
            pixel_preset_id=swatch_preset_id(registry),
            palette_preset_id=entry.palette_preset_id or palette_preset_id,
            preview_compression_id=NO_COMPRESSION,
        )
    elif not is_swatch_preset(session.pixel_preset_id, registry):
        session.pixel_preset_id = swatch_preset_id(registry)
    session.palette_mode = PaletteMode.FILE
    if entry.palette_preset_id:
        session.palette_view_preset_id = entry.palette_preset_id
    return session


def can_compose(entry: Entry, candidate: Entry) -> bool:
    """Whether ``candidate`` is something ``entry`` could take a piece from.

    The one rule behind both the picker that offers sources and the assembly that
    reads them, so what is offered and what is accepted cannot disagree — the
    discipline :meth:`~celpix.ui.main_window.bindings.BindingsMixin.
    _can_supply_tiles` follows, for the same reason.

    **A composite cannot contain a composite.** Without that, a pair pointed at
    each other would each assemble the other, forever; with it, the depth
    question has one answer instead of a recursion limit. Nor itself, which would
    be that same loop with one participant.

    Only **pixels**: a palette or a bookmark has no pixel buffer to contribute,
    and a tilemap's is a borrowed copy of somebody else's art rather than bytes
    of its own — assembling one would put a borrowed copy inside a composite.
    """
    return candidate is not entry and is_composable(candidate)


def is_composable(candidate: Entry) -> bool:
    """The half of :func:`can_compose` that needs no composite to ask it.

    For a question put before there is one — the Files list offering *New
    Composite View* on a row or a selection has only the rows to go on — so the offer
    and the dialog it opens answer by the same rule.
    """
    # ``has_document`` as well as the kind test: a bookmark inherits PIXELS by
    # default, yet is a position with no buffer behind it. A palette file and a
    # slice of one pass both tests, which is what lets a colour-RAM image be
    # assembled out of ``.pal`` runs as readily as out of ROM slices.
    return (
        candidate.kind.has_document
        and candidate.kind is not EntryKind.COMPOSITE
        and candidate.content_kind is ContentKind.PIXELS
    )


def can_supply_palette(entry: Entry, candidate: Entry) -> bool:
    """Whether ``candidate``'s bytes could be read as ``entry``'s palette.

    The one rule behind both the picker that offers sources and the loader that
    reads them, so what is offered and what is accepted cannot disagree — the
    discipline :func:`can_compose` follows, for the same reason.

    **Anything with a document, holding pixels.** Bytes are bytes: a palette
    read out of a ROM is a run of colour words wherever it sits, so a file, a
    slice of one and a composite view assembling several all qualify, and the
    source need not be showing itself as swatches. What is excluded is what has
    no buffer of its own to read — a bookmark — and a tilemap, whose
    ``pixel_data`` is a borrowed copy of somebody else's art.

    A **palette file** and its slices qualify as sources like any other pixel
    entry — a ``.pal`` is a run of colour words, which is what makes it one —
    but a palette entry never *takes* one: the colours its dock shows are a
    decode of its own bytes, and reading somebody else's there would leave its
    swatches and its dock describing two different files.

    **No cycle is possible**, so there is nothing here to guard against one: a
    palette is decoded from *pixel* bytes, and no entry's pixel bytes are ever
    decoded from a palette. A chain of two is the longest there is — a composite
    of slices, read as somebody's colours — and each link is already resolved by
    the rule that owns it.
    """
    return (
        candidate is not entry
        and entry.kind is not EntryKind.PALETTE
        and candidate.kind.has_document
        and candidate.content_kind is ContentKind.PIXELS
    )


def _piece_problem(
    entry: Entry, piece: CompositePiece, workspace: Workspace | None
) -> str | None:
    """Why ``piece`` cannot contribute its source's bytes, or None if it can.

    A **closed** source is the case worth naming: a piece holds the entry object
    itself, so closing it does not stop the file being readable off disk — but a
    piece pointing into a list the entry has left is exactly the "not open"
    state a tilemap binding reports rather than quietly going on drawing
    (``docs/design/tilemap-entry.md`` §1). Asked of the workspace when there is
    one; without one there is no list to be absent from.
    """
    source = piece.entry
    if source is None:
        return None  # a pad, which is not a problem but the plan
    if not can_compose(entry, source):
        return f"{source.name} cannot supply bytes to a composite"
    if workspace is not None and not any(e is source for e in workspace.entries):
        return f"{source.name} is not open"
    return None


def composite_layout(
    entry: Entry,
    registry: Registry,
    workspace: Workspace | None = None,
    preset_id: str = "",
) -> CompositeLayout:
    """Assemble ``entry``'s pieces into one buffer, in list order.

    Each source piece contributes that entry's **resolved** pixel bytes — what it
    looks like once its own container, reshape and decompressor have run
    (:func:`entry_view_bytes`, which serves its live document's bytes when it has
    one, so an edit to a source shows through the composite immediately).

    A piece with a **range** takes only those bytes of it
    (:attr:`CompositePiece.is_ranged`), zero-filled where the source is too short
    to fill it: a stated range is a statement about the composite's layout, so it
    keeps its length whatever the source turns out to hold. The fill is recorded
    as a span nobody owns (:class:`PieceSpan`), exactly as a pad is. A piece without one
    takes everything from ``offset`` on, **rounded up to a whole tile** — that
    rounding is the point of the feature rather than tidiness, since a composite
    view exists to give a map one predictable index space and a ragged source
    would put every tile after it part of a tile out. A stated range is honoured
    to the byte instead, because at that point the user has said where the run
    ends and rounding would move the next one.

    A pad piece — and a source that is closed, unreadable, or one this refuses
    (:func:`can_compose`) — contributes ``piece.extent`` blank bytes. So does an
    un-ranged piece whose ``offset`` starts at or past the end of what its source
    resolves to, which has no bytes left to take: without the fill that run would
    disappear rather than go blank. Contributing *nothing* would be worse than
    blank in either case: every cell of every map bound to the composite would
    shift by the length of the missing run. A **ranged** piece never reaches that
    — it fills to its stated ``length`` whatever the source holds, including
    nothing.

    ``preset_id`` is resolved before it is asked for a tile size
    (:meth:`~celpix.plugins.registry.Registry.resolve_preset`), so a composite whose
    stored format this build has not got assembles at the stage's default instead of
    taking the window down with a ``KeyError`` — the same stand-in
    :func:`composite_preset_id` falls back to. Which format the *view* then reads the
    buffer through is still the entry's own business
    (:func:`~celpix.project.entrystate.repair_presets`); this only needs a tile to round
    to.

    Returns the refreshed pieces alongside, each carrying what was actually
    assembled as its ``measured`` — never as its ``length``, which is the user's
    request and not this function's to touch.
    :func:`record_composite_layout` is what records them, on the same
    "discovered at load, written onto the entry" footing as
    :func:`~celpix.project.configs.backfill_slice_length`.
    """
    # Only the un-ranged case needs this, and only to round a ragged source up.
    # Everything else here is bytes, which is the unit a source and the composite
    # reading it can both agree on (:class:`CompositePiece`).
    tile_bytes = pipeline.pixel_tile_bytes(
        registry.resolve_preset(
            Stage.INTERPRET_PIXEL, preset_id or composite_preset_id(entry, registry)
        ),
        registry,
    )
    out = bytearray()
    spans: list[PieceSpan] = []
    refreshed: list[CompositePiece] = []
    problems: list[str] = []
    for piece in entry.pieces:
        data, owner = None, None
        if not piece.is_pad:
            refused = _piece_problem(entry, piece, workspace)
            if refused is not None:
                problems.append(f"{refused}, so its run is blank")
            else:
                try:
                    # The preset is inert here — ``entry_view_bytes`` stops
                    # before any codec runs — so the composite's own is handed
                    # over rather than each source's being resolved for a value
                    # nothing reads.
                    data, _base = entry_view_bytes(
                        piece.entry, registry, preset_id, workspace
                    )
                    owner = piece.entry
                except (PipelineError, OSError) as exc:
                    problems.append(f"{piece.entry.name} could not be read ({exc})")
                    data = None
        # How many of the run's bytes the owner actually holds. The rest is fill,
        # and fill has no owner: the two spans below are what keeps a stroke
        # over it refused rather than deposited past the end of a file.
        held = 0
        if data is None:
            # The pad case, and every failure above: the run keeps the length it
            # last had so nothing downstream of it moves.
            data = bytes(piece.extent)
        else:
            end = piece.offset + piece.length if piece.length else len(data)
            cut = data[piece.offset : end]
            held = len(cut)
            if piece.length and len(cut) < piece.length:
                problems.append(
                    f"{piece.entry.name} resolves to {len(data)} bytes, too short for "
                    f"the {piece.length} asked for at {format_hex(piece.offset)}; the "
                    "rest of the run is blank"
                )
                cut = cut + bytes(piece.length - len(cut))
            elif not piece.length and not cut:
                # Nothing lies at ``offset``, and an un-ranged run has no stated
                # length to fall back on — so it would contribute no bytes at
                # all and pull every run after it up by the length it used to
                # have. Held to its last extent and blanked instead, exactly as
                # an unreadable source is, and named for the same reason: a
                # blank run the user can see is recoverable, a silent renumber
                # of every map bound to the composite is not.
                problems.append(
                    f"{piece.entry.name} resolves to {len(data)} bytes, so the "
                    f"{format_hex(piece.offset)} this run starts at is past the "
                    "end of it; the whole run is blank"
                )
                cut, held = bytes(piece.extent), 0
            elif not piece.length and len(cut) % tile_bytes:
                filled = ceil_div(len(cut), tile_bytes) * tile_bytes
                problems.append(
                    f"{piece.entry.name} holds {len(cut)} bytes, which is not a whole "
                    f"number of {tile_bytes}-byte tiles; it was padded to {filled}"
                )
                cut = cut + bytes(filled - len(cut))
            data = cut
        if held:
            spans.append(PieceSpan(len(out), held, owner, piece.offset))
        if len(data) > held:
            spans.append(PieceSpan(len(out) + held, len(data) - held, None))
        out += data
        refreshed.append(replace(piece, measured=len(data)))
    return CompositeLayout(bytes(out), tuple(spans), tuple(refreshed), tuple(problems))


def composite_config(
    entry: Entry,
    registry: Registry,
    workspace: Workspace | None = None,
    layout: CompositeLayout | None = None,
    preset_id: str = "",
) -> PathwayConfig:
    """A composite's pixel pathway: the assembled buffer, read as plain bytes.

    ``layout`` is an assembly already in hand — the load path has one, having
    needed its problems and its run lengths, and assembling a second time would
    re-read every source for the same bytes. Omitted, one is made here.

    Every byte stage is on its pass-through, because the pieces have already been
    through theirs — each was read by its own entry's container, reshape and
    decompressor before it was joined. Running a second set over the join would
    be applying a file's framing to a buffer that is not that file.

    ``write_enabled=False``, and that is not a limitation: a composite owns no
    bytes, so there is nothing here to deposit. An edit made on one is deposited
    into the piece that owns it as it lands, and *that* entry's write is what puts
    it on disk (``docs/design/composite-entry.md``). Saying so here is what stops
    :func:`~celpix.pipeline.pipeline.save` from writing the assembled buffer to a
    file named after the entry.
    """
    if layout is None:
        layout = composite_layout(entry, registry, workspace, preset_id)
    return PathwayConfig(
        # The entry's own name as the source path: nothing opens it — the bytes
        # are supplied inline — but the address display and every error message
        # want something to call this buffer, and a composite has no file to
        # borrow a name from.
        source=FileRef((entry.name or "composite",), data=layout.data),
        interpret_preset_id=preset_id or composite_preset_id(entry, registry),
        interpret_params=interpret_params_for(
            entry, preset_id or composite_preset_id(entry, registry), registry
        ),
        write_enabled=False,
    )


def record_composite_layout(entry: Entry, layout: CompositeLayout) -> bool:
    """Record what an assembly measured onto ``entry``; True if a length changed.

    Two things, and they are recorded together because one assembly is where both
    become known — and because letting them be answered separately is what let
    them disagree:

    - each piece's ``measured``, a cache of how many bytes that run actually
      produced, refreshed here at load exactly as a decompressed slice's discovered
      extent is (:func:`~celpix.project.configs.backfill_slice_length`). Keeping it
      current is what lets a closed source leave a hole of the right size behind it
      instead of collapsing the composite.
    - :attr:`Entry.piece_spans`, where each of those runs sits in the buffer the
      assembly just built — the **one** answer to "whose byte is this?", and the
      only one, since it describes a buffer that exists rather than re-deriving a
      guess from recorded lengths.

    Neither touches ``offset`` or ``length``: those are what the user asked for,
    and a measurement that could overwrite a request would quietly turn a stated
    range into whatever its source happened to be this time.

    The return value is about the *lengths* alone — the spans are refreshed every
    time, and are not something a caller re-reads the file over.
    """
    entry.piece_spans = layout.spans
    if layout.pieces == entry.pieces:
        return False
    entry.pieces = layout.pieces
    return True
