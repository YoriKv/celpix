"""One open entry, and the values it carries.

:class:`Entry` is a row of the open-entries list — its kind
(:class:`EntryKind`), its bounds, the references it holds by identity
(:class:`PaletteSource`, :class:`TileSource`, :class:`CompositePiece`), its
session and restore state, and its lazily loaded document — together with the
constructors for a slice or a composite and the walks that need nothing but
the entries themselves (:func:`slice_links`, :func:`anchor_kind`). Pure data:
reading an entry is :mod:`celpix.project.configs`' job, and holding a list of
them is :class:`~celpix.project.workspace.Workspace`'s.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto

from celpix.core.capabilities import Capability, ContentKind, supports
from celpix.core.document import Document, ViewOptions
from celpix.core.errors import PipelineError, fault_report
from celpix.core.font import Glyph
from celpix.core.tilemap import IndexAddressing
from celpix.pipeline.pathway import DEFAULT_SLOT_FILL, SlotFill
from celpix.plugins.base import (
    DEFAULT_PALETTE_PRESET,
    NO_COMPRESSION,
    NO_RESHAPE,
    RAW_CONTAINER,
)
from celpix.project.inputs import InputBinding


class EntryKind(Enum):
    FILE = auto()
    SLICE = auto()
    BOOKMARK = auto()
    PALETTE = auto()
    # An ordered concatenation of *other* entries' pixel bytes, owning no file of
    # its own (``docs/design/composite-entry.md``). A fourth answer to the
    # question this enum asks — how an entry is **bounded** — and not a fifth
    # :class:`~celpix.core.capabilities.ContentKind`: what a composite holds is
    # pixels, which is what lets it be a tile bank a map binds to.
    COMPOSITE = auto()

    @property
    def has_document(self) -> bool:
        """Whether entries of this kind can be shown, and so have a document.

        Asked by name rather than as a set of kinds, so a new kind with a
        document is one answer here and not a dozen sites to find: a file, a
        slice and a composite each have a document, a view, a palette and an
        undo history.

        A **palette** is the fourth: its file opens as a sheet of swatches, the
        way a composite read through the swatch codec does, and it can be sliced
        like any file (``docs/design/palette-editing.md`` §2). A bookmark is a
        position, so it is never current and has no view of its own.
        """
        return self in (
            EntryKind.FILE,
            EntryKind.SLICE,
            EntryKind.COMPOSITE,
            EntryKind.PALETTE,
        )


class SortKey(str, Enum):
    """What a group of rows is put in order by — see
    :func:`~celpix.project.workspace.sorted_entries`.

    The list's order is the user's
    (:meth:`~celpix.project.workspace.Workspace.reorder`), so this is a gesture
    they ask for rather than a state anything keeps: nothing persists it, and
    the next hand-placed row is not moved back.
    """

    NAME = "name"
    OFFSET = "offset"
    TYPE = "type"


class PaletteMode(str, Enum):
    """Where an entry's palette colors come from.

    ``value`` is the stable string persisted in the project file (str-valued for
    exactly that reason, like :class:`~celpix.core.errors.Stage`), so the on-disk
    schema is unchanged by this being a type rather than a bare string.

    The distinctions between the modes drive several different decisions, and the
    properties below are the single statement of each — so the window, the
    workspace and the project reader all branch on one named question rather than
    each carrying its own literal set of modes to keep in step by hand. See
    ``docs/design/palette-editing.md`` for what a color edit can be written back
    to in each.
    """

    DEFAULT = "default"  # the generated fallback palette
    FILE = "file"  # a standalone palette file
    OFFSET = "offset"  # raw bytes at an offset in the entry's own pixel file
    EMULATOR = "emulator"  # pulled from an emulator save state (view-only)
    CUSTOM = "custom"  # colors stored in the .celpix project itself
    # Raw bytes at an offset in **another open entry's** resolved data. A game
    # that builds one colour table out of several ROM places assembles those
    # places as a composite view, and this is what lets a graphic read the
    # result as its palette (``docs/design/palette-editing.md``).
    ENTRY = "entry"

    @classmethod
    def parse(cls, value: object) -> PaletteMode:
        """``value`` as a mode, falling back to DEFAULT for anything unknown —
        a hand-authored or newer project file names a mode this build has no
        meaning for, and opening on the generated palette beats failing."""
        try:
            return cls(value)
        except ValueError:
            return cls.DEFAULT

    @property
    def is_real(self) -> bool:
        """Whether real colors are in play, as opposed to the generated fallback.

        Anything but DEFAULT must survive a pixel reload rather than being
        regenerated at the new format's index space.
        """
        return self is not PaletteMode.DEFAULT

    @property
    def has_source(self) -> bool:
        """Whether the palette can be re-read/re-decoded from somewhere.

        Narrower than :attr:`is_real`: CUSTOM is real but exists only in the
        project, so a format re-decode or plugin refresh has nothing to load.
        """
        return self in (
            PaletteMode.FILE,
            PaletteMode.OFFSET,
            PaletteMode.EMULATOR,
            PaletteMode.ENTRY,
        )

    @property
    def decodes_raw_bytes(self) -> bool:
        """Whether the palette is decoded from raw bytes through a color codec,
        so the format picker can *reinterpret* those bytes.

        FILE and OFFSET read a file and ENTRY another entry's resolved bytes; an
        EMULATOR state's console dictates the initial codec, but the picker still
        lets the user override how its bytes are read. DEFAULT and CUSTOM carry
        their own colors (generated, or ARGB stored in the project), so no codec
        choice applies — CUSTOM shows the format it carries, but read-only.

        Coincides with :attr:`has_source` — anything with bytes to re-read has
        bytes to reinterpret — and is defined from it so the two cannot drift.
        """
        return self.has_source

    @property
    def has_external_file(self) -> bool:
        """Whether the colors come from a file of their own, whose name the
        palette dock shows and whose loss degrades the entry.

        False for ENTRY, whose source is another *entry* rather than a file: the
        entry may be a composite, which comes from no file at all, and the row
        it names is in the same project rather than on disk beside it.
        """
        return self in (PaletteMode.FILE, PaletteMode.EMULATOR)

    @property
    def holds_edits(self) -> bool:
        """Whether a color edit can land on this palette as it stands.

        The generated default has nowhere to store one, and an emulator state is
        never written back — so an edit on either forks to Custom first. Named
        here with the other mode questions rather than as a literal mode set at
        each editing entry point.

        True for ENTRY: the colours live in another entry's bytes, and an edit
        is deposited there exactly as a pixel edit on that entry would be. Where
        those particular bytes have no writable owner — a composite's pad — the
        edit is refused rather than forked, because forking would silently sever
        the link to the ROM (``docs/design/palette-editing.md``).
        """
        return self not in (PaletteMode.DEFAULT, PaletteMode.EMULATOR)

    @property
    def is_exportable(self) -> bool:
        """Whether "Export to File…" has anything to offer.

        FILE already *is* a ``.pal`` and DEFAULT is generated from nothing, so
        exporting either would only copy something the user already has.
        """
        return self not in (PaletteMode.DEFAULT, PaletteMode.FILE)


@dataclass
class PaletteSource:
    """Where an entry's palette colors come from, as restorable plain data.

    Exactly one shape is meaningful (``docs/design/project-format.md`` §4.3):
    inline ``colors`` (ARGB ints — the **custom** palette, which has no external
    source and lives entirely in the project), an external palette file ``path``
    (+ ``offset`` into it), another open ``entry`` (+ ``offset`` into its
    resolved bytes), or just an ``offset`` into the entry's own pixel file. A
    live entry keeps this information on its document's palette config — and,
    for the entry shape, on :attr:`Entry.palette_entry`, a file having no way to
    name an object; this form exists for entries whose document isn't loaded yet
    (project restore) and is consumed on first activation.

    ``entry`` is the source :class:`Entry` **itself**, held by identity for the
    reason :attr:`TileSource.entry` and :attr:`CompositePiece.entry` are: a
    positional index names a different entry the moment anything ahead of it is
    closed or reordered, so the palette would follow the number rather than the
    bytes the user pointed at. The project file stores the position instead,
    computed on save and resolved back on load, in
    :mod:`~celpix.project.projectfile` and nowhere else.
    """

    colors: list[int] | None = None
    path: str | None = None
    offset: int = 0
    entry: Entry | None = None


class TileMode(str, Enum):
    """Whether a tilemap entry's *tiles* are bound, and to what.

    ``value`` is the string persisted in the project file, like
    :class:`PaletteMode`'s.

    There is one bound shape, not two: the tiles are **always another open
    entry**. That entry may be a whole file, or a slice carving the tile bank
    out of a ROM, so every way of bounding bytes the editor already has is
    reused rather than given a second spelling here — and the tiles are read
    from that entry's *live document*, so an edit to the art shows through in
    the map immediately. Picking a file that is not open yet opens it as an
    entry first, the way a palette file is registered before it is applied.
    """

    NONE = "none"
    ENTRY = "entry"


@dataclass(frozen=True)
class TileSource:
    """Where a tilemap's tiles come from.

    ``entry`` is the bound :class:`Entry` **itself**, not a position in the list.
    That distinction is the whole of why this field is an object: a binding
    decides which file a pixel edit made through the map is *deposited* into
    (``docs/design/tilemap-entry.md`` §8.4), and a positional index silently names
    a different entry the moment anything ahead of it is closed or reordered — so
    the binding would follow the number rather than the file the user pointed at.
    Held by identity, which :class:`Entry` has (``eq=False``), so it costs a
    reference and cannot go stale while the entry is open.

    It is **not** what gets written. A project file has no way to name an object,
    so the position is computed on save and resolved back on load, in
    :mod:`~celpix.project.projectfile` and nowhere else — the one place a
    positional index is meaningful, since there the list is fixed.

    A binding whose entry has been **closed** still holds it, and answers "not
    open" rather than "somebody else"
    (:meth:`~celpix.ui.main_window.bindings.BindingsMixin._binding_target`). That is
    also what makes closing a tile bank undoable for free: the restore puts the
    same object back, and every map bound to it is bound to it again.

    ``base_index`` shifts every cell: cell index N draws source tile
    ``base_index + N``. It is what lets a map and its art be bound together when
    the two number their tiles from different places, without rewriting either.
    Bound to another **tilemap**, the same number shifts coordinates instead:
    cell index N names source cell ``base_index + N``
    (:attr:`~celpix.core.cellchain.CellChain.base`).

    It is **not** a format field. The header word that looks like one in a screen
    and a PNL panel is not a base index — celPix reads it from no format, and neither
    does the one independent implementation
    (``docs/graphics-formats-reference/scgcad-formats.md`` §2, "Header fields":
    read as a base index the corpus rules it out immediately).
    What makes it earn its place is the binding: the art a map draws from is
    routinely a *slice* of something bigger, and a slice's tiles start at 0
    however the map numbers them.

    Both directions are used, which is why it is signed. **Positive** when the
    bank sits partway into the bound entry — a map numbering from 0 against a
    whole ROM whose art begins at tile 0x2000. **Negative** when the map
    numbers from partway into a bank the slice starts at — a screen using
    tiles 0x100-0x1EE bound to a slice holding exactly those, where cell
    0x100 must draw the slice's tile 0. A cell that lands outside the source
    renders blank, so a wrong base is visible rather than corrupting anything.
    The base counts what the index counts — cells or tiles in corner
    addressing, whole stamps or metatiles in ordinal — and is added to the index
    before either becomes a place (:func:`~celpix.core.tilemap.index_corner`),
    so ``addressing`` changing re-counts it (:func:`~celpix.core.tilemap.
    rebased`).

    ``addressing`` is the binding's word on how an index numbers what it draws
    — its unit's corner, or a count of units
    (:class:`~celpix.core.tilemap.IndexAddressing`) — and wins over what the
    referring format states. None, the usual answer, leaves the format's
    (``project/documents.py``, ``index_reading``). On the binding because what
    an index counts is a fact about the pair: the units are the source's, cut
    the way the source lays them out.
    """

    mode: TileMode = TileMode.NONE
    entry: Entry | None = None
    base_index: int = 0
    addressing: IndexAddressing | None = None

    @property
    def is_bound(self) -> bool:
        """Whether this names a source at all — False renders as placeholders."""
        return self.mode is not TileMode.NONE


@dataclass(frozen=True)
class CompositePiece:
    """One run of a composite view: a byte range of another entry, or blank bytes.

    A composite view is an ordered list of these and nothing else
    (``docs/design/composite-entry.md``). Two shapes are meaningful: an ``entry``
    contributing its pixel bytes — all of them, or a range — or, with ``entry``
    None, a **pad** of blank bytes standing for a hole in the tile window being
    reproduced.

    What a source contributes is its **resolved** bytes: what it looks like once
    its own container, reshape and decompressor have run, which is what would
    have been uploaded to VRAM. That is the whole point of the feature, and it is
    what ``offset`` addresses — a position in the *decompressed* stream where the
    source is compressed, and in the slice's own bytes where it is not.

    ``entry`` is the bound :class:`Entry` **itself**, for the reason
    :class:`TileSource`'s is: a positional index names a different entry the
    moment anything ahead of it is closed or reordered, so the piece would follow
    the number rather than the data the user pointed at. The project file stores
    the position instead, computed on save and resolved back on load, in
    :mod:`~celpix.project.projectfile` and nowhere else.

    **A piece names a whole entry, and may take a range of it.** Which bytes an
    entry *has* is the entry's own business — its container, reshape,
    compression and format — and a piece restates none of that. What it may say
    is how much of the result to take:

    - ``length = 0`` is the ordinary case: from ``offset`` to the end.
    - ``offset`` and ``length`` together take a run out of the middle.

    That range is not a second spelling of a slice, because for a **compressed**
    source there is no slice that would do: a slice bounds the *compressed
    stream*, and what is wanted here is part of the *resolved output*, which no
    offset and length into the file can name. Real data needs it — a console DMAs
    half of a decompressed blob to one address and half of another below it,
    which is five of the sixteen runs in every screen of the corpus this was
    built for.

    **Bytes, not tiles.** The range addresses the source's own byte stream, which
    is a unit both sides agree on; a tile is not. The same bytes are read as 2bpp
    by one layer and 4bpp by another — this corpus does exactly that — so "tile
    64" means one thing to a source and another to the composite reading it,
    while byte 0x800 means the same thing to both.

    ``measured`` is how many bytes the last successful assembly actually
    produced. It is a **cache**, not a request, and it is separate from ``length``
    so that a refresh can never overwrite what the user asked for. Two things
    need it:

    - A piece whose entry is **closed or unreadable** contributes that many blank
      bytes rather than nothing, so the composite does not silently renumber
      itself — every cell of every map bound to it would move — because a source
      was closed. An undo putting the entry back puts its bytes back too.
    - The dialog's position column, which is the VRAM table the user is
      transcribing and has to read before the sources are loaded.

    0 until first assembled, which is the honest answer for a run nothing has
    measured yet.
    """

    entry: Entry | None = None
    offset: int = 0
    length: int = 0
    measured: int = 0

    @property
    def is_pad(self) -> bool:
        """Whether this run stands for a hole rather than naming a source."""
        return self.entry is None

    @property
    def is_ranged(self) -> bool:
        """Whether this piece takes part of its source rather than all of it."""
        return not self.is_pad and (self.length > 0 or self.offset > 0)

    @property
    def extent(self) -> int:
        """How many bytes this run covers, as best anything can say without reading.

        The **request** where there is one — a pad's length, or a range's — and
        the last measurement otherwise. That order is what makes a ranged piece
        answer for itself before its source has ever been read, and what keeps a
        whole-entry piece answering with the size its entry actually turned out
        to be rather than a stale guess.
        """
        if self.length > 0:
            return self.length
        return max(0, self.measured)


@dataclass(frozen=True)
class FileStages:
    """The byte stages a whole file is read and written through: its container,
    its reshape and its compression.

    The three settle together — in Edit File Container… for a file that exists,
    in New File… for one about to — because between them they decide which bytes
    the region even has, and any one alone leaves the entry re-read. Plain,
    Qt-free data so the dialog that asks, the entry that carries the answer
    (:attr:`Entry.file_stages`) and the config a load builds from it can pass
    one value rather than three. Defaults are the pass-throughs, which is what
    a file is until something says otherwise.

    A **palette** file carries only the container: its colours are read without
    a reshape or a decompressor, so neither is offered for one
    (``ui/file_stages.py``).
    """

    container_id: str = RAW_CONTAINER
    reshape_id: str = NO_RESHAPE
    compression_id: str = NO_COMPRESSION


@dataclass(frozen=True)
class SliceParams:
    """The entry fields a slice's coordinates comprise.

    Plain, Qt-free data shared by the slice dialog (which produces it) and the
    slice-edit undo command (which stores a before/after pair) — one type so a
    dialog result flows straight into a command without a field-by-field copy.

    ``content_kind`` is the odd one out: it says what the region *is* rather than
    where it lies, and only a new slice chooses it. An edit carries the entry's
    own kind in and back out unchanged, which is what keeps the before/after pair
    comparable (:class:`~celpix.ui.slice_dialog.SliceDialog`).
    """

    name: str
    offset: int
    length: int | None
    compression_id: str
    reshape_id: str = NO_RESHAPE
    content_kind: ContentKind = ContentKind.PIXELS
    # Not a coordinate either, but unlike the kind above it is decided by the
    # same answer the dialog is already asking for: it means something only under
    # a compression scheme, so it belongs to the row that chooses one.
    slot_fill: SlotFill = DEFAULT_SLOT_FILL
    # Not part of the re-point at all: the tiles (or cells) a compressed slice
    # should unpack to, or ``None`` where no resize was asked for. Carried here
    # because the dialog is where it is asked, and stripped before the pair
    # reaches the undo command — a resize is an edit to the parent's bytes and
    # goes on the stack as one (``docs/design/slices-and-parents.md`` §5).
    units: int | None = None
    # Whether ``length`` is the parent's to decide (:attr:`Entry.match_parent`).
    # ``length`` is still carried, as what the parent measured when the dialog
    # was answered, so the pair compares on the value the slice will read with.
    match_parent: bool = False


@dataclass
class EntrySession:
    """Per-entry snapshot of the UI session — what entry-switching restores.

    Plain data (project-file material). Only the state that is *not*
    already carried by the entry's :class:`Document` lives here: view geometry,
    offset/nudge and palette row are in ``Document.view``, and the palette
    itself plus both pathway configs are on the document.
    """

    pixel_preset_id: str
    palette_preset_id: str
    palette_mode: PaletteMode = PaletteMode.DEFAULT
    # The decompression-preview combo's position, which is a *view* setting: it
    # says what the toolbar was showing, not how the entry's bytes are read.
    # Entry.compression_id is that, and the two move independently.
    preview_compression_id: str = NO_COMPRESSION
    # The color format the palette-swatch view reads the entry's bytes through
    # — a decode choice, but the *entry's* rather than a preset's, since the same
    # "View as Palette" pick means BGR555 in one ROM and RGB888 in the next. Kept
    # whatever pixel format is showing, so switching back to swatches finds the
    # format that was last read here; it reaches the pipeline as the config's
    # ``interpret_params`` (:func:`~celpix.project.configs.interpret_params_for`).
    palette_view_preset_id: str = DEFAULT_PALETTE_PRESET
    # The selection. ``selected_tile`` is the anchor (and what single-selection
    # consumers read); ``selected_last`` >= it bounds a range, None when the
    # selection is a single tile (or absent). ``selection_slots`` is set only for
    # a *rectangle* selection — its (columns, rows) extent in canvas slots, which
    # together with the anchor and the restored view geometry re-derives exactly
    # which tiles it covered.
    selected_tile: int | None = None
    selected_last: int | None = None
    selection_slots: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        # PaletteMode is str-valued so it persists as itself, which makes a bare
        # string quietly *equal* to the right member while failing every ``is``
        # check the window branches on. Normalising here makes the annotation
        # true of every session however it was built - project file, plugin, or
        # test - so those identity comparisons are safe by construction.
        self.palette_mode = PaletteMode.parse(self.palette_mode)


@dataclass(frozen=True)
class LoadFailure:
    """Why an entry could not be opened, kept on it as :attr:`Entry.load_failure`.

    One shape for every way a load can fail — a stage that raised, a file that
    could not be read, a palette whose bytes are unreadable — so the list, the
    status bar and the unavailable state have one thing to show rather than one
    per cause. ``summary`` is the text to show, already in sentences; ``report``
    is the traceback where a plugin raised (:func:`~celpix.core.errors.fault_report`),
    which a plugin's author needs and nobody else has to read.
    """

    summary: str
    report: str = ""

    @classmethod
    def from_error(cls, exc: PipelineError) -> LoadFailure:
        """The failure a stage reported: its summary, with the traceback behind it."""
        fault = exc.fault
        return cls(exc.summary(), fault_report(fault) if fault is not None else "")


class ParentSliceMissing(Exception):
    """A nested slice was asked to open with no parent slice to read.

    Its own exception rather than a :class:`~celpix.core.errors.PipelineError`,
    because no stage ran and no plugin failed: the bytes it windows into are
    simply not in the list
    (:meth:`~celpix.project.workspace.Workspace.chain_broken`). The message is
    the whole of what the load failure says.
    """


class SliceOutsideParent(Exception):
    """A nested slice was asked to open on a window its parent slice's decoded
    bytes do not reach (:func:`~celpix.project.configs.outside_parent`).

    Its own exception for :class:`ParentSliceMissing`'s reason: no stage ran and
    no plugin failed. The message is the whole of what the load failure says.
    """


@dataclass(eq=False)  # identity semantics: two slices may share coordinates
class Entry:
    """One open item: a whole file, an offset+length slice of one, a bookmark,
    a palette file, or a composite of other entries' bytes.

    ``path`` is the file itself for FILE entries and the **root** file for
    SLICE and BOOKMARK entries. A bookmark is always anchored to a whole file. A
    slice is anchored to a whole file *or to another slice* — a **nested slice**
    (``parent_kind`` SLICE, the parent held in :attr:`parent_entry`), which
    windows into its parent's *decoded* bytes: tiles and a map unpacked from one
    stream are two slices of the slice that unpacks it. The chain nests to any
    depth and always ends at a FILE or a PALETTE, whose path every entry in it
    carries. ``slice_offset`` is an absolute offset from byte 0 of the file for a
    slice of a file — deliberately not header-relative, so a slice or bookmark
    never shifts when the parent's header-skip display setting changes — and an
    offset from byte 0 of the parent slice's decoded buffer for a nested one.
    ``slice_length`` may start ``None`` for a decompressed slice ("to be
    discovered"): the first load backfills it from the structure's true extent so
    save-back is slot-bounded.

    A BOOKMARK is a position marker, not a document: it has no length and is
    never loaded or made current. It repurposes the restore fields as its
    permanent settings snapshot — ``session``, ``pending_view`` and
    ``pending_palette`` hold the parent's state as of the bookmark's creation,
    and (unlike on a file/slice) are never consumed; jumping copies them back
    onto the parent.

    A PALETTE is an external palette file registered with the session: its
    ``path`` is the palette file itself (top-level, never a child of a FILE
    even when their paths collide), and ``palette_preset_id`` remembers the
    codec it was last read with - the format it was registered under, kept in
    step with the format dropdown while this file is the palette on screen - so
    applying it later decodes the same way it last did, regardless of where the
    dropdown has moved for some other palette since. It is applied *onto* the
    entry being shown, and it is also a **pixel document of its own**: opened,
    it shows its colours as swatches through the palette-swatch codec, and
    slices are carved from it as from any file. Its bytes are the authority for
    its colours — the palette the dock edits is a decode of them, and a colour
    edit lands in them (``docs/design/palette-editing.md`` §2).

    A COMPOSITE has **no file**: its bytes are its :attr:`pieces` — other
    entries' decoded buffers joined in order — so its :attr:`paths` is empty,
    its own pathway writes nothing, and an edit lands on the pieces it came from
    (``docs/design/composite-entry.md``).
    """

    name: str
    kind: EntryKind
    path: str
    # The files *after* ``path`` whose bytes join onto it, for a region spread
    # over several ROM chips (:class:`~celpix.plugins.base.FileRef`). ``path``
    # stays the entry's identity — the row in the list, the key slices and
    # bookmarks find their parent by, what a relocate repoints — so the extras
    # are carried beside it rather than folding it into a list; read them
    # together through :attr:`paths`, which is what addresses bytes.
    #
    # A **slice carries its parent's list**, not one of its own: its offset is
    # into the parent's joined buffer, so the same files have to be joined the
    # same way to mean anything. Copying them at creation keeps a slice able to
    # answer that on its own, without a workspace to look its parent up in. A
    # nested slice carries the list its whole chain ends at, since those are the
    # files its bytes finally live in — what a missing-file scan, a relocate and
    # a save's invalidation all key on.
    extra_paths: tuple[str, ...] = ()
    slice_offset: int = 0
    slice_length: int | None = None
    # The scheme the entry's bytes are unpacked through on load and re-packed
    # through on save. A SLICE's, chosen in the slice dialog — the usual case,
    # a structure inside a larger ROM. A FILE may carry one too, from Edit File
    # Container…: a blob lifted out of a ROM whole decompresses whole, and its
    # buffer is then the unpacked stream rather than any byte of the file, so
    # its slices and Offset palettes count in that buffer from 0 as a nested
    # slice's do (:func:`~celpix.project.configs.reorders_bytes`).
    compression_id: str = NO_COMPRESSION
    # What fills the room a re-compressed blob leaves at the end of this slice's
    # slot when it packs tighter than the one it replaces
    # (:class:`~celpix.pipeline.pathway.SlotFill`). A **slice** setting because
    # only a bounded region has a tail to decide about, and only its owner knows
    # whether those spare bytes are really its own — the slice dialog asks, and
    # only where a compression scheme is chosen, since nothing else can shrink.
    slot_fill: SlotFill = DEFAULT_SLOT_FILL
    # SLICE entries only: the length is **the parent's**, not the user's — the slice
    # runs from its offset to the end of whatever its parent holds, and follows it when
    # the parent is resized. ``slice_length`` stays the one field everything reads; it
    # is re-measured wherever a config is built
    # (:func:`~celpix.project.configs._fit_to_parent`), which is every read, so a parent
    # that grew or shrank is picked up by the next re-read of the slice with nothing to
    # notify. What is stored is only the last measurement.
    match_parent: bool = False
    # The region-scoped byte reordering the entry's bytes go through, between
    # container and decompressor. Unlike ``container_id`` this lives on **both
    # FILE and SLICE** entries: a reshape is a property of the region, and a
    # region is either a whole file (a joined ROM pair) or a slice of one (a
    # plane-split range inside a larger ROM) — the slice's bounded window *is*
    # the region its reshape applies to, so no coordinates are invalidated.
    reshape_id: str = NO_RESHAPE
    # The container this file is read and written through, picked by signature
    # when the file is opened and changeable afterwards. FILE and PALETTE: a
    # palette file can be framed too — an authoring tool's palette routinely puts
    # its own metadata after the colours, and read whole that tail decodes as
    # more colours. Which containers either may use is the container's own
    # declaration (``PluginInfo.content_kinds``), so the two lists stay disjoint.
    # Not a slice, though: a slice is a byte range of its *parent* and carries no
    # container of its own — it reads through the parent's coordinates, which is
    # what the parent's container defines. Past a header skip those coordinates
    # are still file offsets and the slice reads the file; where the parent
    # *permutes* them they are not, and the slice reads its buffer instead
    # (:func:`~celpix.project.configs._parent_view_bytes`).
    container_id: str = RAW_CONTAINER
    # SLICE and BOOKMARK entries only: which kind of row the entry was cut from —
    # a FILE, a PALETTE, or (a slice only) another SLICE. A slice of a file is
    # anchored by path, and a ``.pal`` can be open as both a graphics file and a
    # registered palette at once, so the path alone cannot say which of the two
    # a slice was cut from; this does. FILE for every entry any older project
    # holds, which is what they all were (``docs/design/project-format.md``).
    parent_kind: EntryKind = EntryKind.FILE
    # A **nested** slice's parent slice, by identity (``parent_kind`` SLICE), and
    # None on every other entry. Identity rather than the path a slice of a file
    # is found by, because every slice of one file shares that path — the same
    # reason a tile binding and a composite piece hold the entry itself. None on
    # a nested slice is a broken chain (a project naming a parent it does not
    # hold), which opens inert rather than reading the file at an offset that
    # was never a file offset (``Workspace.chain_broken``).
    parent_entry: Entry | None = None
    doc: Document | None = None  # lazy: loaded on first activation
    # Children of this **file or slice** whose current bytes are not in its
    # buffer yet.
    #
    # A slice edit has to reach the file that owns those bytes, and re-encoding it
    # to get there is the expensive half — on a compressed slice it is a search
    # for the tightest packing, which is not a thing to run per stroke. So the
    # edit records the debt here and the fold pays it at the next place the
    # buffer is *believed* (``docs/design/slices-and-parents.md`` §2).
    #
    # Membership rather than a flag because a child that has been undone back to
    # **clean** still owes the parent those bytes: the fold takes dirty children
    # plus these, and "dirty" alone would leave the edited version standing in the
    # buffer after it had been undone in the slice. Identity-keyed, like every
    # other reference to an entry (this class is ``eq=False``).
    #
    # Down a chain every link owes the one below it: an edit to a nested slice
    # puts it here on its parent slice, and the parent slice here on *its*
    # parent, up to the file — so settling any member finds every debt above it.
    pending_folds: set[Entry] = field(default_factory=set)
    # SLICE entries only: why the last fold of this slice into its parent was
    # refused — a re-encoded stream that no longer fits its slot, a compressor
    # with no write half — or None when the parent's buffer holds its bytes.
    #
    # Set where the fold discovers it and read wherever the fold's success is
    # otherwise assumed: a write of the parent marks its dirty slices clean, and
    # an edit to the parent drops their documents, both on the strength of "the
    # buffer already has the slice's edits". A refused fold is the one case
    # where it does not, and without this record the slice was marked saved
    # without a byte of it written, and its document dropped with the edit
    # inside. Session state, never persisted.
    fold_refused: str | None = None
    # Why the last attempt to open this entry failed, or None while it opens (or
    # has not been tried). Set by the one load funnel every activation goes
    # through and read by every surface that shows the entry: the row wears an
    # error mark whose tooltip carries this, and activating the entry makes it
    # current-but-inert instead of trying again — a load that failed on a click
    # fails identically on the next one, and a dialog per click told the user
    # nothing new. Cleared wherever the entry's document is dropped
    # (``Workspace.drop_document``), which is exactly where something about what
    # it reads has changed and another attempt is worth making. Session state,
    # never persisted.
    load_failure: LoadFailure | None = None
    # The summary of the failure last *raised as a dialog* for this entry. A
    # failure is shown once: a retry that fails the same way is silent (the row
    # still says why), one that fails differently is news and shown again, and
    # an entry that opened forgets, so failing later - even identically - is news
    # once more. Kept apart from ``load_failure`` because it has to survive the
    # drops that clear that one; the two together are "what is wrong now" and
    # "what the user has already been told". Session state, never persisted.
    reported_failure: str | None = None
    session: EntrySession | None = None
    # Unsaved in-memory changes, tracked **per pathway** because the two write to
    # different files: the pixel pathway is the entry's own data (its pixel bytes
    # — for a slice, spliced back into the parent file), the palette pathway a
    # separate source (a .pal, or the palette's own region of a ROM). Keeping
    # them apart is what stops a color edit from rewriting the graphic
    # (docs/design/palette-editing.md §2).
    #
    # Each pathway holds a *revision token* rather than a flag: an edit command
    # records a fresh token when it applies and puts the previous one back when
    # it undoes, and a write records the token it saved. "Dirty" is then simply
    # "the live token isn't the saved one", which goes clean again when an undo
    # walks back to the saved state — and stays dirty when it walks back *past*
    # a save point. Tokens rather than a counter because a count can collide
    # (undo one edit, make a different one) and would then report clean wrongly.
    pixel_revision: int = 0
    pixel_saved_revision: int = 0
    palette_revision: int = 0
    palette_saved_revision: int = 0

    # Project-restored display state, held until the lazy document exists and
    # consumed on its first load (the live state then lives on the document).
    pending_view: ViewOptions | None = None
    pending_palette: PaletteSource | None = None
    # Set when an external palette source (file/emulator mode) couldn't be
    # reached on load: the entry renders on the default palette but keeps its
    # palette_mode display, and this holds the source so it can be re-pointed
    # (Locate missing files) and re-saved. None when the palette is healthy or
    # still unloaded (an unloaded source lives on pending_palette).
    missing_palette: PaletteSource | None = None
    # PALETTE entries only: the palette codec the file was imported with — and,
    # the same fact seen from the other side, the colour format its swatch view
    # reads the bytes through (``EntrySession.palette_view_preset_id`` follows
    # it: :func:`~celpix.project.configs.interpret_params_for`).
    palette_preset_id: str | None = None
    # ENTRY palette mode only: the open entry whose resolved bytes this entry's
    # colours are decoded from, held by identity like :attr:`TileSource.entry`
    # (``docs/design/palette-editing.md``). The live half of
    # :attr:`PaletteSource.entry`, which is what a restore and a save speak in —
    # the document's palette config carries the offset and the bytes but has
    # nowhere to put an object, and a config is plain pipeline data that must
    # not learn what an entry is.
    #
    # A binding whose source has been **closed** still holds it and answers "not
    # open", exactly as a tile binding does, which is what makes undoing the
    # close restore the colours for free.
    palette_entry: Entry | None = None

    # What the entry's bytes *are*, independent of how the entry is bounded
    # (`docs/design/tilemap-entry.md` §2). ``kind`` above answers a different
    # question — whole file, slice, bookmark — and conflating the two left
    # nowhere to put an ordinary thing like a tilemap that happens to be a slice
    # of a ROM. Defaults to PIXELS, and is omitted from a written project when
    # it still is, so every project predating tilemaps loads unchanged.
    #
    # A slice or bookmark **inherits its parent's**: a window into a tilemap file
    # is a tilemap (:func:`slice_of`). A **palette file holds pixels** too — its
    # colour words read as swatches through the palette-swatch codec, exactly
    # as a composite of a ROM's colour tables does — so PALETTE entries stay on
    # the default; :attr:`kind` is what files them with the palettes
    # (:func:`~celpix.project.workspace.section_kind`).
    content_kind: ContentKind = ContentKind.PIXELS
    # TILEMAP entries only: where the tiles this map indexes into come from, and
    # which codec reads its cells. Both None/empty until bound — a tilemap opens
    # and renders as placeholders rather than refusing, since the binding is
    # project state that no file states (`docs/design/tilemap-entry.md` §3).
    tile_source: TileSource | None = None
    tilemap_preset_id: str | None = None
    # The next four are **PIXELS entries**, unlike everything around them: the
    # **font alphabet**, which says what this entry's tiles spell for a
    # **fontmap** drawn through them (``docs/design/fontmap-entry.md`` §3). It
    # sits on the font and not on the map that reads it because that is whose
    # fact it is — the tile ⇄ letter mapping is decided when the sheet is drawn,
    # so every string in the game that uses the sheet is bound by it, and ten
    # maps restating it would be ten copies to keep in step. A map re-pointed at
    # another font picks up that font's answer with nothing else to change.
    #
    # **Use as Font** on the view bar. The declaration, and the gate: an
    # unticked entry's table is not read at all. Declared rather than inferred
    # from the table being non-empty, for the reason `layout = "text"` on the map
    # is — it has to answer before there is a table to look at, and it is what
    # makes the editor reachable on a sheet nobody has typed a letter into yet.
    use_as_font: bool = False
    # The code tile 0 draws, added to the whole positional run below — the
    # **Base code** spin. Beside the run and not inside it because the two answer
    # different questions: the run states the *shape* of the mapping (which
    # characters, in what order), which is decided by the art and legible the
    # moment the sheet is, while the *origin* is decided by the game's code and
    # appears in neither the sheet nor the string
    # (``docs/graphics-formats-reference/text-formats.md`` §3.2). So it is a
    # thing to dial against the text window, not a thing to guess at.
    font_base: int = 0
    # The positional half: **one code point per tile, in tile order**, so
    # character *i* is code ``font_base + i``. This is the shape a font sheet
    # states about itself, and the editor's tile grid is a picture of it. A
    # :data:`~celpix.core.font.HOLE` is a slot that draws no character — the run
    # keeps its length either way, or every letter after a gap lands on the
    # wrong tile.
    font_chars: str = ""
    # The absolute half: codes named because the game's code says what they are
    # — a line break, a terminator, a command worth a caption — plus any code the
    # run cannot spell, a pair standing behind one code or a glyph outside the
    # sheet. **Not moved by** ``font_base``, since none of them was read off the
    # sheet; they override the run where they collide.
    font_codes: tuple[Glyph, ...] = ()
    # How many rows the alphabet editor lists **before** the first tile and
    # **after** the last — the Prepend and Append spins. A code outside the sheet
    # is an ordinary thing to have to name (a terminator above a 128-tile font, a
    # letter drawn by the sheet uploaded next to this one), and the table is one
    # row per tile, so without these there is no row to type it into.
    #
    # Saved rather than kept on the window because how far past its tiles a font
    # is read is a fact about that font: a sheet whose stream terminates on $FF
    # is read to $FF every time it is opened, and re-dialling it each session is
    # re-answering a question the project already knows the answer to.
    font_prepend: int = 0
    font_append: int = 0
    # The palette row this map's cells count their own row 0 from — the tile
    # base's colour twin, and the user's word on it. **None means the format's
    # own answer**, which is right almost always: a sprite's 3-bit field counts
    # from CGRAM row 8 and the preset says so
    # (:attr:`~celpix.core.document.Document.palette_row_base`). What needs
    # overriding is the palette that is actually loaded — the same object read
    # against a file holding only the sprite half of CGRAM counts from row 0, and
    # against one holding only rows 8-15 as 0-7 it counts *down*, so this is
    # signed like ``TileSource.base_index``. None rather than 0 because 0 is a
    # real answer the user may have to give against a format that says 8.
    palette_row_base: int | None = None
    # A **sprite map**'s two subsprite sizes, as multiples of the tile size — the
    # pair each size bit chooses between (:data:`~celpix.core.sprite.
    # DEFAULT_SUBSPRITE_TILES`). **None means the format's own answer.** Unlike the
    # two bases this is not a correction to a guess: the pair was a *register* the game
    # set per scene, so no file records it and a preset can only name the commonest.
    # An object authored against another pair draws every subsprite at the wrong size
    # until this says so, which is why it is the user's and why it is per entry.
    sprite_size_pair: tuple[int, int] | None = None
    # What this entry binds to the inputs its plugins declare
    # (:class:`~celpix.plugins.base.InputSpec`): ``plugin id -> input key ->``
    # :data:`InputBinding`. Keyed by the **plugin** rather than the stage because
    # the binding belongs to the plugin that declared the key — two codecs may
    # both call an input ``table`` and mean different bytes — and so that
    # switching a picker away and back loses nothing. On a **slice** or a
    # tilemap entry these feed the codecs that read it; on a **file** entry they
    # feed the compression *preview* run over it, and are copied onto every
    # slice carved under that codec (:func:`slice_of`). Bindings for a plugin
    # the entry does not currently name are inert, and pruned only on save
    # (:func:`~celpix.project.inputs.prune_bindings`). Treated as immutable:
    # an edit replaces the whole mapping, which is what lets an undo command
    # hold the before and after by reference
    # (:func:`~celpix.project.inputs.with_bindings`).
    inputs: dict[str, dict[str, InputBinding]] = field(default_factory=dict)
    # The project reader's scratch: which of those bindings named an entry by
    # position, ``(plugin id, key, position)``, until every entry exists and the
    # positions can become objects (``projectfile._bind_inputs``). Empty on any
    # entry the reader has finished with, and on every entry it never touched.
    _pending_input_sources: tuple[tuple[str, str, int], ...] = ()
    # COMPOSITE entries only: the runs this entry's bytes are assembled from, in
    # order (:class:`CompositePiece`). Empty is a legal state and renders as an
    # empty document — a composite is created before it is filled in, and a
    # dialog that refused to close on an empty list would be the only way to lose
    # the entry you had just named.
    pieces: tuple[CompositePiece, ...] = ()
    # Where each of those pieces landed in the buffer the last assembly produced
    # (:class:`PieceSpan`) — set by
    # :func:`~celpix.project.composites.record_composite_layout` and dropped with the
    # document, because it describes *that* buffer and nothing else.
    #
    # Session state, never persisted: it is derivable from the pieces and is
    # derived again on every load. Held rather than recomputed because the
    # question it answers — whose byte is this? — is asked per stroke, and
    # rebuilding it would mean re-reading every source to find out where a click
    # landed (``docs/design/composite-entry.md``).
    piece_spans: tuple[PieceSpan, ...] = ()

    @property
    def file_stages(self) -> FileStages:
        """The byte stages this file is read through, as one value
        (:class:`FileStages`) — what Edit File Container… opens on and what an
        undo of it puts back. Meaningful for a FILE or PALETTE; a slice carries
        its parent's container and its own reshape and compression."""
        return FileStages(self.container_id, self.reshape_id, self.compression_id)

    def set_file_stages(self, stages: FileStages) -> None:
        """Put ``stages`` on this entry — the three fields at once, since any one
        alone leaves the entry re-read (:class:`FileStages`)."""
        self.container_id = stages.container_id
        self.reshape_id = stages.reshape_id
        self.compression_id = stages.compression_id

    @property
    def paths(self) -> tuple[str, ...]:
        """Every file this entry's bytes come from, in the order they join.

        **Empty for a composite**, which comes from no file: its bytes are other
        entries', and each of those is a row in the same list answering for its
        own. That emptiness is load-bearing rather than incidental — it is what
        keeps a composite out of the missing-file scan, the relocate pass and the
        post-save cache invalidation, none of which have anything to say to an
        entry with no file (``docs/design/composite-entry.md``). Its ``path`` is
        ``""``, so answering from it would name the working directory.
        """
        if self.kind is EntryKind.COMPOSITE:
            return ()
        return (self.path, *self.extra_paths)

    @property
    def pixel_dirty(self) -> bool:
        """Unsaved changes to the entry's own data (its pixel bytes)."""
        return self.pixel_revision != self.pixel_saved_revision

    @property
    def palette_dirty(self) -> bool:
        """Unsaved changes on the entry's palette pathway."""
        return self.palette_revision != self.palette_saved_revision

    @property
    def is_font_sheet(self) -> bool:
        """Whether this entry's tiles are letters — the tick *and* the kind.

        **Only a sheet of pixels can be a font**, which is why every reader asks
        this rather than :attr:`use_as_font` directly. A map has no tiles of its
        own to spell: its cells name another entry's, so a fontmap answering yes
        would offer itself as the font for a second string and read that string
        through its own table. The tick is offered on a pixels entry alone, so a
        map carrying it can only be stale — an older project, or a hand-edited
        file — and this is what keeps that inert rather than load-bearing.
        """
        return self.use_as_font and self.content_kind is ContentKind.PIXELS

    def can(self, capability: Capability) -> bool:
        """Whether this entry supports ``capability`` — the gate on its controls.

        A thin pass to :func:`~celpix.core.capabilities.supports` so a caller
        asks the entry rather than reaching through it for its content kind.
        """
        return supports(self.content_kind, capability)


def new_slice(
    parent_path: str,
    name: str,
    offset: int,
    length: int | None = None,
    compression_id: str = NO_COMPRESSION,
    extra_paths: tuple[str, ...] = (),
    reshape_id: str = NO_RESHAPE,
    parent_kind: EntryKind = EntryKind.FILE,
) -> Entry:
    """A SLICE entry over ``parent_path`` — not yet in any workspace.

    Building and *adding* are separate because the UI's adds are undoable: an
    ``AddEntryCommand`` needs the entry to exist before it is pushed, and the
    command owns the insertion (so undo/redo re-add the very same object). This
    is the one statement of what a slice entry is, shared by that path and by
    :meth:`~celpix.project.workspace.Workspace.add_slice`.

    ``parent_path`` is a whole *file* — a FILE or a PALETTE (``parent_kind``) —
    and it becomes the entry's ``path``: a slice is named by the file it cuts
    into, not by one of its own. A slice of another slice is named by the parent
    *entry*, which a path cannot say, so it is built by :func:`slice_of` alone.
    ``offset`` is likewise absolute in that file, and ``length`` may be ``None``
    for a compressed slice whose extent is discovered on first load.

    ``extra_paths`` is the rest of the parent's file list when its region spans
    several ROM chips (:attr:`Entry.extra_paths`). Offsets into a joined region
    only mean anything against the same join, so a slice of one carries the
    parent's whole list — :func:`slice_of` is the way to get that right.
    """
    return Entry(
        name=name,
        kind=EntryKind.SLICE,
        path=parent_path,
        extra_paths=extra_paths,
        slice_offset=offset,
        slice_length=length,
        compression_id=compression_id,
        reshape_id=reshape_id,
        parent_kind=parent_kind,
    )


def new_composite(name: str, pieces: tuple[CompositePiece, ...] = ()) -> Entry:
    """A COMPOSITE entry — not yet in any workspace.

    Built and *added* separately for the reason :func:`new_slice` is: the UI's
    adds are undoable, so an ``AddEntryCommand`` needs the entry to exist before
    it is pushed and owns the insertion, which is what lets undo and redo put the
    very same object back — and every piece of every other composite, and every
    tilemap binding, goes on naming it.

    ``path`` is ``""`` and stays that way: a composite is assembled out of other
    entries and has no file of its own (:attr:`Entry.paths`).
    """
    return Entry(name=name, kind=EntryKind.COMPOSITE, path="", pieces=tuple(pieces))


def slice_of(
    parent: Entry,
    name: str,
    offset: int,
    length: int | None = None,
    compression_id: str = NO_COMPRESSION,
    reshape_id: str = NO_RESHAPE,
) -> Entry:
    """A SLICE entry over an open ``parent`` — :func:`new_slice` given the entry.

    The form to reach for whenever the parent entry is in hand, because it is the
    one that cannot get the file list wrong: it takes the parent's paths whole
    rather than leaving the caller to remember that a region may be several
    files. :func:`new_slice` stays for callers holding only a path.

    It also carries the parent's :class:`ContentKind` down, which
    :func:`new_slice` cannot: a window into a tilemap file is a tilemap, and only
    the entry knows what its file holds.

    And it carries the parent's **input bindings** for ``compression_id`` down
    (:attr:`Entry.inputs`): a file binds the code table its compression preview
    decodes with, and the slice promoted or carved out of that preview has to be
    read with the same table or it opens degraded. A **copy**, not a live link —
    one file may keep two tables for one codec, so "the parent's binding" is
    not one thing to inherit from — and the ``None``-sourced shape means "this
    file" on the slice exactly as it did on the parent
    (:class:`~celpix.project.inputs.RegionBinding`).

    ``parent`` may itself be a **slice**: the result is then a nested slice, its
    ``offset`` counted from byte 0 of the parent's decoded buffer and the parent
    held by identity (:attr:`Entry.parent_entry`). The file list is still the
    parent's, which is the root file's, since that is where its bytes end up.
    """
    entry = new_slice(
        parent.path,
        name,
        offset,
        length,
        compression_id,
        parent.extra_paths,
        reshape_id,
        parent_kind=parent.kind,
    )
    if parent.kind is EntryKind.SLICE:
        entry.parent_entry = parent
    entry.content_kind = parent.content_kind
    inherited = parent.inputs.get(compression_id)
    if inherited:
        entry.inputs = {compression_id: dict(inherited)}
    return entry


def slice_links(entry: Entry) -> tuple[list[Entry], bool]:
    """The slice parents above ``entry``, nearest first, and whether the chain
    ends intact — at an entry that is not itself cut from a slice.

    Walked through :attr:`Entry.parent_entry` alone, with no workspace, so it
    answers for rows no list holds yet. It stops, not intact, at a missing parent
    or at one it has already passed, so a circular chain is walked once.
    """
    links: list[Entry] = []
    seen = {id(entry)}
    node = entry
    while node.parent_kind is EntryKind.SLICE:
        parent = node.parent_entry
        if parent is None or id(parent) in seen:
            return links, False
        seen.add(id(parent))
        links.append(parent)
        node = parent
    return links, True


def anchor_kind(entry: Entry) -> EntryKind:
    """Which kind of whole-file row a slice's or bookmark's chain is cut from.

    :attr:`Entry.parent_kind` for a child of a file, and the same answer for a
    nested slice, found by walking its parents to the one cut from a file — a
    run of colours carved out of a slice of a ``.pal`` is still a slice of a
    palette. Walked through the entries themselves rather than a workspace,
    since the question is asked of rows before any list holds them. A chain
    that breaks on the way up answers FILE, what an unstated parent means.
    """
    links, intact = slice_links(entry)
    if not intact:
        return EntryKind.FILE
    return (links[-1] if links else entry).parent_kind


# -- composites (docs/design/composite-entry.md) ---------------------------
@dataclass(frozen=True)
class PieceSpan:
    """Where one run of a :class:`CompositePiece` landed in the assembled buffer.

    ``owner`` is the entry those bytes belong to — the piece's, and **None** for
    a pad or for a source that could not be read. That is the whole of what the
    deposit path needs: a byte position in a composite either has an owner to be
    written into or it has nothing behind it, and an edit there is refused rather
    than kept in a buffer no file answers for
    (``docs/design/slices-and-parents.md``).

    A *run* rather than a piece, because a piece can land as two: the bytes its
    source held, and the zero fill after them where a stated range outran the
    source or a ragged one was rounded up to a tile. The fill is a span of its
    own with no owner, since the owner has no byte at those positions — a
    deposit there would be dropped at the end of its buffer while the stroke
    stayed on screen, in the undo stack, and vanished at the next reassembly.

    ``source_base`` is where this run started **inside the owner's own buffer**,
    which is 0 for a whole-entry piece and the range's own ``offset`` for a ranged
    one (:attr:`CompositePiece.is_ranged`). An edit at composite position *p*
    lands at ``source_base + (p - start)`` in the owner, so this is the one number
    that keeps a deposit honest when only part of a source is on screen.
    """

    start: int
    length: int
    owner: Entry | None
    source_base: int = 0

    @property
    def end(self) -> int:
        """One past the last byte — the half-open bound the overlap test uses."""
        return self.start + self.length


@dataclass(frozen=True)
class CompositeLayout:
    """An assembled composite: its bytes, what they came from, and what went wrong.

    ``problems`` are per-piece complaints in list order — a source that is closed,
    unreadable or itself a composite, or one whose bytes did not fill a whole
    number of tiles. They are returned rather than raised because none of them
    stops the composite being shown: the run becomes blank tiles and everything
    after it stays where it was, which is the only behaviour a map bound to it
    can survive. The load path puts them on the document's context as notices
    (:meth:`~celpix.ui.main_window.session.SessionMixin._load_entry`).
    """

    data: bytes
    spans: tuple[PieceSpan, ...]
    pieces: tuple[CompositePiece, ...]
    problems: tuple[str, ...] = ()
