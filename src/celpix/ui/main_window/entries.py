"""The open-entries list: files coming onto it, and entries going off it.

The two ends of the files dock. At one, the plainest entry there is — creating a
file on disk (File ▸ New File…) or opening one at all, which is one entry
appended to the workspace and the view switched onto it, and the single funnel
behind File ▸ Open, a dropped file and the open-as prompt.

Removal closes the list off at the other end, and is the one path that has to
look outside the entry being removed: a file takes its slices and bookmarks with
it, and a **palette** other graphics are rendering with cannot simply vanish -
each user is re-homed onto a Custom copy of its colors first, as one undoable
step, so nothing is left pointing at a palette that is gone.

What lies between is its neighbours'. Entries carved out of or assembled from
others are :mod:`~celpix.ui.main_window.slices`'; the jumps back to a parent,
and bookmarks, :mod:`~celpix.ui.main_window.jumps`'; a file's container and
size, and the re-read after any re-point,
:mod:`~celpix.ui.main_window.containers`'; the project that holds the list,
:mod:`~celpix.ui.main_window.projects`'. Putting an entry's bytes *back on disk*
— and the file/slice reconciliation that makes a region safe to write — is
:mod:`~celpix.ui.main_window.writing`. What happens when the view *moves*
between these entries is :mod:`~celpix.ui.main_window.session`'s.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QMessageBox,
)

from celpix.core.capabilities import ContentKind
from celpix.core.document import ViewOptions
from celpix.core.errors import PipelineError, Stage
from celpix.pipeline import pipeline
from celpix.plugins.detect import (
    content_kind_for,
    detect_container,
    tilemap_preset_for,
)
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    palette_source_for,
)
from celpix.ui.new_file_dialog import NewFileDialog, NewFileParams
from celpix.ui.undo_commands import (
    AddEntryCommand,
    PaletteConsumerLink,
    RemoveEntriesCommand,
    RemovePaletteWithConsumersCommand,
)
from celpix.ui.widgets import (
    ask_save_path,
)


class EntriesMixin:
    """Creating and opening files, and removing entries from the list.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    # -- creating a file -----------------------------------------------------
    # What the save picker offers for each kind, when the container has no
    # extension of its own to suggest. A palette's ``.pal`` is the conventional
    # name for a file that is nothing but colours; the other two are raw payloads
    # with no convention at all, so ``.bin`` says exactly that.
    _NEW_FILTERS = {
        ContentKind.PIXELS: ("Binary files (*.bin);;All files (*)", ".bin"),
        ContentKind.TILEMAP: ("Binary files (*.bin);;All files (*)", ".bin"),
        ContentKind.PALETTE: ("Palette files (*.pal *.col);;All files (*)", ".pal"),
    }

    def _new_file(self) -> None:
        """File ▸ New File… — create a blank file on disk and open it as an entry.

        The one gesture that does not start from somebody else's bytes. It runs
        in the order the answers depend on each other: the dialog settles *what*
        is being made (content, container, codec, size), the save picker settles
        *where*, and only then is anything written — so a cancel at either step
        has left no file behind.

        The file is written before the entry is *added* because an entry is a
        reference to a path, and everything downstream of one — the load, a save,
        the project file — is written against a file that is there. It is built
        first, though: the pathway the file is written through is the one the
        entry will read it through, and the one place that says what that is
        (:meth:`~...containers.ContainersMixin._file_config`) answers for an
        entry — the same answer a resize of the file gets. The entry carries
        exactly the answers the dialog gave rather than re-detecting them:
        detection reads a signature, and a blank payload has none to read.

        Undo removes the entry, as it does for an opened file. The file itself
        stays on disk — deleting a user's file is not something an undo of "add
        a row to a list" should do, and redo simply opens it again.
        """
        params = NewFileDialog.get_params(
            self,
            self._registry,
            pixel_preset_id=self._pixel_preset_id(),
            palette_preset_id=self._palette_preset_id(),
        )
        if params is None:
            return
        path = self._ask_new_file_path(params)
        if path is None:
            return
        # A file already open would be blanked under the entry editing it, which
        # no amount of undo puts back — the same refusal relocating onto an open
        # path makes, and for the same reason.
        if self._workspace.find_file(path) or self._workspace.find_palette(path):
            self._alert(
                f"{Path(path).name} is already open in this project. "
                "Close it first, or create the new file under another name.",
                title="celPix - new file",
            )
            return
        entry = self._new_file_entry(path, params)
        try:
            size = pipeline.create_file(
                self._file_config(entry, params.codec_id),
                kind=params.content_kind,
                codec_id=params.codec_id,
                units=params.units,
                reg=self._registry,
            )
        except PipelineError as exc:
            self._report(exc)
            return
        except (OSError, ValueError) as exc:
            self._alert(f"Cannot write {path}: {exc}", title="celPix - new file")
            return
        self._push_command(AddEntryCommand(self, entry, f"new file {entry.name}"))
        self.statusBar().showMessage(
            f"Created {entry.name} - {self._new_file_extent(params)}, {size:,} bytes"
        )

    def _ask_new_file_path(self, params: NewFileParams) -> str | None:
        """Where the new file goes, defaulting to the container's own extension.

        A container that claims a suffix is claiming it for files of its format,
        and this is about to write one, so its first extension is the right
        default — that is also what lets the file be *re-detected* as that format
        when it is opened again from disk in another session.
        """
        info = self._registry.plugin(Stage.CONTAINER, params.stages.container_id).info
        file_filter, suffix = self._NEW_FILTERS[params.content_kind]
        if info.extensions:
            suffix = info.extensions[0]
            file_filter = f"{info.name} (*{suffix});;All files (*)"
        return ask_save_path(
            self,
            "New file",
            str(Path(self._export_dir(self._workspace.current)) / f"untitled{suffix}"),
            file_filter,
            suffix,
        )

    def _new_file_entry(self, path: str, params: NewFileParams) -> Entry:
        """The workspace entry for a file just created with ``params``.

        Every answer the dialog gave is stamped on rather than re-derived: the
        stages go on the entry as Edit File Container… would put them, the
        codec on the session (or, for a map, on the entry's own cell format
        and, for a palette file, on its recorded import format), and the size
        on the pending view so the sheet opens at the shape it was asked for
        instead of at the window's default 16x16.
        """
        name = Path(path).name
        if params.content_kind is ContentKind.PALETTE:
            # A palette entry is registered, never activated: it has no session
            # and no view of its own, and the codec it was written with is the
            # one it must be read back with (``docs/design/project-format.md`` §4).
            entry = Entry(
                name=name,
                kind=EntryKind.PALETTE,
                path=path,
                palette_preset_id=params.codec_id,
            )
            entry.set_file_stages(params.stages)
            return entry
        tilemap = params.content_kind is ContentKind.TILEMAP
        entry = Entry(
            name=name,
            kind=EntryKind.FILE,
            path=path,
            content_kind=params.content_kind,
            tilemap_preset_id=params.codec_id if tilemap else None,
        )
        entry.set_file_stages(params.stages)
        entry.session = EntrySession(
            pixel_preset_id=(self._pixel_preset_id() if tilemap else params.codec_id),
            palette_preset_id=self._palette_preset_id(),
            palette_view_preset_id=self._palette_view_preset_id(),
        )
        entry.pending_view = ViewOptions(columns=params.columns, rows=params.rows)
        return entry

    @staticmethod
    def _new_file_extent(params: NewFileParams) -> str:
        """The size as the dialog stated it, for the status line."""
        if params.content_kind is ContentKind.PALETTE:
            return f"{params.units} colors"
        noun = "cells" if params.content_kind is ContentKind.TILEMAP else "tiles"
        return f"{params.columns}x{params.rows} {noun}"

    # -- opening a file ------------------------------------------------------
    def _open_pixel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open pixel data")
        if path:
            self._load_pixel(path, content_kind=ContentKind.PIXELS, inherit=True)

    def _open_tilemap(self) -> None:
        """File ▸ Open tilemap data — read any file as a map of tile indices.

        The tilemap twin of Open pixel data, and forcing in the same way: a file
        whose signature says nothing (a region of a ROM, a raw dump) has no way
        to be recognised as a map, so asking for one is how it is said. A file
        that *is* a known tilemap format opens the same either way.
        """
        path, _ = QFileDialog.getOpenFileName(self, "Open tilemap data")
        if path:
            self._load_pixel(path, content_kind=ContentKind.TILEMAP, inherit=True)

    def _ask_content_kind(self, path: str) -> ContentKind | None:
        """Which of the three readings to open ``path`` as, or None if cancelled —
        the Ctrl-drop's question. Detection is a guess from a signature and a
        suffix, and silent about being one; holding Ctrl is how the user says
        they know better."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("celPix - open as")
        box.setText(f"Open {Path(path).name} as:")
        box.setInformativeText(
            "Pixels are tile graphics, a palette is colors, and a tilemap is\n"
            "indices into tiles that live somewhere else."
        )
        role = QMessageBox.ButtonRole.ActionRole
        buttons = {
            box.addButton("&Pixels", role): ContentKind.PIXELS,
            box.addButton("Pa&lette", role): ContentKind.PALETTE,
            box.addButton("&Tilemap", role): ContentKind.TILEMAP,
        }
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return buttons.get(box.clickedButton())

    def _load_pixel(
        self,
        path: str,
        *,
        content_kind: ContentKind | None = None,
        inherit: bool = False,
    ) -> None:
        """Open ``path`` as a workspace entry and switch the view to it.

        The shared entry point for both File ▸ Open and drag-and-drop, so a
        dropped file behaves exactly like an opened one. A file that is
        already open activates its existing entry - identity is the path -
        so only a genuinely new entry becomes an undoable step.

        The container is picked here, once, from the file's name and leading
        bytes: it is a property of the file, so detecting it at open time means
        every later load reads through the same one, and the answer is on the
        entry where the user can see and change it.

        ``content_kind`` overrides what the container implies, for the gestures
        that *say* what a file is — File ▸ Open pixel/tilemap data, and the
        open-as prompt. Detection can only recognise a format it knows, so a raw
        region of a ROM has no way to announce itself as a map; asking is how
        that is said. ``None`` keeps the container's own answer.

        ``inherit`` is for the gestures where the *user* opens a file — File ▸
        Open and a drop: the file then reads the way the entry on screen does,
        where both are the same kind (:meth:`~...view_settings.ViewSettingsMixin.
        _inherit_view_settings`). A file opened on the way to something else — a
        map's tile source, a bookmark's file — is read as itself.
        """
        existing = self._workspace.find_file(path)
        if existing is not None:
            self._activate_entry(existing)
            return
        container_id = detect_container(self._registry, path)
        # Follows from the container, which was itself chosen from the file's
        # signature — so a screen or panel file opens into the Tilemaps section
        # without being asked about — unless the caller said otherwise.
        detected = content_kind_for(self._registry, container_id)
        entry = Entry(
            name=Path(path).name,
            kind=EntryKind.FILE,
            path=path,
            container_id=container_id,
            content_kind=content_kind or detected,
            tilemap_preset_id=tilemap_preset_for(self._registry, container_id) or None,
        )
        if inherit:
            self._inherit_view_settings(entry)
        self._push_command(AddEntryCommand(self, entry, f"open {entry.name}"))

    # -- removal -------------------------------------------------------------
    def _remove_entry(self, entry: Entry, *, confirm: bool = True) -> None:
        """Remove one entry — :meth:`_remove_entries` for a list of one."""
        self._remove_entries([entry], confirm=confirm)

    def _remove_entries(self, entries: list[Entry], *, confirm: bool = True) -> None:
        """Remove every entry in ``entries`` (a row takes everything nested
        under it along), confirming once for the lot - Remove is also on the
        Delete key, and a slip there costs each entry's whole session setup.

        ``confirm=False`` is **Cut**, which has already said where the row is
        going: it is on the clipboard before this runs, so the question the
        prompt asks has an answer the gesture itself gave. A palette that graphics
        render still asks either way — re-homing them is a change to *those*
        entries, which no clipboard holds.

        Several rows are removed as a **macro** over the one-entry commands rather
        than by one command that knows about lists. Each is built and pushed in
        turn, so each captures the list positions it is actually removing from,
        and undo unwinds them in reverse and puts every row back where it was.
        A palette in the set keeps its own command, which is what carries the
        re-homing; the macro is the only thing that has to know they belong
        together.
        """
        roots = self._removal_roots(entries)
        if not roots:
            return
        # The current graphic's palette mode is only written to its session on a
        # switch, so snapshot it first - otherwise a palette in use *right now*
        # looks unused and would be dropped without re-homing it.
        if any(root.kind is EntryKind.PALETTE for root in roots):
            self._capture_session()
        going = {
            e for root in roots for e in (root, *self._workspace.descendants_of(root))
        }
        # Only the graphics that are *staying* need re-homing: one being removed
        # in the same gesture would be re-pointed at a custom palette on its way
        # out of the project.
        rehomed: dict[Entry, list[Entry]] = {}
        for root in roots:
            if root.kind is not EntryKind.PALETTE:
                continue
            staying = [
                c for c in self._workspace.palette_consumers(root) if c not in going
            ]
            if staying:
                rehomed[root] = staying
        if not self._confirm_removal(roots, rehomed, confirm=confirm):
            return
        if len(roots) == 1:
            self._push_removal(roots[0], rehomed.get(roots[0]))
            return
        self._undo_stack.beginMacro(f"remove {len(roots)} entries")
        try:
            for root in roots:
                self._push_removal(root, rehomed.get(root))
        finally:
            self._undo_stack.endMacro()

    def _removal_roots(self, entries: list[Entry]) -> list[Entry]:
        """``entries`` in list order, minus every row a selected *ancestor*
        already takes with it — a file picked along with two of its own slices is
        one removal, not three, and so is a slice picked with one nested two
        levels under it.
        """
        chosen = set(entries)
        return [
            entry
            for entry in self._workspace.entries
            if entry in chosen
            and not any(a in chosen for a in self._workspace.ancestors_of(entry))
        ]

    def _confirm_removal(
        self,
        roots: list[Entry],
        rehomed: dict[Entry, list[Entry]],
        *,
        confirm: bool,
    ) -> bool:
        """Ask before removing ``roots``; True to go ahead.

        One prompt however many rows are going, naming what travels with them:
        the slices and bookmarks a file takes and the slices nested in a slice,
        to any depth, the unsaved edits that are
        discarded, and the graphics a palette leaves needing colors of their own.

        A palette with consumers is asked about **even when ``confirm`` is
        False**: the caller that skips the question is Cut, and re-homing a
        graphic is a change to that graphic, which the clipboard is not holding.
        """
        victims = [(root, self._workspace.descendants_of(root)) for root in roots]
        if len(roots) == 1:
            root = roots[0]
            message = f"Remove {root.name}?"
        else:
            names = ", ".join(root.name for root in roots)
            message = f"Remove {len(roots)} entries ({names})?"
        parts = []
        counts = [
            f"{n} {label}(s)"
            for label, n in (
                ("slice", self._kind_count(victims, EntryKind.SLICE)),
                ("bookmark", self._kind_count(victims, EntryKind.BOOKMARK)),
            )
            if n
        ]
        if counts:
            whose = "its " if len(roots) == 1 else ""
            parts.append(f"removes {whose}{' and '.join(counts)}")
        dirty = [
            e.name
            for root, children in victims
            for e in (root, *children)
            if e.pixel_dirty or e.palette_dirty
        ]
        if dirty:
            parts.append(f"discards unsaved changes ({', '.join(dirty)})")
        users = sorted({c.name for consumers in rehomed.values() for c in consumers})
        if users:
            parts.append(
                f"leaves {len(users)} graphic(s) ({', '.join(users)}) keeping "
                "these colors as their own custom palette, stored in the project"
            )
        if parts:
            message += " This also " + " and ".join(parts) + "."
        if not confirm and not rehomed:
            return True
        answer = QMessageBox.question(self, "celPix - remove", message)
        return answer == QMessageBox.StandardButton.Yes

    @staticmethod
    def _kind_count(victims: list[tuple[Entry, list[Entry]]], kind: EntryKind) -> int:
        """How many rows of ``kind`` come along *under* what is going, at any
        depth."""
        return sum(e.kind is kind for _root, children in victims for e in children)

    def _push_removal(self, entry: Entry, rehomed: list[Entry] | None) -> None:
        """Push the command that takes ``entry`` out — no questions asked.

        ``rehomed`` is the graphics still rendering it, for a file palette; each
        keeps its colors as a Custom copy so none is left showing a palette that
        is gone, and the whole thing is one undo step.
        """
        victims = [entry, *self._workspace.descendants_of(entry)]
        entries = self._workspace.entries
        positions = [(entries.index(e), e) for e in victims]
        if rehomed:
            self._push_command(
                RemovePaletteWithConsumersCommand(
                    self,
                    entry,
                    victims=positions,
                    was_current=self._workspace.current,
                    consumers=[self._consumer_link(entry, c) for c in rehomed],
                )
            )
            return
        self._push_command(
            RemoveEntriesCommand(
                self,
                entry,
                victims=positions,
                was_current=self._workspace.current,
            )
        )

    def _consumer_link(self, palette: Entry, consumer: Entry) -> PaletteConsumerLink:
        """``consumer``'s File-mode link to ``palette``, captured before the
        re-home so undo can point it back at the palette it had."""
        src = palette_source_for(consumer)
        return PaletteConsumerLink(
            entry=consumer,
            path=src.path if src and src.path else palette.path,
            offset=src.offset if src else 0,
            preset_id=(
                consumer.session.palette_preset_id
                if consumer.session is not None
                else self._palette_preset_id()
            ),
            loaded=consumer.doc is not None,
        )

    def _apply_remove_palette_to_custom(
        self, palette: Entry, consumers: list[PaletteConsumerLink]
    ) -> None:
        """Freeze the palette's colors into each graphic as a Custom copy, then
        drop the palette - :class:`RemovePaletteWithConsumersCommand`'s redo."""
        colors = self._file_palette_colors(palette)
        preset = palette.palette_preset_id or self._palette_preset_id()
        for link in consumers:
            self._convert_graphic_to_custom(link.entry, colors, preset)
        # The ordinary close, slices and all: a palette's slices are pieces and
        # palette sources in their own right, and what reads them degrades the
        # way it does for any closed entry.
        self._apply_close_entry(palette)
        self._reshow_current_entry()

    def _apply_rehome_palette_consumers(
        self, palette: Entry, consumers: list[PaletteConsumerLink]
    ) -> None:
        """Freeze the palette's colours into each graphic as a Custom copy,
        leaving the palette in place - :class:`RehomePaletteConsumersCommand`'s
        redo, and the first half of :meth:`_apply_remove_palette_to_custom`."""
        colors = self._file_palette_colors(palette)
        preset = palette.palette_preset_id or self._palette_preset_id()
        for link in consumers:
            self._convert_graphic_to_custom(link.entry, colors, preset)
        self._reshow_current_entry()

    def _apply_relink_palette_consumers(
        self, consumers: list[PaletteConsumerLink]
    ) -> None:
        """Point every graphic back at the palette it had - the command's undo."""
        for link in consumers:
            self._relink_graphic_to_file_palette(link)
        self._reshow_current_entry()

    def _apply_restore_palette_consumers(
        self,
        victims: list[tuple[int, Entry]],
        was_current: Entry | None,
        consumers: list[PaletteConsumerLink],
    ) -> None:
        """Re-register the palette (and its slices) and relink every graphic -
        the command's undo."""
        self._apply_restore_entries(victims, was_current)
        for link in consumers:
            self._relink_graphic_to_file_palette(link)
        self._reshow_current_entry()

    def _reshow_current_entry(self) -> None:
        """Re-apply the current entry's (possibly changed) palette to the dock and
        canvas after a re-home, so the on-screen mode/label follow the entry."""
        current = self._workspace.current
        if current is not None and current.doc is not None:
            self._restore_session(current)
        # A re-home runs whatever is current, and that can be an entry that is
        # unavailable (its file gone, its load failed) or nothing at all - no
        # document to render, only the dock, which reads the palette alone.
        if self._doc is not None:
            self._refresh_view()
        else:
            self._refresh_palette_dock()
