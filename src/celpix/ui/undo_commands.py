"""Undo commands for the main window's editing surfaces.

Undo/redo is built on Qt's ``QUndoStack``/``QUndoCommand`` — a deliberate
exception to the Qt-free-model rule (``docs/design/undo-redo.md``): history is
per-launch UI session state, and Qt's stack provides menu actions, merging and
obsolete-command handling for free while ``core``/``pipeline``/``project``
stay Qt-free.

One **unified session stack** holds every command in chronological order —
structural files-pane operations, per-document config changes, view moves,
and pixel/color edits — so a single Ctrl+Z always reverts the most recent
action regardless of which surface made it. Two consequences shape the
classes here:

- **Document-scoped commands carry their entry and re-activate it** before
  applying, so undoing a change made in another entry first switches the view
  back to where that change happened. The two *editing* commands carry the
  tile/pixel edit mode alongside it (``_ensure_edit_context``): the same bytes
  are edited from either mode, and a step reverts where it was made.
- **Entry lifecycle is itself on the stack** (`AddEntryCommand` /
  `RemoveEntriesCommand`, which keep the removed `Entry` *objects*), so a
  command can never reference an entry that chronology hasn't restored yet.
  The one lifecycle change outside the stack — loading a project — clears it.

Commands are thin: each captures only the before/after of what one gesture
touched and delegates all application to a ``MainWindow`` ``_apply_*`` helper,
called inside the window's re-entrancy guard so an apply can never push a
second command. ``QUndoStack.push()`` invokes ``redo()`` immediately — push
sites therefore capture state *before* mutating and let the first ``redo()``
do the work.

That shape is :class:`_StateCommand`, and most commands here are one line of it:
a subclass says how far it has to reach (:class:`_CurrentEntryCommand`,
:class:`_InPlaceCommand`, :class:`_EditModeCommand`) and what applying one half
of its pair means. The exceptions are written out, and are the ones whose two
directions are genuinely different operations rather than one over a pair —
adding versus removing an entry, and the color edit, which merges.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QRect
from PySide6.QtGui import QUndoCommand

from celpix.core.context import PipelineContext
from celpix.core.document import Document
from celpix.core.palette import Palette
from celpix.core.paletteregions import PaletteRegions
from celpix.core.tilerearrangement import TileRearrangement
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.project.workspace import (
    Entry,
    EntryKind,
    PaletteMode,
    PaletteSource,
    SliceParams,
    TileSource,
)
from celpix.ui.container_dialog import ContainerEdit

if TYPE_CHECKING:
    from celpix.ui.main_window import MainWindow

# QUndoStack only attempts mergeWith on commands whose id() match (and -1
# never merges); any other command landing in between breaks the chain.
OFFSET_MOVE_ID = 1
COLOR_EDIT_ID = 2


@dataclass(frozen=True)
class PaletteState:
    """Snapshot of a document's palette pathway plus its UI selectors.

    Palettes are small (≤512 entries), so snapshotting the loaded colors is
    cheap and makes undo exact — no fallible re-load from disk. The
    :class:`Palette` is held by reference, which is safe because color edits
    never mutate in place: :meth:`~celpix.core.palette.Palette.with_color`
    returns a new palette and the window swaps it in, leaving every captured
    snapshot intact.
    """

    preset_id: str
    mode: PaletteMode
    palette: Palette
    config: PathwayConfig
    ctx: PipelineContext
    # The bytes the palette was read from — the splice base a save needs (see
    # Document.palette_base_bytes). Carried through undo so reverting a palette
    # change restores the right base, not just the right colors.
    base_bytes: bytes = b""
    edits: frozenset[int] = frozenset()


class _StateCommand(QUndoCommand):
    """One entry's state moving between a captured ``before`` and ``after``.

    The shape almost every command here has: hold the pair, and in each direction
    hand the right half to one ``MainWindow`` ``_apply_*`` helper inside the
    window's re-entrancy guard. Stating it once is what keeps the two invariants
    from having to be re-remembered per command — the guard must wrap the apply
    (or an apply could push a second command), and a document-scoped change must
    reach the entry it happened in before it lands.

    Subclasses supply :meth:`_apply`, and pick their reach by subclassing
    :class:`_CurrentEntryCommand` or :class:`_InPlaceCommand` rather than by
    overriding :meth:`_reach` directly. Commands whose two directions are not the
    same operation over a pair — adding versus removing an entry — are not this
    shape and stay written out.
    """

    def __init__(
        self, window: MainWindow, entry: Entry, text: str, before, after
    ) -> None:
        super().__init__(text)
        self._window = window
        self._entry = entry
        self._before = before
        self._after = after

    def redo(self) -> None:
        self._run(self._after)

    def undo(self) -> None:
        self._run(self._before)

    def _run(self, state) -> None:
        with self._window._undo_apply():
            if self._reach():
                self._apply(state)

    def _reach(self) -> bool:
        """Put the window where this command's change belongs; False to skip."""
        raise NotImplementedError

    def _apply(self, state) -> None:
        """Land ``state`` — one direction of this command, on the window."""
        raise NotImplementedError


class _CurrentEntryCommand(_StateCommand):
    """A change to the *document on screen*: undo returns to it first.

    The unified stack is chronological across entries, so a step made in another
    entry has to switch the view back before it can be reverted where it happened
    (``docs/design/undo-redo.md``).
    """

    def _reach(self) -> bool:
        return self._window._ensure_current(self._entry)


class _InPlaceCommand(_StateCommand):
    """A change visible wherever you are, so the view never moves for it.

    A rename, a reorder, a slice re-pointed: the files pane is where it shows, and
    yanking the view to the affected entry would be a surprise rather than the
    context the change needs.
    """

    def _reach(self) -> bool:
        return True


class _EditModeCommand(_StateCommand):
    """A change made *in* an editing mode, reverted where it was made.

    The same bytes are edited from tile mode and from pixel mode, so a step that
    came back in the other one would land a marquee on a document not in pixel
    mode, or a tile edit with the pixel tools armed. The mode therefore travels
    with the command alongside the entry, and reaching the change means restoring
    both.

    ``through`` splits the entry the change is *seen* in from the entry that owns
    the bytes, which are usually one and are not when a pixel edit is made through
    a **tilemap**: the gesture happens on the map, the bytes belong to the tile
    bank it is bound to (``docs/design/tilemap-entry.md`` §8.1). Reaching the bank
    would take the view off the picture the stroke was drawn on, and reverting the
    stroke somewhere it cannot be seen is the thing this class exists to prevent.
    """

    def __init__(
        self,
        window: MainWindow,
        entry: Entry,
        text: str,
        before,
        after,
        *,
        through: Entry | None = None,
    ) -> None:
        super().__init__(window, entry, text, before, after)
        self._mode = window._edit_mode
        self._through = through if through is not None else entry

    def _reach(self) -> bool:
        return self._window._ensure_edit_context(self._through, self._mode)


class OffsetMoveCommand(_CurrentEntryCommand):
    """One view-position move; consecutive moves in the same entry merge."""

    def __init__(
        self,
        window: MainWindow,
        entry: Entry,
        *,
        before: tuple[int, int],  # (offset, nudge)
        after: tuple[int, int],
    ) -> None:
        super().__init__(window, entry, "move view", before, after)

    def id(self) -> int:
        return OFFSET_MOVE_ID

    def mergeWith(self, other: QUndoCommand) -> bool:
        # The same-entry check is load-bearing on the unified stack: moves in
        # entry A and entry B can sit adjacent and must stay separate steps.
        if not isinstance(other, OffsetMoveCommand) or other._entry is not self._entry:
            return False
        self._after = other._after
        if self._after == self._before:
            # The run walked back to its start — drop the empty step entirely.
            self.setObsolete(True)
        return True

    def _apply(self, state: tuple[int, int]) -> None:
        self._window._apply_offset(*state)


class TileRearrangementCommand(_CurrentEntryCommand):
    """One rearrangement of tile *display* positions, as before/after maps.

    A rearrangement moves nothing in the file (see
    :mod:`celpix.ui.main_window.rearrange`), so unlike an edit this command has
    no bytes and stamps no revision: undoing it leaves the document exactly as
    dirty — or as clean — as it was. It is on the undo stack all the same,
    because a drag is an interaction the user will expect Ctrl+Z to take back,
    and a mis-drop is easy to make and tedious to reverse by hand.

    The maps are whole values rather than a delta: they hold only the tiles that
    moved, so a snapshot is small however large the file, and restoring one
    cannot drift the way replaying a sequence of swaps could.
    """

    def _apply(self, state: TileRearrangement) -> None:
        self._window._set_tile_rearrangement(state)


class TilemapCellsCommand(_InPlaceCommand):
    """One edit to a tilemap's cells, as before/after lists.

    Unlike :class:`TileRearrangementCommand` this **is** an edit — the cells are
    the entry's own data and a save writes them — so it stamps a revision on the
    data pathway in each direction, and an undo back to what was written reads
    clean again.

    Whole lists rather than a delta, for the reason the rearrangement gives: a
    map is a few thousand frozen cells, so a snapshot costs less than the
    bookkeeping a delta would need, and restoring one cannot drift the way
    replaying a sequence of per-cell writes could.
    """

    def __init__(
        self,
        window: MainWindow,
        entry: Entry,
        text: str,
        before: list,
        after: list,
    ) -> None:
        # The state is the cells *paired with* the data-pathway revision they leave
        # the entry at, so an undo hands back the exact unsaved-state it had before.
        super().__init__(
            window,
            entry,
            text,
            (before, entry.pixel_revision),
            (after, window._workspace.next_revision()),
        )

    def _apply(self, state: tuple[list, int]) -> None:
        cells, revision = state
        self._window._set_cells(self._entry, cells, revision)


@dataclass(frozen=True)
class TilemapBindingState:
    """What a tilemap entry draws from, and how its cells are read.

    The whole of the binding bar's answer travels as one value — the tile
    source, the cell format and the sprite size pair — because they are one kind
    of thing: project state no file records, set on one bar and remembered per
    entry (``docs/design/tilemap-entry.md`` §3, §7). A gesture moves exactly one
    of them, so a snapshot is three fields where two are unchanged, which is
    cheaper than three command classes and cannot get out of step with itself.

    The palette row base is **not** here, though the bar used to set it: it is a
    fact about how a named row meets the palette that got loaded, which a tile
    bank has as much as a map, so it travels on its own
    (:class:`PaletteRowBaseCommand`).

    The three palette fields ride along because **binding seeds a palette**
    (:meth:`~celpix.ui.main_window.tilemap_bar.TilemapBarMixin._seeded_palette`):
    a map still on the default palette adopts its source's colours the first
    time it is pointed at one, so a step that put the binding back and left
    those colours would be half a revert. They are plain data — a mode, a preset
    id and the pending source a load consumes — rather than the loaded palette a
    :class:`PaletteState` carries, because at the moment this is captured the
    seed has not been read yet.
    """

    tile_source: TileSource | None = None
    preset_id: str | None = None
    size_pair: tuple[int, int] | None = None
    palette_mode: PaletteMode = PaletteMode.DEFAULT
    palette_preset_id: str = ""
    pending_palette: PaletteSource | None = None


class TilemapBindingCommand(_CurrentEntryCommand):
    """One change to a tilemap's binding, as before/after snapshots.

    Sibling of :class:`PixelConfigCommand`, and for the same reason: the state
    is small and re-deriving the document from it is what applying *means*, so
    the command carries the configuration and never the bytes it produces. It
    stamps no revision — a binding is project state, not the entry's data, so
    the map is no more (or less) unsaved for having been re-pointed, though the
    *project* is, which needs nothing here (it is computed by re-serializing).

    Unlike the pixel switch there is no pre-validated payload: a tilemap load
    builds the document and its bound tiles together, so there is nothing to
    hand over half-done. So validation happens by *trying*, and the apply takes
    a failure back off — which leaves the same outcome the pixel switch reaches
    by refusing up front, as long as the dead step goes too. It does, on the
    **first** redo: a command marked obsolete inside ``push()`` is dropped
    instead of pushed, so a gesture that could not be read costs no step.

    A later undo or redo that fails is the ordinary
    ``docs/design/undo-redo.md`` §5 edge and is left alone — the state is
    restored and the step stays where the history put it, because removing a
    command from the middle of a stack strands every step underneath it.
    """

    def __init__(
        self, window: MainWindow, entry: Entry, text: str, before, after
    ) -> None:
        super().__init__(window, entry, text, before, after)
        self._pushed = False

    def redo(self) -> None:
        super().redo()
        self._pushed = True

    def _apply(self, state: TilemapBindingState) -> None:
        # Which direction this is, so the apply can ask what *the step* moved
        # rather than what the entry happens to hold. A push site that has
        # already pointed the entry at its new source, so the switch back to it
        # reads the right bytes (``_bind_tiles_from_file``), would otherwise look
        # from the inside like a step that changed nothing but a palette row —
        # and land in place, leaving the newly bound tiles unread.
        previously = self._before if state is self._after else self._after
        landed = self._window._apply_tilemap_binding(self._entry, state, previously)
        if not landed and not self._pushed:
            # The gesture could not be read and the apply put everything back, so
            # this step stands for nothing. Marked here rather than by the push
            # site because only ``redo()`` inside ``push()`` can still refuse the
            # command — after that, dropping it would strand the history below it.
            self.setObsolete(True)


class PaletteRegionsCommand(_CurrentEntryCommand):
    """One pin/unpin of palette regions, as before/after sets.

    Sibling of :class:`TileRearrangementCommand` in every respect that matters: a pinned
    region changes no bytes, so this **stamps no revision** and undoing it leaves
    the document exactly as dirty — or as clean — as it was. It is on the stack
    all the same, because pinning is an interaction the user will expect Ctrl+Z to
    take back, and a mis-pinned rectangle is tedious to undo by hand. It does make
    the *project* unsaved, which needs nothing here: that is computed by
    re-serializing and diffing.

    The sets are whole values rather than a delta. They hold only the pinned spans
    — a handful however large the file — so a snapshot is cheap, and restoring one
    cannot drift the way replaying a sequence of pins and unpins could.
    """

    def _apply(self, state: PaletteRegions) -> None:
        self._window._set_palette_regions(state)


class PaletteRowBaseCommand(_CurrentEntryCommand):
    """One move of the palette dock's Base Palette Row spin.

    Its own step rather than a field of the binding above, because the base is
    not a binding: a tile bank has one as much as a map does — a bank's per-tile
    rows count from it exactly as a map's cells do — and a pixel entry has no
    binding to carry it. Like a pinned region it stamps **no revision**: the base
    changes how the bytes are read, never what they are.

    The state is a pair, ``(entry, document)``, and both halves are needed. The
    entry's is the user's own answer and may be ``None`` — "whatever the file
    says" — while the document carries the base **in force**, which is the
    resolved number a render reads
    (:meth:`~celpix.ui.main_window.session.SessionMixin._row_base_for`). Carrying
    the resolved one is what lets undo put back the file's answer without a
    re-read: a pixel entry holds unsaved edits its document is the only copy of,
    so reloading it to recover a number would cost the user their work.
    """

    def _apply(self, state: tuple[int | None, int]) -> None:
        self._window._set_palette_row_base(self._entry, *state)


class PixelConfigCommand(_CurrentEntryCommand):
    """A pixel interpretation change: preset switch or header-skip change.

    Captures config parameters, never pixel bytes (``pixel_data`` can be a
    whole ROM): applying re-runs the pipeline. The push site pre-validates by
    loading once; that result rides in ``preloaded`` and is consumed by the
    first ``redo()``, so pushing never double-loads and a doomed config never
    lands on the stack.
    """

    def __init__(
        self,
        window: MainWindow,
        entry: Entry,
        text: str,
        *,
        before: tuple[str, int],
        after: tuple[str, int],
        preloaded: pipeline.PixelData | None = None,
    ) -> None:
        super().__init__(window, entry, text, before, after)  # (preset_id, position)
        self._preloaded = preloaded
        self._pending: pipeline.PixelData | None = None

    def redo(self) -> None:
        # Handed over here rather than read in _apply so it can only ever reach
        # the *first* redo; a later one re-runs the pipeline as an undo does.
        self._pending, self._preloaded = self._preloaded, None
        super().redo()

    def _apply(self, state: tuple[str, int]) -> None:
        preloaded, self._pending = self._pending, None
        self._window._apply_pixel_config(*state, preloaded=preloaded)


class PaletteCommand(_CurrentEntryCommand):
    """Any palette-source change, as a before/after :class:`PaletteState` pair.

    One class serves every push site (format switch, default/file/offset mode
    changes) — the sites differ only in how they compute the after state and
    in the label they pass as ``text``.
    """

    def __init__(
        self,
        window: MainWindow,
        entry: Entry,
        text: str,
        *,
        before: PaletteState,
        after: PaletteState,
    ) -> None:
        super().__init__(window, entry, text, before, after)

    def _apply(self, state: PaletteState) -> None:
        self._window._apply_palette_state(state)


class ColorEditCommand(QUndoCommand):
    """One palette entry's color changing, as a before/after ARGB pair.

    ``owner`` is the entry whose palette dirt this edit belongs to, and ``doc`` the
    document that holds the palette: for a *file* palette that is the PALETTE entry
    and its own document (the graphic only mirrors it); for offset/custom it is the
    graphic itself. Capturing both keeps the edit anchored to the palette it changed
    even after the view moves to a different graphic sharing (or not sharing) it.

    Only the edited entry is captured, not the whole palette: consecutive edits to
    the *same* entry merge, so dragging a channel slider — which emits on every
    step — collapses into a single undo step rather than flooding the stack. A
    different entry (or any other command) breaks the run, exactly as it does for
    :class:`OffsetMoveCommand`.

    Forking a Custom palette off a read-only source is *not* part of this command:
    the window pushes that separately as a :class:`PaletteCommand` first, so undo
    peels the edit and the fork apart in the order they happened.
    """

    def __init__(
        self,
        window: MainWindow,
        owner: Entry,
        doc: Document,
        index: int,
        *,
        before: int,
        after: int,
        pixel_owner: Entry | None = None,
    ) -> None:
        super().__init__(f"edit color {index}")
        self._window = window
        self._owner = owner
        self._doc = doc
        self._index = index
        self._before = before
        self._after = after
        # The palette pathway's revision on either side of this command, so an
        # undo hands the owner back the exact unsaved-state it had before.
        self._before_revision = owner.palette_revision
        self._after_revision = window._workspace.next_revision()
        # A buffer-backed Offset palette persists through the *pixel* pathway of
        # the entry whose buffer holds it (its own pathway can't write a span of
        # a permuted region) — so that entry's pixel revision is tokened on both
        # sides too, exactly as the palette revision is above.
        self._pixel_owner = pixel_owner
        self._before_pixel_revision = (
            pixel_owner.pixel_revision if pixel_owner is not None else 0
        )
        self._after_pixel_revision = (
            window._workspace.next_revision() if pixel_owner is not None else 0
        )

    def id(self) -> int:
        return COLOR_EDIT_ID

    def mergeWith(self, other: QUndoCommand) -> bool:
        if (
            not isinstance(other, ColorEditCommand)
            or other._owner is not self._owner
            or other._doc is not self._doc
            or other._index != self._index
        ):
            return False
        self._after = other._after
        self._after_revision = other._after_revision  # other's redo already ran
        self._after_pixel_revision = other._after_pixel_revision
        if self._after == self._before:
            # The run landed back on the original color — drop the empty step,
            # and with it the dirty mark the swallowed edits stamped on.
            self.setObsolete(True)
            self._window._workspace.set_palette_revision(
                self._owner, self._before_revision
            )
            if self._pixel_owner is not None:
                self._window._workspace.set_pixel_revision(
                    self._pixel_owner, self._before_pixel_revision
                )
        return True

    def redo(self) -> None:
        self._apply(self._after, self._after_revision, self._after_pixel_revision)

    def undo(self) -> None:
        self._apply(self._before, self._before_revision, self._before_pixel_revision)

    def _apply(self, argb: int, revision: int, pixel_revision: int) -> None:
        with self._window._undo_apply():
            # A PALETTE entry can never be current, so a file-palette edit applies
            # without switching the view; a graphic-owned edit first returns to the
            # graphic it happened on, as every document-scoped command does.
            if self._owner.kind is EntryKind.PALETTE or self._window._ensure_current(
                self._owner
            ):
                self._window._apply_color_edit(
                    self._owner,
                    self._doc,
                    self._index,
                    argb,
                    revision,
                    pixel_owner=self._pixel_owner,
                    pixel_revision=pixel_revision,
                )


class PixelEditCommand(_EditModeCommand):
    """One pixel edit, as the before/after bytes of the regions it rewrote.

    Every graphics edit (paste, cut, clear, the drawing tools) lands as byte
    splices into the document's decompressed pixel data, so one command covers
    them all: the push site encodes whatever tiles it wants through the codec and
    hands over the resulting regions.

    Bytes rather than tiles, because bytes are the document's source of truth and
    a codec round-trips *pixels*, not bytes — re-encoding on undo could hand back
    something merely equivalent. Regions are bounded by the edited runs, so the
    snapshots stay small even on a multi-megabyte ROM.

    Several regions rather than one because a **rearranged** view
    (``tile_rearrangement``)
    scatters one gesture's tiles across the file: a stroke over what looks like
    four neighbouring tiles can be four splices far apart. It is still one
    interaction and so must still be one Ctrl+Z, which is why the list lives
    inside a single command rather than becoming several.
    """

    def __init__(
        self,
        window: MainWindow,
        entry: Entry,
        text: str,
        *,
        regions: list[tuple[int, bytes, bytes]],  # (start, before, after), disjoint
        through: Entry | None = None,
    ) -> None:
        # ``entry`` is whose bytes these are; ``through`` is where they were
        # edited, and differs only for a pixel edit made on a **tilemap** — the
        # map borrows its art from the bound entry, so the dirt and the save
        # belong to that entry while the picture belongs to the map.
        #
        # The data pathway's revision on either side of this command, so an
        # undo hands the entry back the exact unsaved-state it had before.
        #
        # A slice's bytes live inside its parent's region, so an edit to one is
        # an edit to that file: it is folded into the parent's buffer as it
        # lands (``_propagate_pixel_edit``) and the file carries the unsaved
        # state too. That revision therefore has to travel through undo as well,
        # exactly as a color edit inside a reordered region carries its owner's
        # (:class:`ColorEditCommand`). The *after* token is shared - one edit,
        # one state - while the *before* pair differs, since the parent may have
        # been at an unsaved state of its own before this command.
        owner = (
            window._workspace.find_file(entry.path)
            if entry.kind is EntryKind.SLICE
            else None
        )
        after_revision = window._workspace.next_revision()
        super().__init__(
            window,
            entry,
            text,
            (
                [(start, before) for start, before, _after in regions],
                entry.pixel_revision,
                owner.pixel_revision if owner is not None else 0,
            ),
            (
                [(start, after) for start, _before, after in regions],
                after_revision,
                after_revision,
            ),
            through=through,
        )

    def _apply(self, state: tuple[list[tuple[int, bytes]], int, int]) -> None:
        """Land every splice, as one refresh.

        The regions are disjoint (the push site merges anything that touches),
        so the order they land in doesn't matter — but the view must not be
        rebuilt between them, or a multi-region edit would flicker through
        half-applied states.

        The entry travels with the command rather than being read off the window:
        an edit made through a tilemap lands in a *different* entry than the one
        on screen, and one made in another entry lands after the reach above has
        switched to it — so "whose bytes" must not be re-asked at apply time.
        """
        self._window._apply_pixel_bytes(*state, entry=self._entry)


@dataclass(frozen=True)
class FloatState:
    """Pixels lifted off the page but not yet set down.

    A floating selection writes nothing until it lands, so the history can put
    one back simply by handing the grid — and the hole its move still owes
    (``source``; ``None`` for a paste, which removed nothing) — to the window
    again. Where it hovers is the selection rectangle the command already
    carries. The grid is held by reference, which is safe because the float is
    never mutated in place: a transform replaces it with a new grid.
    """

    grid: object
    source: QRect | None = None


class PixelSelectionCommand(_EditModeCommand):
    """One pixel-mode interaction that rewrote no bytes, as its before/after
    selection.

    Making, replacing, moving or dropping a pixel selection is a user action the
    history should step through, so it lands as its own command even though the
    document is untouched. The same class absorbs a *painting* gesture that
    happened to change nothing — both ends are then identical, and undo simply
    steps past it — so every pixel interaction costs exactly one step whether or
    not it moved a pixel.

    ``None`` on either end means "no selection". The rectangle is in image-pixel
    coordinates of the view window, like the live marquee it restores; a
    :class:`FloatState` alongside it says those pixels were *in the air* at that
    point, which is a selection state like any other because a float is written
    only when it lands.
    """

    def __init__(
        self,
        window: MainWindow,
        entry: Entry,
        text: str,
        *,
        before: QRect | None,
        after: QRect | None,
        before_float: FloatState | None = None,
        after_float: FloatState | None = None,
    ) -> None:
        # The rectangles are copied: the caller's are the live marquee, which
        # moves on. Each travels paired with the float that was in the air at
        # that point, since a float is a selection state like any other.
        super().__init__(
            window,
            entry,
            text,
            (None if before is None else QRect(before), before_float),
            (None if after is None else QRect(after), after_float),
        )

    def _apply(self, state: tuple[QRect | None, FloatState | None]) -> None:
        self._window._apply_marquee(*state)


class RenameEntryCommand(_InPlaceCommand):
    """Rename of an entry — the change is visible in the files panel wherever
    you are, so the view does not move for it."""

    def __init__(
        self, window: MainWindow, entry: Entry, before: str, after: str
    ) -> None:
        super().__init__(window, entry, f'rename to "{after}"', before, after)

    def _apply(self, state: str) -> None:
        self._window._apply_entry_name(self._entry, state)


class SliceEditCommand(_InPlaceCommand):
    """Re-pointing a slice's coordinates (offset/length/codec/name).

    Undo restores the *coordinates* and re-reads the region — it cannot
    resurrect unsaved edits that were discarded when the document was
    dropped (the edit dialog warns before discarding them). Applied in
    place; a non-current slice reloads on its next activation.
    """

    def __init__(
        self,
        window: MainWindow,
        entry: Entry,
        *,
        before: SliceParams,
        after: SliceParams,
    ) -> None:
        super().__init__(window, entry, f'edit slice "{after.name}"', before, after)

    def _apply(self, state: SliceParams) -> None:
        self._window._apply_slice_params(self._entry, state)


class ContainerEditCommand(_InPlaceCommand):
    """Re-pointing a file's file list, container and reshape (Edit File
    Container…).

    The three settle together because they decide the same thing between them —
    which bytes the region even has — so one command carries all three, and undo
    puts the whole :class:`~celpix.ui.container_dialog.ContainerEdit` back and
    re-reads. Like :class:`SliceEditCommand` it restores the *coordinates*, not
    unsaved edits discarded when the documents were dropped (the dialog confirms
    before discarding them). A re-pointed file's slices and bookmarks move with
    it in both directions, since they are keyed by its first file.
    """

    def __init__(
        self,
        window: MainWindow,
        entry: Entry,
        *,
        before: ContainerEdit,
        after: ContainerEdit,
    ) -> None:
        super().__init__(window, entry, f'edit container "{entry.name}"', before, after)

    def _apply(self, state: ContainerEdit) -> None:
        self._window._apply_container_edit(self._entry, state)


class MoveEntryCommand(_InPlaceCommand):
    """Reordering a file in the files pane — one place up or down.

    A single step is its own inverse, so the two directions are the same move
    with the sign flipped.
    """

    def __init__(self, window: MainWindow, entry: Entry, delta: int) -> None:
        super().__init__(
            window,
            entry,
            f'move "{entry.name}" {"up" if delta < 0 else "down"}',
            -delta,
            delta,
        )

    def _apply(self, state: int) -> None:
        self._window._apply_move_entry(self._entry, state)


class AddEntryCommand(QUndoCommand):
    """Adding one entry to the files pane: an opened file or a new slice.

    Holds the constructed :class:`Entry` itself — undo removes it from the
    workspace but keeps the object, so redo restores it identically (same
    document, session, and identity for every later command that targets it).
    """

    def __init__(self, window: MainWindow, entry: Entry, text: str) -> None:
        super().__init__(text)
        self._window = window
        self._entry = entry

    def redo(self) -> None:
        with self._window._undo_apply():
            self._window._apply_add_entry(self._entry)

    def undo(self) -> None:
        with self._window._undo_apply():
            self._window._apply_close_entry(self._entry)


class RemoveEntriesCommand(QUndoCommand):
    """Removing an entry — and, for a file, the slices that go with it.

    Captures the removed entries with their list positions plus which entry
    was current, so undo reinstates the files pane exactly (parents re-insert
    before their slices because they sit at lower indices).

    **Tile bindings need no capture.** A binding holds the bound entry itself
    (:class:`~celpix.project.workspace.TileSource`), so closing a tile bank leaves
    every map still holding it — resolving to "not open" meanwhile — and the
    re-insert below puts the same object back and the maps are bound again. That
    is the whole of what identity buys over a position: nothing to snapshot, and
    nothing that can be restored inconsistently.
    """

    def __init__(
        self,
        window: MainWindow,
        root: Entry,
        *,
        victims: list[tuple[int, Entry]],
        was_current: Entry | None,
    ) -> None:
        super().__init__(f'remove "{root.name}"')
        self._window = window
        self._root = root
        self._victims = victims
        self._was_current = was_current

    def redo(self) -> None:
        with self._window._undo_apply():
            self._window._apply_close_entry(self._root)

    def undo(self) -> None:
        with self._window._undo_apply():
            self._window._apply_restore_entries(self._victims, self._was_current)


@dataclass(frozen=True)
class PaletteConsumerLink:
    """A graphic's File-mode link to a palette, captured before it is re-homed.

    Removing a file palette that graphics use converts each to a Custom copy; this
    records exactly how to relink it on undo — its path/offset and format, and
    whether its document was loaded (a loaded graphic re-mirrors from the restored
    palette; an unloaded one just re-points its pending source).
    """

    entry: Entry
    path: str
    offset: int
    preset_id: str
    loaded: bool


class RemovePaletteWithConsumersCommand(QUndoCommand):
    """Remove a file palette that graphics use, re-homing each as a Custom copy.

    Deleting a shared palette would strand the graphics that render it, so each
    keeps the colors as its own Custom palette — project-stored, so this is a
    change to the *project*, never to the graphic's own bytes. Undo re-registers
    the palette at its old list position and relinks every graphic back to it.
    """

    def __init__(
        self,
        window: MainWindow,
        palette: Entry,
        *,
        index: int,
        consumers: list[PaletteConsumerLink],
    ) -> None:
        super().__init__(f'remove "{palette.name}"')
        self._window = window
        self._palette = palette
        self._index = index
        self._consumers = consumers

    def redo(self) -> None:
        with self._window._undo_apply():
            self._window._apply_remove_palette_to_custom(self._palette, self._consumers)

    def undo(self) -> None:
        with self._window._undo_apply():
            self._window._apply_restore_palette_consumers(
                self._palette, self._index, self._consumers
            )
