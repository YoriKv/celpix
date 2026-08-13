"""The session's open-entries collection: files, slices, bookmarks, palettes.

A :class:`Workspace` is the model behind the UI's open-files list. It holds an
ordered list of :class:`Entry` — a whole **file**, a **slice** (an
offset+length region of a parent file, optionally decompressed, that acts as
its own document), a **bookmark** (an offset into a parent file plus a
snapshot of settings, with no document or view of its own), or a **palette**
(an external palette file, remembering the codec it was imported with) — plus
a *current* pointer for the single active view. Bookmarks and palettes are
never current: a bookmark is jumped *through*, reconfiguring its parent, and a
palette is *applied* to whichever entry is on screen, not activated.
It is session-lifetime only; persisting it is :mod:`celpix.project.projectfile`'s
job (``docs/design/project-format.md``).

The workspace is Qt-free. The UI subscribes to the plain callback lists
(``on_added`` …) to mirror changes into its list widget; nothing here knows
about widgets, documents' rendering, or the pipeline's execution — an entry
only *carries* its lazily loaded :class:`~celpix.core.document.Document` and
the config factory (:func:`pixel_config_for`) that tells the pipeline how to
read it.

**Slices reference their parent by path**, and **one region has one authority**:
the parent owns its bytes and a slice is a derived view of a window of them
(``docs/design/slices-and-parents.md``). Reading is an ordinary bounded
:class:`~celpix.plugins.base.FileRef` served by the ordinary container — from the
file on disk, *except* where the parent's own buffer is the only truth (it holds
unsaved pixel edits, or it reorders), when :func:`pixel_config_for` points the
source at that buffer instead (``FileRef.data``). Writing never deposits at those
bounds: the pathway is flagged ``writes_through_parent`` and the host folds the
slice into the parent's buffer and writes the *parent*, so the parent's container
runs over bytes that changed inside it.

Cached documents of other entries on the same path go stale only when one of them
saves — :meth:`Workspace.invalidate_path` drops those caches (except dirty ones:
an invalidation must never discard in-memory changes) so they reload fresh on next
activation. External changes to the file on disk are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from os.path import abspath, basename, exists, normcase, splitext
from typing import Callable

from celpix.core import ceil_div
from celpix.core.address import format_hex
from celpix.core.capabilities import Capability, ContentKind, supports
from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_SOURCE_OFFSET,
    PipelineContext,
)
from celpix.core.document import Document, ViewOptions
from celpix.core.errors import PipelineError, Stage
from celpix.core.font import Glyph
from celpix.core.notices import Notice, notices
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import DEFAULT_SLOT_FILL, PathwayConfig, SlotFill
from celpix.plugins.base import (
    NO_COMPRESSION,
    NO_RESHAPE,
    RAW_CONTAINER,
    STAGE_DEFAULT_PRESET,
    FileRef,
)
from celpix.plugins.detect import resolved_container_id
from celpix.plugins.registry import Registry

# The digit runs :func:`_natural_key` compares as numbers. Captured, so the split
# keeps them and the key sees the whole name rather than only its text.
_DIGIT_RUN = re.compile(r"(\d+)")


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

        The question a dozen sites were asking as the literal pair
        ``(FILE, SLICE)``, which is exactly the kind of set that falls out of
        step when a fifth member arrives — and did: a composite is a third thing
        with a document, a view, a palette and an undo history, and every one of
        those sites meant "one of those" rather than "a file or a slice".

        A bookmark is a position and a palette is applied rather than activated,
        so neither is ever current and neither has a view of its own.
        """
        return self in (EntryKind.FILE, EntryKind.SLICE, EntryKind.COMPOSITE)


class SortKey(str, Enum):
    """What a group of rows is put in order by — see :func:`sorted_entries`.

    The list's order is the user's (:meth:`Workspace.reorder`), so this is a
    gesture they ask for rather than a state anything keeps: nothing persists it,
    and the next hand-placed row is not moved back.
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
        return self in (PaletteMode.FILE, PaletteMode.OFFSET, PaletteMode.EMULATOR)

    @property
    def decodes_raw_bytes(self) -> bool:
        """Whether the palette is decoded from raw bytes through a color codec,
        so the format picker can *reinterpret* those bytes.

        FILE and OFFSET read a file; an EMULATOR state's console dictates the
        initial codec, but the picker still lets the user override how its bytes
        are read. DEFAULT and CUSTOM carry their own colors (generated, or ARGB
        stored in the project), so no codec choice applies — CUSTOM shows the
        format it carries, but read-only.

        Coincides with :attr:`has_source` — anything with bytes to re-read has
        bytes to reinterpret — and is defined from it so the two cannot drift.
        """
        return self.has_source

    @property
    def has_external_file(self) -> bool:
        """Whether the colors come from a file of their own, whose name the
        palette dock shows and whose loss degrades the entry."""
        return self in (PaletteMode.FILE, PaletteMode.EMULATOR)

    @property
    def holds_edits(self) -> bool:
        """Whether a color edit can land on this palette as it stands.

        The generated default has nowhere to store one, and an emulator state is
        never written back — so an edit on either forks to Custom first. Named
        here with the other mode questions rather than as a literal mode set at
        each editing entry point.
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
    (+ ``offset`` into it), or just an ``offset`` into the
    entry's own pixel file. A live entry keeps this information on its
    document's palette config; this form exists for entries whose document
    isn't loaded yet (project restore) and is consumed on first activation.
    """

    colors: list[int] | None = None
    path: str | None = None
    offset: int = 0


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
    (:meth:`~celpix.ui.main_window.session.SessionMixin._binding_target`). That is
    also what makes closing a tile bank undoable for free: the restore puts the
    same object back, and every map bound to it is bound to it again.

    ``base_index`` shifts every cell: cell index N draws source tile
    ``base_index + N``. It is what lets a map and its art be bound together when
    the two number their tiles from different places, without rewriting either.

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
    """

    mode: TileMode = TileMode.NONE
    entry: Entry | None = None
    base_index: int = 0

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


@dataclass
class EntrySession:
    """Per-entry snapshot of the UI session — what entry-switching restores.

    Plain data (project-file material). Only the state that is *not*
    already carried by the entry's :class:`Document` lives here: view geometry,
    offset/nudge and subpalette are in ``Document.view``, and the palette
    itself plus both pathway configs are on the document.
    """

    pixel_preset_id: str
    palette_preset_id: str
    palette_mode: PaletteMode = PaletteMode.DEFAULT
    # The decompression-preview combo's position, which is a *view* setting: it
    # says what the toolbar was showing, not how the entry's bytes are read.
    # Entry.compression_id is that, and the two move independently.
    preview_compression_id: str = NO_COMPRESSION
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


@dataclass(eq=False)  # identity semantics: two slices may share coordinates
class Entry:
    """One open item: a whole file, an offset+length slice of one, or a bookmark.

    ``path`` is the file itself for FILE entries and the **parent** file for
    SLICE and BOOKMARK entries. **Slices and bookmarks never nest**: both are
    always anchored to a whole file, never to another slice, so their ``path``
    always names a FILE and the open-entries list is exactly two levels deep.
    ``slice_offset`` is an absolute offset from byte 0 of the file —
    deliberately not header-relative, so a slice or bookmark never shifts when
    the parent's header-skip display setting changes. ``slice_length`` may
    start ``None`` for a decompressed slice ("to be discovered"): the first
    load backfills it from the structure's true extent so save-back is
    slot-bounded.

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
    dropdown has moved for some other palette since. Like a bookmark
    it has no document or view and is never current — it is applied *onto*
    the entry being shown.
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
    # answer that on its own, without a workspace to look its parent up in.
    extra_paths: tuple[str, ...] = ()
    slice_offset: int = 0
    slice_length: int | None = None
    compression_id: str = NO_COMPRESSION
    # What fills the room a re-compressed blob leaves at the end of this slice's
    # slot when it packs tighter than the one it replaces
    # (:class:`~celpix.pipeline.pathway.SlotFill`). A **slice** setting because
    # only a bounded region has a tail to decide about, and only its owner knows
    # whether those spare bytes are really its own — the slice dialog asks, and
    # only where a compression scheme is chosen, since nothing else can shrink.
    slot_fill: SlotFill = DEFAULT_SLOT_FILL
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
    # (:func:`_parent_view_bytes`).
    container_id: str = RAW_CONTAINER
    doc: Document | None = None  # lazy: loaded on first activation
    # Children of this **file** whose current bytes are not in its buffer yet.
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
    pending_folds: set[Entry] = field(default_factory=set)
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
    # PALETTE entries only: the palette codec the file was imported with.
    palette_preset_id: str | None = None

    # What the entry's bytes *are*, independent of how the entry is bounded
    # (`docs/design/tilemap-entry.md` §2). ``kind`` above answers a different
    # question — whole file, slice, bookmark — and conflating the two left
    # nowhere to put an ordinary thing like a tilemap that happens to be a slice
    # of a ROM. Defaults to PIXELS, and is omitted from a written project when
    # it still is, so every project predating tilemaps loads unchanged.
    #
    # A slice or bookmark **inherits its parent's**: a window into a tilemap file
    # is a tilemap (:func:`slice_of`).
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
    # COMPOSITE entries only: the runs this entry's bytes are assembled from, in
    # order (:class:`CompositePiece`). Empty is a legal state and renders as an
    # empty document — a composite is created before it is filled in, and a
    # dialog that refused to close on an empty list would be the only way to lose
    # the entry you had just named.
    pieces: tuple[CompositePiece, ...] = ()
    # Where each of those pieces landed in the buffer the last assembly produced
    # (:class:`PieceSpan`) — set by :func:`record_composite_layout` and dropped
    # with the document, because it describes *that* buffer and nothing else.
    #
    # Session state, never persisted: it is derivable from the pieces and is
    # derived again on every load. Held rather than recomputed because the
    # question it answers — whose byte is this? — is asked per stroke, and
    # rebuilding it would mean re-reading every source to find out where a click
    # landed (``docs/design/composite-entry.md``).
    piece_spans: tuple[PieceSpan, ...] = ()

    def __post_init__(self) -> None:
        # A palette entry's content kind is not a separate choice — its ``kind``
        # already says what it holds. Derived here so no construction site can
        # forget it and the project file need not carry it.
        if self.kind is EntryKind.PALETTE:
            self.content_kind = ContentKind.PALETTE

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
) -> Entry:
    """A SLICE entry over ``parent_path`` — not yet in any workspace.

    Building and *adding* are separate because the UI's adds are undoable: an
    ``AddEntryCommand`` needs the entry to exist before it is pushed, and the
    command owns the insertion (so undo/redo re-add the very same object). This
    is the one statement of what a slice entry is, shared by that path and by
    :meth:`Workspace.add_slice`.

    ``parent_path`` is always a whole *file* — slices never nest, so a slice's
    parent is a FILE, never another slice — and it becomes the entry's ``path``:
    a slice is named by the file it cuts into, not by one of its own.
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
    """
    entry = new_slice(
        parent.path,
        name,
        offset,
        length,
        compression_id,
        parent.extra_paths,
        reshape_id,
    )
    entry.content_kind = parent.content_kind
    return entry


class Workspace:
    """The ordered open-entries list + current pointer, with change callbacks."""

    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self.current: Entry | None = None
        # Project-level (not per-entry) settings the project file persists. The
        # pixel-format filter is view-only — which codecs the Pixel dropdown
        # lists — so it rides on the workspace root rather than any one entry.
        self.hidden_pixel_presets: set[str] = set()
        self.on_added: list[Callable[[Entry], None]] = []
        self.on_removed: list[Callable[[Entry], None]] = []
        # Fired instead of per-entry removals when the whole list is swapped —
        # see :meth:`replace`. A listener that mirrors the list drops everything
        # it holds and rebuilds from the additions that follow.
        self.on_reset: list[Callable[[], None]] = []
        self.on_current_changed: list[Callable[[Entry | None], None]] = []
        self.on_dirty_changed: list[Callable[[Entry], None]] = []
        self._revision = 0  # allocator for per-entry revision tokens

    # -- lookups -----------------------------------------------------------
    @staticmethod
    def path_key(path: str) -> str:
        """``path`` reduced to what makes two references the same file.

        The project lives on a Windows drive but is used from both OSes, so path
        identity must survive case differences on the same file. Public because
        the same question is asked of lists this workspace does not hold — a
        paste works out where every row lands before it changes anything, and
        has to ask it against the list it is building.
        """
        return normcase(abspath(path))

    def _find(self, kind: EntryKind, path: str) -> Entry | None:
        key = self.path_key(path)
        for entry in self.entries:
            if entry.kind is kind and self.path_key(entry.path) == key:
                return entry
        return None

    def find_file(self, path: str) -> Entry | None:
        """The FILE entry for ``path``, if one is open (slices never match)."""
        return self._find(EntryKind.FILE, path)

    def find_palette(self, path: str) -> Entry | None:
        """The PALETTE entry for ``path``, if one is registered — same
        path-is-identity rule as :meth:`find_file`, per kind."""
        return self._find(EntryKind.PALETTE, path)

    def palette_render_targets(self, path: str) -> list[Entry]:
        """Loaded FILE/SLICE entries whose document currently renders ``path``.

        Matched on the live palette config, not the saved session mode, so it is
        reliable *mid-switch* — the graphics to re-mirror the instant a file
        palette's colors change. Only entries with a document are returned; an
        unloaded one re-mirrors on its next load.
        """
        key = self.path_key(path)
        out = []
        for entry in self.entries:
            if not entry.kind.has_document or entry.doc is None:
                continue
            src = entry.doc.palette_config.source.path
            if src and self.path_key(src) == key:
                out.append(entry)
        return out

    def add_palette(
        self, path: str, preset_id: str | None, container_id: str = RAW_CONTAINER
    ) -> Entry:
        """Append a PALETTE entry for ``path`` (or return the one already there).

        The **non-undoable** registration, like :meth:`open_file` — used by the
        restore/self-heal path when a graphic references a ``.pal`` the project
        never registered. Interactive registration goes through an
        ``AddEntryCommand`` so it can be undone.

        ``container_id`` is detected by the caller, which holds the registry;
        plain bytes is the answer for the ``.pal`` this path usually gets and the
        one that leaves the entry reading its whole file.
        """
        existing = self.find_palette(path)
        if existing is not None:
            return existing
        entry = Entry(
            name=basename(path),
            kind=EntryKind.PALETTE,
            path=path,
            container_id=container_id,
            palette_preset_id=preset_id,
        )
        self.entries.append(entry)
        self._notify(self.on_added, entry)
        return entry

    def palette_consumers(self, palette: Entry) -> list[Entry]:
        """The graphics entries whose File-mode palette *is* this PALETTE file.

        The reverse of the file → palette reference: a File-mode graphic records
        its palette by path (its live config, or its pending source before load),
        so a shared ``.pal`` is matched by path-identity here — loaded or not.
        This is what lets removing a palette find, and re-home, every graphic that
        renders through it. Empty for anything but a PALETTE entry.
        """
        if palette.kind is not EntryKind.PALETTE:
            return []
        key = self.path_key(palette.path)
        users = []
        for entry in self.entries:
            if not entry.kind.has_document:
                continue
            session = entry.session
            if session is None or session.palette_mode is not PaletteMode.FILE:
                continue
            path = entry_palette_path(entry)
            if path is not None and self.path_key(path) == key:
                users.append(entry)
        return users

    def slices_of(self, entry: Entry) -> list[Entry]:
        """The SLICE entries carved from ``entry``'s file, in list order.

        Only a FILE has slices — slices never nest — so this is a single hop,
        never recursive.
        """
        return [e for e in self.children_of(entry) if e.kind is EntryKind.SLICE]

    def children_of(self, entry: Entry) -> list[Entry]:
        """The SLICE and BOOKMARK entries anchored to ``entry``'s file, in
        list order (empty unless ``entry`` is a FILE — children never nest).
        A PALETTE entry sharing the path is not a child: its path names the
        palette file itself, not a parent."""
        return self._children_in(entry, self.entries)

    def _children_in(self, entry: Entry, entries: list[Entry]) -> list[Entry]:
        """:meth:`children_of` against an arbitrary list — what :meth:`reorder`
        asks of the list it is *about* to commit, where the live one still holds
        the group it has lifted out."""
        if entry.kind is not EntryKind.FILE:
            return []
        key = self.path_key(entry.path)
        return [
            e
            for e in entries
            if e.kind in (EntryKind.SLICE, EntryKind.BOOKMARK)
            and self.path_key(e.path) == key
        ]

    def parent_of(self, entry: Entry) -> Entry | None:
        """The open FILE entry a SLICE or BOOKMARK is anchored to.

        None for the three kinds that anchor to nothing: a FILE and a PALETTE,
        whose path is their own file, and a **COMPOSITE**, which has no path at
        all. That last one is not merely tidy — a composite's ``path`` is ``""``,
        which :meth:`path_key` resolves to the working *directory*, so asking
        would be looking a file up by a name no file has.
        """
        if entry.kind in (EntryKind.FILE, EntryKind.PALETTE, EntryKind.COMPOSITE):
            return None
        return self.find_file(entry.path)

    def dirty_entries(self) -> list[Entry]:
        """Every entry with anything unsaved, on either pathway.

        Callers that ask "is there unsaved work?" — the close/replace prompts,
        Write All — mean both kinds; which files a write then touches is
        :meth:`Entry.pixel_dirty`/:attr:`Entry.palette_dirty`'s job, not this one's.
        """
        return [e for e in self.entries if e.pixel_dirty or e.palette_dirty]

    # -- mutations ---------------------------------------------------------
    def open_file(self, path: str, extra_paths: tuple[str, ...] = ()) -> Entry:
        """Add a FILE entry for ``path`` — or return the one already open.

        Identity is the (normalized) path: a document *is* its file, so opening
        it twice yields the same entry rather than a duplicate.

        ``extra_paths`` opens several files as **one** region, joined in the
        order given (an arcade board's graphics ROMs). ``path`` is still the
        entry's identity, so the already-open check is on it alone: reopening
        the first chip returns the region that was built from it rather than
        starting a second, competing document over the same bytes.
        """
        existing = self.find_file(path)
        if existing is not None:
            return existing
        entry = Entry(
            name=basename(path),
            kind=EntryKind.FILE,
            path=path,
            extra_paths=tuple(extra_paths),
        )
        self.entries.append(entry)
        self._notify(self.on_added, entry)
        return entry

    def add_slice(
        self,
        parent_path: str,
        name: str,
        offset: int,
        length: int | None,
        compression_id: str = NO_COMPRESSION,
        reshape_id: str = NO_RESHAPE,
    ) -> Entry:
        """Build a slice of ``parent_path`` and append it directly.

        The **non-undoable** add, like :meth:`open_file`. The UI does not take
        this path for slices the user creates: those go through an
        ``AddEntryCommand`` so the new entry can be undone, which means the
        command has to own the insertion (see :func:`new_slice`, which builds the
        entry the command then adds). This stays for callers with no undo stack
        to answer to — scripting the model directly, and the tests.

        The parent is looked up so the slice inherits its file list (see
        :func:`slice_of`); a parent that isn't open contributes only its path,
        which is right, since a closed one is a single file as far as anything
        here knows.
        """
        parent = self.find_file(parent_path)
        entry = (
            slice_of(parent, name, offset, length, compression_id, reshape_id)
            if parent is not None
            else new_slice(
                parent_path, name, offset, length, compression_id, reshape_id=reshape_id
            )
        )
        # Placed by the same rule the undoable path uses — offset order among the
        # parent's children. Undo is the only thing the two adds differ in, and
        # letting them differ in *position* too would mean a slice landing
        # somewhere else depending on which of them made it.
        self.insert(entry, self.add_index_for(entry))
        return entry

    def insert(self, entry: Entry, index: int) -> None:
        """Insert an already-constructed entry at ``index`` (undo/redo path:
        re-adding restores the *same* Entry object, so its document, session
        and any commands referencing it stay valid)."""
        self.entries.insert(index, entry)
        self._notify(self.on_added, entry)

    def add_index_for(self, entry: Entry) -> int:
        """Where a newly created ``entry`` belongs in the list.

        The end, except a **slice or bookmark of an open file**, which lands in
        offset order among that file's children. That seeding is the only thing
        the offsets decide: from then on the order is the user's, so dragging a
        row moves it and an offset edit leaves it where it sits
        (:meth:`reorder`). Seeding rather than sorting is what lets both be true
        — a list nobody has arranged still reads low-to-high, which is the order
        slices are usually carved in.

        A child whose parent isn't open has no group to sort within, so it goes
        to the end like anything else.
        """
        parent = self.parent_of(entry)
        if parent is None or entry.kind not in (EntryKind.SLICE, EntryKind.BOOKMARK):
            return len(self.entries)
        siblings = self.children_of(parent)
        later = next((e for e in siblings if e.slice_offset > entry.slice_offset), None)
        if later is not None:
            return self.entries.index(later)
        # Past the last child, or straight after the parent when it has none —
        # never merely "at the parent's index + 1", which would bury a new slice
        # under the ones already there.
        return self.entries.index(siblings[-1] if siblings else parent) + 1

    def reorder(self, entry: Entry, before: Entry | None) -> bool:
        """Move ``entry`` so it sits immediately in front of ``before``.

        The single reordering primitive, behind both the drag gesture and
        Shift+Up/Down: every kind's order is the user's, and one operation says
        so for all of them. ``before`` is the entry the moved row lands in front
        of, ``None`` for last in its own group. False when nothing moved.

        A **file takes its slices and bookmarks with it**. They are matched by
        path rather than position, so the list *could* leave them behind — but a
        parent has to precede its children for the panel to nest them (that is
        the order a project reload replays), so the whole group moves as one, and
        ``before`` names the row the group as a whole goes in front of.

        For a **slice or bookmark** ``None`` means after its last sibling rather
        than at the end of the list: a child dropped last in its parent's group
        is still that parent's child, and letting it drift past unrelated entries
        would only break the contiguity the file move above relies on.

        What ``before`` does *not* have to be is the entry that immediately
        follows in this flat list. The panel groups rows into sections, so two
        rows adjacent on screen may have another section's file between them
        here; inserting in front of ``before`` gets their relative order right
        either way, which is the only thing the display reads.
        """
        group = [entry, *self.children_of(entry)]
        if before is not None and any(before is member for member in group):
            return False
        rest = [e for e in self.entries if not any(e is member for member in group)]
        if before is not None:
            at = rest.index(before)
        elif entry.kind in (EntryKind.SLICE, EntryKind.BOOKMARK):
            parent = self.parent_of(entry)
            if parent is None:
                at = len(rest)
            else:
                siblings = self._children_in(parent, rest)
                at = rest.index(siblings[-1] if siblings else parent) + 1
        else:
            at = len(rest)
        rest[at:at] = group
        if rest == self.entries:  # Entry is eq=False, so this compares identity
            return False
        self.entries[:] = rest
        return True

    def close(self, entry: Entry) -> list[Entry]:
        """Remove ``entry`` — and, for a file, the slices/bookmarks under it.

        A slice or bookmark nested under a closed parent would be an orphan in
        the list, so the parent takes its children with it (the UI confirms
        first). Returns everything removed. If the current entry was among
        them, ``current`` moves to a list neighbour — skipping bookmarks,
        which cannot be current — or None when no candidate remains.
        """
        removed = [entry, *self.children_of(entry)]
        anchor = min(self.entries.index(e) for e in removed)
        for e in removed:
            self.entries.remove(e)
            self._notify(self.on_removed, e)
        if self.current in removed:
            # Bookmarks and palettes can never be current, so the neighbour
            # search skips them.
            after = self.entries[anchor:]
            before = reversed(self.entries[:anchor])
            neighbour = next(
                (e for e in after if e.kind.has_document),
                next((e for e in before if e.kind.has_document), None),
            )
            self.set_current(neighbour)
        return removed

    def replace(self, entries: list[Entry], current: Entry | None) -> None:
        """Swap the whole list for ``entries`` — a loaded project replaces the
        workspace, never merges into it.

        The old list goes as **one** ``on_reset``, not an ``on_removed`` per
        entry: closing a project is a single operation, and reporting it n times
        makes every listener pay its per-removal cost n times over for a list
        that is about to be empty anyway (a tree unwound row by row, a visit
        trail rebuilt per entry, a missing-file scan re-run over the shrinking
        remainder). Additions stay per-entry — the new list *is* built one entry
        at a time. ``current`` is set last, so the activation lands on a
        populated list.
        """
        self.set_current(None)
        self.entries.clear()
        for callback in list(self.on_reset):
            callback()
        self.entries.extend(entries)
        for entry in entries:
            self._notify(self.on_added, entry)
        self.set_current(current)

    def set_current(self, entry: Entry | None) -> None:
        if entry is self.current:
            return
        assert entry is None or entry in self.entries
        # Bookmarks and palettes have no document or view of their own — they
        # can never be shown.
        assert entry is None or entry.kind.has_document
        self.current = entry
        self._notify(self.on_current_changed, entry)

    def next_revision(self) -> int:
        """A fresh revision token, unique across the whole workspace.

        Never reused, so a token identifies one exact state of one pathway:
        that is what lets an undo restore "the state that was saved" rather
        than merely "one edit fewer" (see :class:`Entry`).
        """
        self._revision += 1
        return self._revision

    def set_pixel_revision(self, entry: Entry, revision: int) -> None:
        """Record ``revision`` on the entry's data pathway (an edit applying,
        or an undo putting the previous token back)."""
        self._set_revision(entry, "pixel_revision", revision)

    def set_palette_revision(self, entry: Entry, revision: int) -> None:
        """Record one on the entry's *palette* pathway, leaving its data alone."""
        self._set_revision(entry, "palette_revision", revision)

    def mark_saved(
        self, entry: Entry, *, pixel: bool = True, palette: bool = True
    ) -> None:
        """Record the current revisions as the ones on disk — the entry reads
        clean until it is edited away from them again.

        Also the honest way to drop changes that no longer exist (a slice
        re-pointed at another region discards its document): there is nothing
        unsaved once the edits themselves are gone.
        """
        before = (entry.pixel_dirty, entry.palette_dirty)
        if pixel:
            entry.pixel_saved_revision = entry.pixel_revision
        if palette:
            entry.palette_saved_revision = entry.palette_revision
        if (entry.pixel_dirty, entry.palette_dirty) != before:
            self._notify(self.on_dirty_changed, entry)

    def _set_revision(self, entry: Entry, field: str, revision: int) -> None:
        before = (entry.pixel_dirty, entry.palette_dirty)
        setattr(entry, field, revision)
        if (entry.pixel_dirty, entry.palette_dirty) != before:
            self._notify(self.on_dirty_changed, entry)

    def drop_document(self, entry: Entry) -> None:
        """Discard an entry's cached document, preserving its palette source.

        The palette must survive a document drop because for a **custom**
        palette the document is the *only* place its colors exist — nothing on
        disk backs them. Capturing the source into ``pending_palette`` hands
        them to the reload the same way a project restore does, so re-reading
        the pixel bytes never silently reverts an edited palette to the
        generated default. For the file-backed modes this is simply a
        re-resolution of the reference they already carry.
        """
        source = palette_source_for(entry)
        if source is not None:
            entry.pending_palette = source
        entry.doc = None
        # A composite's spans describe the buffer that just went; keeping them
        # would leave the one question they answer being answered about bytes
        # nothing holds any more (:attr:`Entry.piece_spans`).
        entry.piece_spans = ()

    def invalidate_path(self, path: str, keep: Entry | None = None) -> None:
        """Drop cached documents of entries rooted at ``path`` (after a save).

        ``keep`` — the entry that just saved — retains its cache. Entries with
        unsaved changes on *either* pathway also retain theirs: their document
        holds those changes, and dropping it would silently lose them; they
        simply stay based on the pre-save bytes until written or explicitly
        reloaded.

        A region's later chips count as much as the file it is named after: a
        save that rewrites one of them leaves every other entry reading it —
        including one that only borrows it as its *second* file — holding stale
        bytes.
        """
        key = self.path_key(path)
        for entry in self.entries:
            if entry is keep or entry.pixel_dirty or entry.palette_dirty:
                continue
            if any(self.path_key(p) == key for p in entry.paths):
                self.drop_document(entry)

    @staticmethod
    def _notify(callbacks: list[Callable[[Entry], None]], entry) -> None:
        for callback in list(callbacks):
            callback(entry)


#: What **by type** means, in the order the rows land: the picture first, then
#: the three readings of a map — an even grid, the same cells placed freely, the
#: same cells read as words — and the palettes applied onto all of them last.
#: Keyed by string so one table covers both halves of the question, the content
#: kind's own value and the *layout* a tilemap's cell format declares (the same
#: declaration the row's icon reads).
_TYPE_ORDER: dict[str, int] = {
    ContentKind.PIXELS.value: 0,
    ContentKind.TILEMAP.value: 1,
    "sprite": 2,
    "text": 3,
    ContentKind.PALETTE.value: 4,
}


def sorted_entries(
    entries: list[Entry],
    key: SortKey,
    *,
    layout: Callable[[Entry], str] | None = None,
) -> list[Entry]:
    """``entries`` in ``key`` order — one group of rows, rearranged.

    Sorting acts on a *group* (a file's children, or the files of one section),
    because that is the only span whose order means anything: the list is flat
    here but nested and sectioned on screen, so a sort across the whole of it
    would rearrange rows the user cannot see together.

    By **offset** the name breaks ties, so two bookmarks on one position still
    land in a stable, readable order; by **name** the offset is not consulted at
    all — a group sorted by name and holding two of the same name keeps the order
    it had, Python's sort being stable. Offsets are the child kinds' question
    only: a file and a palette are the whole of their bytes and every one of them
    would answer 0.

    By **type** nothing breaks a tie, deliberately: sorts compose, so a group put
    in name order and then in type order reads as names within each type. It is
    the cheapest way to state "fontmaps last, alphabetically" and it is why the
    rank is the whole of the key.

    ``layout`` answers what a tilemap's cell **format** declares its cells to be
    (``"sprite"``, ``"text"``, or ``""`` for an even grid). Handed in because it
    is a question for the preset registry rather than for the entry, and only the
    type sort asks it: without one every map ranks as a plain tilemap, which is
    what an unrecognised format is anyway.
    """
    if key is SortKey.OFFSET:
        return sorted(entries, key=lambda e: (e.slice_offset, _natural_key(e.name)))
    if key is SortKey.TYPE:
        return sorted(entries, key=lambda e: _type_rank(e, layout))
    return sorted(entries, key=lambda e: _natural_key(e.name))


def _type_rank(entry: Entry, layout: Callable[[Entry], str] | None) -> int:
    """Where ``entry`` sits in :data:`_TYPE_ORDER`.

    A tilemap is asked what its format lays its cells out as; everything else is
    its content kind and nothing more. An unknown answer either way ranks with
    the plain reading of the kind it belongs to, since a map celPix has no format
    for is still a map and sorting is not the place to say otherwise.
    """
    if entry.content_kind is ContentKind.TILEMAP and layout is not None:
        declared = layout(entry)
        if declared in _TYPE_ORDER:
            return _TYPE_ORDER[declared]
    return _TYPE_ORDER.get(entry.content_kind.value, 0)


def _natural_key(name: str) -> tuple[tuple[int, object], ...]:
    """A sort key that reads runs of digits as numbers, case-insensitively.

    Plain string order is wrong for exactly the names this list holds: default
    slice names lead with a hex offset (``0x800`` sorts after ``0x1000``
    lexically, once the widths differ), and hand-typed ones number their variants
    (``tile10`` before ``tile2``). Splitting on digit runs and comparing those as
    integers puts both right.

    Each part carries a leading flag saying which kind it is, so a number is never
    compared against text. Two names line up by kind on their own — the split
    always starts with a (possibly empty) text piece and alternates from there —
    but that is a property of the splitting, and the flag is what makes it one of
    the key, which is where a comparison actually happens.
    """
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in _DIGIT_RUN.split(name)
    )


def pixel_config_for(
    entry: Entry,
    preset_id: str,
    registry: Registry,
    workspace: Workspace | None = None,
) -> PathwayConfig:
    """The pixel pathway config that reads (and writes back) ``entry``.

    A slice needs no special pipeline machinery: it is an ordinary config whose
    source is a *bounded* FileRef into the parent — the container slices the
    region, the compression scheme unpacks it, and at save time the same bounds
    make the write splice into (and never overflow) the parent's slot.

    Pass ``workspace`` so a slice of a parent with **unsaved pixel edits** reads
    those edits rather than the stale bytes on disk (see the module docstring);
    without it — or with a clean/unloaded parent — the file is the source, which
    is the same thing. The rebase is what keeps that honest: the parent's buffer
    starts at its header skip, so it is handed over as ``data`` with a matching
    ``data_base``, leaving the slice's own ``offset`` file-absolute for reading,
    writing and the address display alike.

    A compression scheme that can be decoded but not re-encoded yields a config
    with ``write_enabled=False`` — the slice loads and views fine, it just can't
    be written back. A whole file is the same rule applied to its **container**
    (``Entry.container_id``) — a slice does not go through one, for the reason
    given on that field — and both kinds apply it to their **reshape**
    (``Entry.reshape_id``), which either may carry. A stage whose plugin this
    build hasn't got at all is view-only too, and named in ``missing_plugins`` so
    the load can tell the user which one to install
    (:meth:`~celpix.plugins.registry.Registry.resolve_stage`).

    A slice is saved **through its parent** (``writes_through_parent``) rather
    than deposited at its own bounds, so its writability is its own stages'
    *and* its parent's — see the comment at the branch.

    A **composite** reads none of those stages: its bytes are already other
    entries' decompressed buffers, joined here rather than read from anywhere, so
    the config is an in-memory source over the assembled buffer with every byte
    stage on its pass-through (:func:`composite_config`).
    """
    if entry.kind is EntryKind.COMPOSITE:
        return composite_config(entry, registry, workspace, preset_id=preset_id)
    stages = [(Stage.RESHAPE, entry.reshape_id)]
    if entry.kind is EntryKind.FILE:
        stages.append((Stage.CONTAINER, entry.container_id))
    else:
        stages.append((Stage.COMPRESSION, entry.compression_id))
    resolved = {
        stage: registry.resolve_stage(stage, wanted) for stage, wanted in stages
    }
    missing = tuple(
        (stage, wanted) for stage, wanted in stages if resolved[stage][0] != wanted
    )
    writable = all(writes for _id, writes in resolved.values())
    reshape_id = resolved[Stage.RESHAPE][0]
    if entry.kind is EntryKind.FILE:
        return PathwayConfig(
            source=FileRef(entry.paths),
            interpret_preset_id=preset_id,
            container_id=resolved[Stage.CONTAINER][0],
            reshape_id=reshape_id,
            write_enabled=writable,
            missing_plugins=missing,
        )
    parent = workspace.find_file(entry.path) if workspace is not None else None
    reordered = parent is not None and reorders_bytes(parent, registry)
    live, live_base = _parent_view_bytes(entry, parent, reordered, registry, preset_id)
    if parent is not None:
        # **Every** slice is saved by splicing into the parent's buffer and
        # writing the parent, not by depositing at its own bounds. Under a
        # reordering parent that is the only thing that *can* work; everywhere
        # else it is what keeps the file whole - the parent's container gets to
        # run its write half (repair a checksum, re-wrap a header) over bytes
        # that changed inside it, which a splice around it silently skips.
        #
        # So the parent's own write is the thing this can fail on: a parent that
        # cannot save (a reshape with no unshape, a container with no write, a
        # plugin this build hasn't got) leaves the slice with nowhere to land.
        # A slice is part of the larger whole and cannot outrank it.
        writable = (
            writable and pixel_config_for(parent, preset_id, registry).write_enabled
        )
    return PathwayConfig(
        # The parent's *whole* file list, not just the file the slice is named
        # after: a slice's offset addresses the parent's joined buffer, so
        # reading one chip of a several-chip region would put every offset past
        # the first chip somewhere else entirely.
        source=FileRef(
            entry.paths,
            offset=entry.slice_offset,
            length=entry.slice_length,
            data=live,
            data_base=live_base,
        ),
        # The slice's own bounds: a file position where the parent reads
        # straight, a position in its buffer where it reorders. Either way they
        # bound the splice and the slot checks rather than naming a deposit —
        # `writes_through_parent` says the parent performs the delivery. Without
        # a workspace there is no parent to route through, and the config falls
        # back to depositing here (the factory's caller-beware form).
        dest=FileRef(entry.paths, offset=entry.slice_offset, length=entry.slice_length),
        interpret_preset_id=preset_id,
        reshape_id=reshape_id,
        compression_id=resolved[Stage.COMPRESSION][0],
        slot_fill=entry.slot_fill,
        write_enabled=writable,
        writes_through_parent=parent is not None,
        missing_plugins=missing,
    )


def reorders_bytes(entry: Entry, registry: Registry) -> bool:
    """Does reading ``entry`` move its bytes about, so its positions are not file
    offsets?

    True for an active reshape, and for a container that **permutes** rather than
    merely skipping (:attr:`~celpix.plugins.base.PluginInfo.preserves_offsets`) —
    a ``.smd``, an interleaved SNES image, a byte-swapped N64 dump. In both cases
    the entry's buffer is the ROM as the machine addresses it and the file is a
    scrambled encoding of that, so a position in one names nothing in the other.

    Everything that resolves an offset against this entry's coordinates keys off
    this: a slice of it has to read (and cannot write) through its buffer, and so
    does an Offset palette (``docs/design/palette-editing.md`` §2). A plugin the
    registry no longer has reads as plain bytes, which preserve positions.
    """
    if registry.resolve_stage(Stage.RESHAPE, entry.reshape_id)[0] != NO_RESHAPE:
        return True
    plugin = registry.plugin(
        Stage.CONTAINER, resolved_container_id(registry, entry.container_id)
    )
    return not plugin.info.preserves_offsets


def entry_view_bytes(
    entry: Entry,
    registry: Registry,
    preset_id: str,
    workspace: Workspace | None = None,
) -> tuple[bytes, int]:
    """``entry``'s view buffer and the file offset its first byte sits at.

    The single definition of "what this entry shows": its live document's bytes
    when one is loaded, else the region read fresh through its own container and
    reshape (``pipeline.read_region`` — the preset is inert, the read stops
    before any codec runs). The base is what Read **recorded**, not what the
    config asked for: only the container knows where it actually began (past a
    copier header, past the iNES header and PRG banks).

    Everything that resolves an offset in this entry's coordinates reads through
    this — a slice of a reordering parent, an Offset palette — so they can never
    disagree with the view about what the bytes at an offset are.

    One exception, and it is deliberate: a **tilemap's bound tiles** apply the
    same rule in their own function
    (:meth:`~celpix.ui.main_window.session.SessionMixin._live_bound_tiles`),
    because they need the read's context and its tile geometry as well as its
    bytes, and this returns neither. The two must move together.
    """
    if entry.doc is not None:
        return entry.doc.pixel_data, entry.doc.pixel_ctx.get(KEY_SOURCE_OFFSET, 0)
    data, ctx = pipeline.read_region(
        pixel_config_for(entry, preset_id, registry, workspace), registry
    )
    return data, ctx.get(KEY_SOURCE_OFFSET, 0)


# -- composites (docs/design/composite-entry.md) ---------------------------
@dataclass(frozen=True)
class PieceSpan:
    """Where one :class:`CompositePiece` landed in the assembled buffer.

    ``owner`` is the entry those bytes belong to — the piece's, and **None** for
    a pad or for a source that could not be read. That is the whole of what the
    deposit path needs: a byte position in a composite either has an owner to be
    written into or it has nothing behind it, and an edit there is refused rather
    than kept in a buffer no file answers for
    (``docs/design/slices-and-parents.md``).

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
    missing preset gets (:data:`~celpix.plugins.base.STAGE_DEFAULT_PRESET`).
    """
    for piece in entry.pieces:
        source = piece.entry
        if source is None or source.session is None:
            continue
        return registry.resolve_preset(
            Stage.INTERPRET_PIXEL, source.session.pixel_preset_id
        )
    return STAGE_DEFAULT_PRESET[Stage.INTERPRET_PIXEL]


def can_compose(entry: Entry, candidate: Entry) -> bool:
    """Whether ``candidate`` is something ``entry`` could take a piece from.

    The one rule behind both the picker that offers sources and the assembly that
    reads them, so what is offered and what is accepted cannot disagree — the
    discipline :meth:`~celpix.ui.main_window.session.SessionMixin.
    _can_supply_tiles` follows, for the same reason.

    **A composite cannot contain a composite.** Without that, a pair pointed at
    each other would each assemble the other, forever; with it, the depth
    question has one answer instead of a recursion limit. Nor itself, which would
    be that same loop with one participant.

    Only **pixels**: a palette or a bookmark has no pixel buffer to contribute,
    and a tilemap's is a borrowed copy of somebody else's art rather than bytes
    of its own — assembling one would put a borrowed copy inside a composite.
    """
    return (
        candidate is not entry
        and candidate.kind is not EntryKind.COMPOSITE
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
    keeps its length whatever the source turns out to hold. A piece without one
    takes everything from ``offset`` on, **rounded up to a whole tile** — that
    rounding is the point of the feature rather than tidiness, since a composite
    view exists to give a map one predictable index space and a ragged source
    would put every tile after it part of a tile out. A stated range is honoured
    to the byte instead, because at that point the user has said where the run
    ends and rounding would move the next one.

    A pad piece — and a source that is closed, unreadable, or one this refuses
    (:func:`can_compose`) — contributes ``piece.extent`` blank bytes. Contributing
    *nothing* would be worse than blank: every cell of every map bound to the
    composite would shift by the length of the missing run.

    Returns the refreshed pieces alongside, each carrying what was actually
    assembled as its ``measured`` — never as its ``length``, which is the user's
    request and not this function's to touch.
    :func:`record_composite_layout` is what records them, on the same
    "discovered at load, written onto the entry" footing as
    :func:`backfill_slice_length`.
    """
    # Only the un-ranged case needs this, and only to round a ragged source up.
    # Everything else here is bytes, which is the unit a source and the composite
    # reading it can both agree on (:class:`CompositePiece`).
    tile_bytes = pipeline.pixel_tile_bytes(
        preset_id or composite_preset_id(entry, registry), registry
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
        if data is None:
            # The pad case, and every failure above: the run keeps the length it
            # last had so nothing downstream of it moves.
            data = bytes(piece.extent)
        else:
            end = piece.offset + piece.length if piece.length else len(data)
            cut = data[piece.offset : end]
            if piece.length and len(cut) < piece.length:
                problems.append(
                    f"{piece.entry.name} resolves to {len(data)} bytes, too short for "
                    f"the {piece.length} asked for at {format_hex(piece.offset)}; the "
                    "rest of the run is blank"
                )
                cut = cut + bytes(piece.length - len(cut))
            elif not piece.length and len(cut) % tile_bytes:
                filled = ceil_div(len(cut), tile_bytes) * tile_bytes
                problems.append(
                    f"{piece.entry.name} holds {len(cut)} bytes, which is not a whole "
                    f"number of {tile_bytes}-byte tiles; it was padded to {filled}"
                )
                cut = cut + bytes(filled - len(cut))
            data = cut
        spans.append(PieceSpan(len(out), len(data), owner, piece.offset))
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
        write_enabled=False,
    )


def record_composite_layout(entry: Entry, layout: CompositeLayout) -> bool:
    """Record what an assembly measured onto ``entry``; True if a length changed.

    Two things, and they are recorded together because one assembly is where both
    become known — and because letting them be answered separately is what let
    them disagree:

    - each piece's ``measured``, a cache of how many bytes that run actually
      produced, refreshed here at load exactly as a decompressed slice's
      discovered extent is (:func:`backfill_slice_length`). Keeping it current is
      what lets a closed source leave a hole of the right size behind it instead
      of collapsing the composite.
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


def _parent_view_bytes(
    entry: Entry,
    parent: Entry | None,
    reordered: bool,
    registry: Registry,
    preset_id: str,
) -> tuple[bytes | None, int]:
    """The parent bytes a slice reads through, and the file offset they start at.

    ``(None, 0)`` — read the files — whenever the parent's own Read is a plain
    window onto them, because then the slice's offset lands on the same bytes
    either way. Two things make the parent's buffer the only correct source:

    - **A parent that reorders** (``reordered``, from :func:`reorders_bytes`).
      Then the file simply does not hold the bytes the slice's offset names,
      dirty or not — so the buffer is read even when the parent has no document
      of its own (it is re-read for this), since falling back to disk would
      quietly hand back a scrambled region.
    - **Unsaved pixel edits.** A dirty parent's live bytes are what the slice is a
      view of; the file still holds the old ones. A dirty *palette* doesn't
      qualify — it lives on the other pathway and in another file, so it says
      nothing about these bytes.

    Either way the slice must fall inside the parent's window: one anchored
    before whatever the parent's container skipped isn't in that buffer at all,
    so it reads from disk (and under a reorder those bytes are no one's to name).
    """
    if parent is None:
        return (None, 0)
    if not (reordered or (parent.doc is not None and parent.pixel_dirty)):
        return (None, 0)
    # Both cases want exactly what the parent's own view shows, which is the one
    # definition of that; with a loaded document it costs no read.
    data, base = entry_view_bytes(parent, registry, preset_id)
    return (data, base) if entry.slice_offset >= base else (None, 0)


def palette_source_for(entry: Entry) -> PaletteSource | None:
    """The entry's live palette as restorable plain data — ``None`` for default.

    Derived from the loaded document (its palette config is the truth for the
    file/offset modes) plus the session's mode; a never-activated entry has no
    live state, so its pending source (if any) is returned as-is. This is the
    inverse of :meth:`_apply_restored_state`'s consumption of ``pending_palette``
    — it's what both project-save and new-slice seeding read to carry a palette
    forward. An offset source is an absolute file offset, so it resolves against
    a slice's parent file exactly as it does for the parent itself.
    """
    # A degraded palette (its file went missing) keeps its intended source here
    # rather than on the live config, so save and new-slice seeding carry the
    # reference forward even while the entry renders on the default palette.
    if entry.missing_palette is not None:
        return entry.missing_palette
    if entry.doc is None or entry.session is None:
        return entry.pending_palette
    mode = entry.session.palette_mode
    source = entry.doc.palette_config.source
    if mode is PaletteMode.CUSTOM:
        # The custom palette *is* the project data — there is no file behind it,
        # so the colors themselves are what round-trips.
        return PaletteSource(colors=list(entry.doc.palette.colors))
    if mode is PaletteMode.FILE:
        return PaletteSource(path=source.path, offset=source.offset)
    if mode is PaletteMode.OFFSET:
        return PaletteSource(offset=source.offset)
    if mode is PaletteMode.EMULATOR:
        # Only the state file's path is stored; where the palette sits inside it
        # (and which console codec decodes it) is re-detected on restore, so a
        # newer detector or an edited state stays authoritative over stale coords.
        return PaletteSource(path=source.path)
    return None


# -- missing-reference handling (docs/design/project-format.md §3) ---------
def data_missing(entry: Entry) -> bool:
    """Whether any of the entry's data files is gone from disk.

    For a slice or bookmark this is the parent file (their ``path``); a missing
    parent leaves the child unloadable exactly as a missing file does. **Any**
    of a several-file region counts: the region is the files joined, so one
    absent chip does not shorten it, it moves every byte after the gap.
    """
    return any(not exists(path) for path in entry.paths)


def entry_palette_path(entry: Entry) -> str | None:
    """The external palette-source file the entry references, or ``None``.

    Only file/emulator modes have an external palette file, and its path is read
    from wherever the entry currently keeps it: the degraded source (loaded, but
    its file went missing), the live document config (loaded and healthy), or
    the pending source (not yet activated).
    """
    session = entry.session
    if session is None or not session.palette_mode.has_external_file:
        return None
    if entry.missing_palette is not None:
        return entry.missing_palette.path
    if entry.doc is not None:
        return entry.doc.palette_config.source.path or None
    if entry.pending_palette is not None:
        return entry.pending_palette.path
    return None


def palette_missing(entry: Entry) -> bool:
    """Whether the entry's external palette file is referenced but gone."""
    path = entry_palette_path(entry)
    return path is not None and not exists(path)


def path_is_palette_only(ws: Workspace, path: str) -> bool:
    """Whether nothing in ``ws`` reads ``path`` as pixel data.

    True for a file referenced only as a palette — an entry's external palette
    source, or a registered ``.pal`` row. What tells the two kinds of missing
    file apart when the user is being asked to find one: a palette file follows
    the graphic that uses it and may never have been picked by hand, so being
    asked for it by bare name reads as "which of my ROMs is this?".
    """
    key = Workspace.path_key(path)
    return not any(
        entry.kind is not EntryKind.PALETTE
        and any(Workspace.path_key(p) == key for p in entry.paths)
        for entry in ws.entries
    )


def entry_notices(entry: Entry) -> tuple[Notice, ...]:
    """What the stages said while reading ``entry`` — both pathways, pixel first.

    Read off the live document rather than stored on the entry, because that is
    where they are already: a notice is produced by a load and the document *is*
    the result of one, so the two cannot fall out of step. An entry whose document
    has never been built has nothing to report, which is correct — nothing has
    been read yet.
    """
    if entry.doc is None:
        return ()
    return notices(entry.doc.pixel_ctx) + notices(entry.doc.palette_ctx)


@dataclass(frozen=True)
class MissingPreset:
    """One entry field that named a format this build hasn't got, and its stand-in.

    ``used`` is what the field now holds — the stage's default format, or ``""``
    where the field was cleared instead (an alphabet, which has no stand-in).
    """

    entry: Entry
    stage: Stage
    wanted: str
    used: str


def repair_presets(entries: list[Entry], registry: Registry) -> list[MissingPreset]:
    """Point every entry at a format ``registry`` has; report what was swapped.

    The Interpret-stage counterpart to what :func:`pixel_config_for` does for the
    byte stages, and it has to be a *repair* rather than a resolution at the point
    of use: a byte stage's pass-through is one substitution inside one config,
    where a preset id is read by a dozen surfaces — the codec combo, the transform
    probes, the cell width, every decode — and each of them answering "no format"
    separately is how a missing preset became an uncaught ``KeyError`` in the
    first place. One pass, before the entries are shown, leaves every one of those
    reading a format that exists.

    Called wherever the registry and the entries can disagree, which is exactly
    where the registry is rebuilt: opening a project (whose ``plugins/`` folder
    may supply formats the previous one did not), and refreshing plugins.

    **The stored id is overwritten**, so saving the project afterwards writes the
    stand-in and the original reference is gone — which is why the swap is
    reported and shown rather than made quietly. Quitting without saving keeps the
    project file as it was, and installing the missing plugin makes it open
    correctly again.
    """
    replaced: list[MissingPreset] = []

    def resolved(entry: Entry, stage: Stage, wanted: str) -> str:
        if not wanted:
            return wanted
        used = registry.resolve_preset(stage, wanted)
        if used != wanted:
            replaced.append(MissingPreset(entry, stage, wanted, used))
        return used

    for entry in entries:
        if entry.session is not None:
            entry.session.pixel_preset_id = resolved(
                entry, Stage.INTERPRET_PIXEL, entry.session.pixel_preset_id
            )
            entry.session.palette_preset_id = resolved(
                entry, Stage.INTERPRET_PALETTE, entry.session.palette_preset_id
            )
        if entry.palette_preset_id:
            entry.palette_preset_id = resolved(
                entry, Stage.INTERPRET_PALETTE, entry.palette_preset_id
            )
        if entry.tilemap_preset_id:
            entry.tilemap_preset_id = resolved(
                entry, Stage.INTERPRET_TILEMAP, entry.tilemap_preset_id
            )
        # Nothing here for the font alphabet: it is the entry's own data now
        # (``font_chars`` / ``font_codes``), so there is no plugin for it to be
        # missing and nothing to repair.
    return replaced


def missing_paths(ws: Workspace) -> list[str]:
    """Every referenced path not on disk, de-duplicated, in list order.

    Unions each entry's data file with its external palette file, so one shared
    ROM (a file plus the slices/bookmarks under it) yields a single worklist
    entry — located once, corrected everywhere.
    """
    seen: set[str] = set()
    result: list[str] = []
    for entry in ws.entries:
        # Each *individually* missing file of a region, not the whole list: the
        # user locates the one chip that moved, and the ones still on disk must
        # not be put in front of them again.
        candidates = [path for path in entry.paths if not exists(path)]
        if palette_missing(entry):
            candidates.append(entry_palette_path(entry))
        for path in candidates:
            key = Workspace.path_key(path)
            if key not in seen:
                seen.add(key)
                result.append(path)
    return result


def relocate_path(ws: Workspace, old_path: str, new_path: str) -> list[Entry]:
    """Repoint every reference to ``old_path`` at ``new_path``; return the
    entries touched.

    Rewrites an entry's data ``path`` and any pending/degraded palette source
    naming the same file, so relocating a shared ROM fixes the file and its
    slices/bookmarks (and any palette read from it) together. Pure data — the
    caller reloads the affected documents/palettes.
    """
    key = Workspace.path_key(old_path)
    old_name, new_name = basename(old_path), basename(new_path)
    touched: list[Entry] = []
    for entry in ws.entries:
        data_moved = bool(entry.path) and Workspace.path_key(entry.path) == key
        if data_moved:
            entry.path = new_path
            # A FILE's or PALETTE's display name defaults to its on-disk
            # basename, so a located file that was renamed (or re-extensioned)
            # takes the new name — but only while the row is still showing that
            # default. A name the user typed is theirs, and survives the move;
            # slices and bookmarks are always named that way.
            if entry.kind in (EntryKind.FILE, EntryKind.PALETTE):
                if entry.name == old_name:
                    entry.name = new_name
        # A region's later chips move the same way, and independently: locating
        # one of them must not disturb the others or the order they join in.
        moved_extra = tuple(
            new_path if Workspace.path_key(p) == key else p for p in entry.extra_paths
        )
        if moved_extra != entry.extra_paths:
            entry.extra_paths = moved_extra
            data_moved = True
        changed = data_moved
        for source in (entry.missing_palette, entry.pending_palette):
            if source is None or not source.path:
                continue
            if Workspace.path_key(source.path) == key:
                source.path = new_path
                changed = True
        if changed:
            touched.append(entry)
    return touched


def retarget_files(ws: Workspace, entry: Entry, paths: tuple[str, ...]) -> list[Entry]:
    """Re-point a FILE at ``paths``, carrying its children; the entries touched.

    A file list is the entry's identity as much as its content: ``paths[0]`` is
    the row in the Files list, the key a slice or bookmark finds its parent by,
    and the file a save is attributed to. So the children move in the same step —
    a child's offset addresses the *joined* buffer (:func:`pixel_config_for`), so
    it has to be joined the same way to mean anything, and one left on the old
    path would no longer find its parent at all.

    A FILE's display name defaults to its first file's basename, so it follows
    the list too — unless the user has renamed the row, which is theirs to keep
    (the same rule as :func:`relocate_path`). Pure data — the caller drops the
    affected documents and re-reads them.
    """
    if entry.kind is not EntryKind.FILE or not paths:
        return []
    first, *rest = paths
    named_after_file = entry.name == basename(entry.path)
    # The children are found *before* the path moves — they are keyed by the one
    # that is about to change.
    touched = [entry, *ws.children_of(entry)]
    for moved in touched:
        moved.path = first
        moved.extra_paths = tuple(rest)
    if named_after_file:
        entry.name = basename(first)
    return touched


def exportable_entries(ws: Workspace) -> list[Entry]:
    """The entries a bulk (whole-project) export should render, in list order.

    Every slice, plus every FILE that has **no** slices. A file that *has* slices
    is skipped: its slices are the curated regions worth exporting, so dumping the
    whole file alongside them would be redundant (and a whole ROM is rarely a
    useful image). A sliced file is exported only when the user names it
    explicitly (the single-entry Export), never in bulk — matching the rule that a
    file with slices isn't exported unless it alone is selected. Bookmarks and
    palettes hold no graphic of their own and never appear.

    A **composite** always appears, and is not redundant with the pieces it is
    assembled from even though its bytes are theirs: the picture it makes is one
    nobody else in the list draws, which is the whole reason it exists. It has no
    slices of its own to defer to either — a slice's parent is always a file.
    """
    result: list[Entry] = []
    for entry in ws.entries:
        if entry.kind in (EntryKind.SLICE, EntryKind.COMPOSITE):
            result.append(entry)
        elif entry.kind is EntryKind.FILE and not ws.slices_of(entry):
            result.append(entry)
    return result


# Characters kept verbatim in an export filename; everything else becomes '_' so
# a slice name (which may hold spaces, parentheses, or path separators) is always
# a safe basename on every platform.
_SAFE_NAME = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_."
)


def export_basename(entry: Entry) -> str:
    """A filesystem-safe basename (no extension) for exporting ``entry``.

    A FILE keeps its own stem (``foo.chr`` → ``foo``). A slice is prefixed with
    its parent file's stem so slices of different files don't collide in one
    export folder, and its own (possibly punctuation-heavy) name is sanitized
    (``foo`` + ``1000 (800)`` → ``foo_1000__800_``). The caller still de-dupes,
    since two slices of one file can share a name.

    A **composite** has no file to take a stem from, so its own name is the whole
    answer — which is also the only name it has ever had, being the one the user
    typed when they built it.
    """
    if entry.kind is EntryKind.COMPOSITE:
        return _sanitize(entry.name)
    parent_stem = splitext(basename(entry.path))[0] or "export"
    if entry.kind is not EntryKind.SLICE:
        return _sanitize(parent_stem)
    return f"{_sanitize(parent_stem)}_{_sanitize(entry.name)}"


def _sanitize(name: str) -> str:
    cleaned = "".join(c if c in _SAFE_NAME else "_" for c in name).strip("._")
    return cleaned or "export"


def default_slice_name(
    offset: int,
    length: int | None,
    compression_id: str = NO_COMPRESSION,
    reshape_id: str = NO_RESHAPE,
) -> str:
    """The generated name for an unnamed slice:
    ``offset (length) reshape compression``.

    No parent-filename prefix — the slice nests under its parent in the list,
    so the coordinates alone identify it. The length is omitted while still
    unknown (a compressed slice awaiting discovery), as are the pass-through
    reshape and compression.
    """
    parts = [format_hex(offset)]
    if length is not None:
        parts.append(f"({format_hex(length)})")
    if reshape_id != NO_RESHAPE:
        parts.append(reshape_id.removeprefix("reshape."))
    if compression_id != NO_COMPRESSION:
        parts.append(compression_id.removeprefix("compression."))
    return " ".join(parts)


def backfill_slice_length(entry: Entry, ctx: PipelineContext) -> bool:
    """Fill in a decompressed slice's extent discovered at load; True if it did.

    A slice created without a length ("decompress from here, wherever it ends")
    reads to end-of-file, and the decompressor reports the structure's true
    byte extent in the context. Recording that extent onto the entry bounds
    every later load — and, crucially, makes save-back slot-enforced. Only a
    *complete* decompress counts: a truncated/partial extent would bound the
    slice at the wrong size.

    **Never under an active reshape.** The discovered extent is measured in
    *reshaped* space, and re-bounding the window changes the region's length —
    which changes the permutation itself, so the slice would decode differently
    after discovery than during it. The slice dialog requires an explicit
    length whenever a reshape is chosen, so this guard is its backstop.
    """
    if entry.kind is not EntryKind.SLICE or entry.slice_length is not None:
        return False
    if entry.reshape_id != NO_RESHAPE:
        return False
    consumed = ctx.get(KEY_COMPRESSED_SIZE)
    if not consumed or not ctx.get(KEY_DECOMPRESS_COMPLETE):
        return False
    entry.slice_length = consumed
    return True
