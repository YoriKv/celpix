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
  back to where that change happened.
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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtGui import QUndoCommand

from celpix.core.context import PipelineContext
from celpix.core.document import Document
from celpix.core.palette import Palette
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.project.workspace import Entry, EntryKind, PaletteMode, SliceParams

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
    # The bytes the palette was read from, and which entries have been edited
    # since — the splice base a save needs (see Document.palette_bytes). Carried
    # through undo so reverting a palette change restores the right base, not
    # just the right colors.
    data: bytes = b""
    edits: frozenset[int] = frozenset()


class OffsetMoveCommand(QUndoCommand):
    """One view-position move; consecutive moves in the same entry merge."""

    def __init__(
        self,
        window: MainWindow,
        entry: Entry,
        *,
        before: tuple[int, int],
        after: tuple[int, int],
    ) -> None:
        super().__init__("move view")
        self._window = window
        self._entry = entry
        self._before = before  # (offset, nudge)
        self._after = after

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

    def redo(self) -> None:
        with self._window._undo_apply():
            if self._window._ensure_current(self._entry):
                self._window._apply_offset(*self._after)

    def undo(self) -> None:
        with self._window._undo_apply():
            if self._window._ensure_current(self._entry):
                self._window._apply_offset(*self._before)


class PixelConfigCommand(QUndoCommand):
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
        before: tuple[str, int, int],
        after: tuple[str, int, int],
        preloaded: pipeline.PixelData | None = None,
    ) -> None:
        super().__init__(text)
        self._window = window
        self._entry = entry
        self._before = before  # (preset_id, header_offset, byte_position)
        self._after = after
        self._preloaded = preloaded

    def redo(self) -> None:
        preloaded, self._preloaded = self._preloaded, None
        with self._window._undo_apply():
            if self._window._ensure_current(self._entry):
                self._window._apply_pixel_config(*self._after, preloaded=preloaded)

    def undo(self) -> None:
        with self._window._undo_apply():
            if self._window._ensure_current(self._entry):
                self._window._apply_pixel_config(*self._before)


class PaletteCommand(QUndoCommand):
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
        super().__init__(text)
        self._window = window
        self._entry = entry
        self._before = before
        self._after = after

    def redo(self) -> None:
        with self._window._undo_apply():
            if self._window._ensure_current(self._entry):
                self._window._apply_palette_state(self._after)

    def undo(self) -> None:
        with self._window._undo_apply():
            if self._window._ensure_current(self._entry):
                self._window._apply_palette_state(self._before)


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
        if self._after == self._before:
            # The run landed back on the original color — drop the empty step,
            # and with it the dirty mark the swallowed edits stamped on.
            self.setObsolete(True)
            self._window._workspace.set_palette_revision(
                self._owner, self._before_revision
            )
        return True

    def redo(self) -> None:
        self._apply(self._after, self._after_revision)

    def undo(self) -> None:
        self._apply(self._before, self._before_revision)

    def _apply(self, argb: int, revision: int) -> None:
        with self._window._undo_apply():
            # A PALETTE entry can never be current, so a file-palette edit applies
            # without switching the view; a graphic-owned edit first returns to the
            # graphic it happened on, as every document-scoped command does.
            if self._owner.kind is EntryKind.PALETTE or self._window._ensure_current(
                self._owner
            ):
                self._window._apply_color_edit(
                    self._owner, self._doc, self._index, argb, revision
                )


class PixelEditCommand(QUndoCommand):
    """One pixel edit, as the before/after bytes of the region it rewrote.

    Every graphics edit (paste, cut, clear — later the drawing tools) lands as
    a byte splice into the document's decompressed pixel data, so one command
    covers them all: the push site encodes whatever tiles it wants through the
    codec and hands over the resulting region.

    Bytes rather than tiles, because bytes are the document's source of truth and
    a codec round-trips *pixels*, not bytes — re-encoding on undo could hand back
    something merely equivalent. Regions are bounded by the edited run, so the
    snapshots stay small even on a multi-megabyte ROM.
    """

    def __init__(
        self,
        window: MainWindow,
        entry: Entry,
        text: str,
        *,
        start: int,
        before: bytes,
        after: bytes,
    ) -> None:
        super().__init__(text)
        self._window = window
        self._entry = entry
        self._start = start
        self._before = before
        self._after = after
        # The data pathway's revision on either side of this command, so an
        # undo hands the entry back the exact unsaved-state it had before.
        self._before_revision = entry.pixel_revision
        self._after_revision = window._workspace.next_revision()

    def redo(self) -> None:
        with self._window._undo_apply():
            if self._window._ensure_current(self._entry):
                self._window._apply_pixel_bytes(
                    self._start, self._after, self._after_revision
                )

    def undo(self) -> None:
        with self._window._undo_apply():
            if self._window._ensure_current(self._entry):
                self._window._apply_pixel_bytes(
                    self._start, self._before, self._before_revision
                )


class RenameEntryCommand(QUndoCommand):
    """Rename of an entry — applied in place, without switching the view
    (the change is visible in the files panel wherever you are)."""

    def __init__(
        self, window: MainWindow, entry: Entry, before: str, after: str
    ) -> None:
        super().__init__(f'rename to "{after}"')
        self._window = window
        self._entry = entry
        self._before = before
        self._after = after

    def redo(self) -> None:
        with self._window._undo_apply():
            self._window._apply_entry_name(self._entry, self._after)

    def undo(self) -> None:
        with self._window._undo_apply():
            self._window._apply_entry_name(self._entry, self._before)


class SliceEditCommand(QUndoCommand):
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
        super().__init__(f'edit slice "{after.name}"')
        self._window = window
        self._entry = entry
        self._before = before
        self._after = after

    def redo(self) -> None:
        with self._window._undo_apply():
            self._window._apply_slice_params(self._entry, self._after)

    def undo(self) -> None:
        with self._window._undo_apply():
            self._window._apply_slice_params(self._entry, self._before)


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
class PaletteUserLink:
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


class RemovePaletteWithUsersCommand(QUndoCommand):
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
        users: list[PaletteUserLink],
    ) -> None:
        super().__init__(f'remove "{palette.name}"')
        self._window = window
        self._palette = palette
        self._index = index
        self._users = users

    def redo(self) -> None:
        with self._window._undo_apply():
            self._window._apply_remove_palette_to_custom(self._palette, self._users)

    def undo(self) -> None:
        with self._window._undo_apply():
            self._window._apply_restore_palette_users(
                self._palette, self._index, self._users
            )
