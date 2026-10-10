"""The Files dock's context menu: what a right-click on a row offers.

Every row is one of five kinds and each builds its own menu, in the same groups
top to bottom (:meth:`EntryMenuMixin._show_menu`); the rows the kinds share are
built once each by the ``_add_*`` builders, so two kinds cannot disagree about
one action's label, key or rule for being live. The menu never acts: every row
emits one of the panel's signals with the right-clicked entry, and the window
answers.

Mixed into :class:`~celpix.ui.file_list_panel.FileListPanel`, whose signals,
selection and ``_current`` it reads.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMenu

from celpix.core.address import format_hex
from celpix.core.capabilities import Capability, ContentKind
from celpix.project.workspace import (
    Entry,
    EntryKind,
    SortKey,
    can_supply_palette,
    is_composable,
)
from celpix.ui import clipboard
from celpix.ui.entry_tree import DUPLICATE_KEY

__all__ = ["EntryMenuMixin"]


class EntryMenuMixin:
    """The context-menu half of the Files dock; see the module docs."""

    @staticmethod
    def _entry_action(
        menu: QMenu,
        text: str,
        handler,  # noqa: ANN001 — Callable, usually a signal's ``emit``
        *args: object,
        enabled: bool = True,
        shortcut=None,  # noqa: ANN001 — QKeySequence | StandardKey
    ) -> QAction:
        """One row of an entry's context menu: label, what it does, whether it is
        live.

        Every row in :meth:`_show_menu` is the same three statements around a
        signal carrying the right-clicked entry, so a call site says only which
        signal and with what — ``handler`` being that signal's ``emit`` (or, for
        the rows this panel answers itself, the method). The arguments are passed
        here rather than closed over at the call site because a lambda built in a
        loop-free run of twenty rows is exactly where a captured name goes stale.

        A ``shortcut`` here is always **display-only**: a closed menu's action
        never fires, so it labels the key in the shortcut column while the working
        binding lives elsewhere (the tree's own key handling, or the File menu's
        row for the current entry).

        Not :func:`~celpix.ui.widgets.make_action`, whose ``slot`` takes the
        signal's own arguments: a row here always carries its entry.
        """
        action = menu.addAction(text)
        action.triggered.connect(lambda: handler(*args))
        if shortcut is not None:
            action.setShortcut(shortcut)
        action.setEnabled(enabled)
        # Returned so a caller can name it to :meth:`_only_these_live` — which is
        # the only reason, so most call sites drop it.
        return action

    def _add_rename_action(self, menu: QMenu, entry: Entry) -> None:
        """Rename…, on every kind of row — the tree's inline editor, opened here."""
        self._entry_action(menu, "Re&name…", self._begin_rename, entry)

    def _add_new_bookmark_action(
        self, menu: QMenu, entry: Entry, *, sliceable: bool
    ) -> None:
        """New Bookmark, on a file's or a palette's row, live where the row is on
        screen (the view's offset is what it records).

        "k" rather than the File menu's "B", which Sort by holds here: the letters
        only have to be unique within one menu, and every other one in "Sort by"
        is spoken for on these rows (S by New Slice, o by Move Up, r by Remove, t
        by Cut, y by Copy).
        """
        self._entry_action(
            menu,
            "New Boo&kmark",
            self.new_bookmark_requested.emit,
            entry,
            enabled=sliceable,
        )

    def _add_edit_container_action(self, menu: QMenu, entry: Entry) -> None:
        """Edit File Container…, on the two kinds with a container of their own.

        Always offered, and needs no document: correcting the container is
        exactly what a file that failed to make sense needs — and a palette whose
        colors stop before its bytes do needs one as much, detection being as
        wrong there as anywhere. Its shortcut is display-only: the working binding
        is the File menu's action, which acts on the *current* entry — a file or
        a palette file alike — rather than the right-clicked one.
        """
        self._entry_action(
            menu,
            "&Edit File Container…",
            self.change_container_requested.emit,
            entry,
            shortcut=QKeySequence("Ctrl+E"),
        )

    def _add_container_info_action(self, menu: QMenu, entry: Entry) -> None:
        """What the container read, under the action that chooses which one.

        On both kinds that have a container of their own, from one builder so the
        two cannot drift apart. Needs no document and no successful load — a file
        that came out looking wrong is exactly when the report is worth reading,
        which is why it is always offered.
        """
        # "C" rather than the File menu's "I", which "Import from PNG…" holds
        # here — a mnemonic only has to be unique within the menu it appears in.
        self._entry_action(
            menu, "&Container Info…", self.container_info_requested.emit, entry
        )

    def _add_inputs_actions(self, menu: QMenu, entry: Entry) -> QAction:
        """Inputs…, Copy Inputs and Paste Inputs, under the row that chooses the
        codec they belong to.

        Inputs… is live only where a format on the entry declares something to
        bind; Copy only where the entry binds something; Paste only with a copy
        on the clipboard, and onto every selected row the right-clicked one is
        among — forty slices sharing one table is the case this exists for.
        """
        self._entry_action(
            menu,
            "Inp&uts…",
            self.inputs_requested.emit,
            entry,
            enabled=self._inputs_probe(entry),
        )
        self._entry_action(
            menu,
            "Copy Inputs",
            self.copy_inputs_requested.emit,
            entry,
            enabled=bool(entry.inputs),
        )
        selected = self.selected_entries()
        targets = selected if entry in selected else [entry]
        # Returned so the multi-row gate can spare it: it is the one row here
        # that is *about* several entries rather than about the clicked one.
        return self._entry_action(
            menu,
            "Paste Inputs",
            self.paste_inputs_requested.emit,
            targets,
            enabled=clipboard.has_inputs()
            and any(self._inputs_probe(e) for e in targets),
        )

    def _add_write_action(self, menu: QMenu, entry: Entry) -> None:
        """Write, sitting under the entry's own settings (a file's container, a
        slice's definition, a composite's piece list) rather than down by the
        import/export group: it is what commits the edits those dialogs and the
        canvas make. One builder, so the rule for when each kind's Write is live
        is written in one place.

        A file or a slice needs a loaded, write-capable document: a
        never-activated or view-only entry has nothing to write. A **palette
        file** owns its colours and writes them back to its own file, whichever
        way they were edited — through the dock, or by painting its swatches —
        so it is live exactly when a write would put something down: loaded, and
        carrying unsaved edits. A **composite** is asked for the document only.
        Its ``write_enabled`` is False about the assembled buffer, which is
        nobody's file, while the edits made through it
        were deposited in the pieces — and those are what the handler writes, so
        reading the buffer's flag here would grey out the very row that sends
        them home. Nor is it gated on a piece being dirty: a file and a slice
        stay live whether or not they have unsaved edits, and a row that came and
        went with the state of entries it doesn't show would be a rule the user
        can't see. The handler answers instead, naming the pieces that landed or
        saying that none of them had anything to write.
        """
        if entry.doc is None:
            live = False
        elif entry.kind is EntryKind.PALETTE:
            live = entry.pixel_dirty or entry.palette_dirty
        else:
            live = (
                entry.kind is EntryKind.COMPOSITE or entry.doc.data_config.write_enabled
            )
        self._entry_action(
            menu, "&Write", self.write_requested.emit, entry, enabled=live
        )

    def _add_open_swatches_action(self, menu: QMenu, entry: Entry) -> None:
        """Open Swatches, on a row filed under Palettes.

        Filed there, a click selects rather than opens, so there has to be a
        way to say so — and it is offered only there, since everywhere else the
        row's own click already is it. Named for what opening it shows, which
        also leaves the letter free: every one in "Open" is taken in these menus
        already. A palette file, a slice of one and a swatch composite all take
        it (``docs/design/palette-editing.md`` §2).
        """
        if self.section_of(entry) is ContentKind.PALETTE:
            self._entry_action(menu, "Open Swatc&hes", self.entry_activated.emit, entry)

    def _add_slice_actions(self, menu: QMenu, entry: Entry) -> bool:
        """New Slice…, from View and from Selection, on a file's, a palette's or
        a slice's row; True when the row is on screen, which is what the last
        two need.

        The new slice is always cut from the row the menu was opened on — a
        slice's row makes a **nested** slice, in that slice's decoded
        coordinates. All but the plain dialog additionally need the row on
        screen — the viewport, selection and settings snapshot live only there.
        """
        sliceable = entry is self._current and entry.doc is not None
        self._entry_action(menu, "New &Slice…", self.new_slice_requested.emit, entry)
        # ...and a view to read: an entry shown entire has no window for this to
        # cover, which is the entry's own answer to give (the File menu's row is
        # gated on the same capability).
        self._entry_action(
            menu,
            "New Slice from &View",
            self.new_slice_from_view_requested.emit,
            entry,
            enabled=sliceable and entry.can(Capability.NAVIGATION),
        )
        self._entry_action(
            menu,
            "New Slice &from Selection",
            self.new_slice_from_selection_requested.emit,
            entry,
            enabled=sliceable and self._has_selection,
        )
        return sliceable

    def _add_piece_jump_menu(self, menu: QMenu, entry: Entry) -> None:
        """Jump to Source on a composite: one row per piece, in list order.

        A composite has as many sources as it has pieces, so the slice's single
        row becomes a submenu naming each. The same entry may appear twice with
        different ranges — each run is its own row, since where it lands is the
        difference. Pads name nothing and are left out; a piece whose entry has
        been closed stays listed but dead, because the composite still counts
        its bytes and an undo can bring the entry back. With no piece to go to
        the submenu is dead as a whole rather than missing, so the gesture stays
        where the user learned it.
        """
        sub = menu.addMenu("&Jump to Source")
        for piece in entry.pieces:
            source = piece.entry
            if source is None:
                continue
            # Doubled so a name holding "&" shows it rather than a mnemonic.
            label = source.name.replace("&", "&&")
            if piece.is_ranged:
                last = piece.offset + piece.extent
                label += f"  [{format_hex(piece.offset)}\u2013{format_hex(last)}]"
            self._entry_action(
                sub,
                label,
                self.jump_to_piece_requested.emit,
                piece,
                enabled=source in self._items,
            )
        sub.setEnabled(any(a.isEnabled() for a in sub.actions()))

    def _add_use_as_palette_action(self, menu: QMenu, entry: Entry) -> None:
        """Use as Palette, beside the row's own way of being used.

        Every row whose bytes could be read as colours offers it, whatever kind
        it is: a ROM's palette table is a file, a slice or a composite view
        assembling several of them, and which of those it happens to be says
        nothing about the answer (``docs/design/palette-editing.md``). Gated on
        the same rule the palette dock's picker filters by, asked of the entry
        on screen, since that is what the colours would be applied to.
        """
        if self._current is not None and can_supply_palette(self._current, entry):
            self._entry_action(
                menu, "Use &as Palette", self.use_entry_as_palette_requested.emit, entry
            )

    def _add_new_composite_action(
        self, menu: QMenu, entry: Entry, acting: list[Entry]
    ) -> QAction | None:
        """New Composite View…, with the clicked row — or the selection — listed.

        The File menu's row, started from here: the rows that could be a piece
        open the dialog already listed, one whole run each in list order. On one
        row it is offered only where that row could be a piece (a file or slice
        of pixels), since anywhere else it would be the File menu's row with
        nothing of the click in it. Over a selection it is always shown, and the
        rows that could not be pieces are left out rather than making it go dead,
        as Export does with the rows that hold no document.

        **No mnemonic on a file's or a palette's row**, for the File menu's
        reason: every letter of the label is spoken for there (``C`` by Container
        Info). The other branches have ``C`` free.
        """
        sources = [e for e in acting if is_composable(e)]
        if len(acting) == 1 and not sources:
            return None
        label = (
            "New Composite View…"
            if entry.kind in (EntryKind.FILE, EntryKind.PALETTE)
            else "New &Composite View…"
        )
        return self._entry_action(
            menu,
            label,
            self.new_composite_requested.emit,
            sources,
            enabled=bool(sources),
        )

    def _add_order_actions(
        self, menu: QMenu, entry: Entry, moving: list[Entry]
    ) -> list[QAction]:
        """Move Up / Move Down and the Sort submenu, on every kind of row.

        ``moving`` is what the two move rows act on — the clicked row, or the
        whole selection when the click landed inside one. They are two of the
        three rows a multi-row selection leaves live, so they are handed back for
        :meth:`_only_these_live` to spare.

        Every row's place in the list is the user's, so every row can be moved —
        within its own group, which is what all of these are gated on. The keys go
        in the label after a tab, which Qt renders in the shortcut column, rather
        than being registered as the actions' shortcuts: a real binding here would
        fire from anywhere in the app, and reordering rows is a thing the *list*
        does. The working one is the tree's own key handling, which the app-wide
        navigation filter defers to while the list has focus.

        Sorting rearranges the **group** the clicked row is in rather than the
        whole list — the files of one section, or one file's children — since that
        is the span the user is looking at as an ordered thing. Nothing latches:
        it is one rearrangement of rows whose order stays theirs afterwards, so a
        row moved by hand next stays moved, and a re-pointed slice stays put.

        The orders live in a **submenu**: they are one question asked three ways,
        and three more siblings of Move Up would be most of the menu. It also
        gives them a mnemonic space of their own, where out here the obvious
        letters are taken — N by Rename in every branch, S by New Slice where a
        file's menu offers one — so inside it each order is its own first letter.
        The submenu itself takes b, which is why a file's New Bookmark spells
        itself with a k.

        **By offset only on the child kinds**, where the offset is what the row
        is: a file and a palette are the whole of their bytes, so a group of them
        would sort on a column of zeros. Left out rather than shown dead, because
        it is not a thing those rows could do under other circumstances — the
        distinction the rest of this menu draws between an absent row and a
        disabled one.
        """
        menu.addSeparator()
        live = []
        for label, delta in (("M&ove Up\tAlt+Up", -1), ("Move &Down\tAlt+Down", 1)):
            live.append(
                self._entry_action(
                    menu,
                    label,
                    self.move_requested.emit,
                    moving,
                    delta,
                    # Live while *any* of the rows has somewhere to go: a
                    # selection whose top row is already at the head of its group
                    # still moves the rest (see :meth:`move_orders`).
                    enabled=bool(self.move_orders(moving, delta)),
                )
            )
        # A group of one has no order to put right — a dead submenu, not a missing
        # one: the same rows with a second sibling would sort.
        sort = menu.addMenu("Sort &by")
        sort.setEnabled(len(self.sibling_entries(entry)) > 1)
        self._entry_action(sort, "&Name", self.sort_requested.emit, entry, SortKey.NAME)
        self._entry_action(sort, "&Type", self.sort_requested.emit, entry, SortKey.TYPE)
        if entry.kind in (EntryKind.SLICE, EntryKind.BOOKMARK):
            self._entry_action(
                sort, "&Offset", self.sort_requested.emit, entry, SortKey.OFFSET
            )
        return live

    def _add_clipboard_actions(self, menu: QMenu, entry: Entry) -> None:
        """Cut / Copy / Paste / Duplicate, on every kind of row.

        What travels is the **entry** — a reference plus the settings it is read
        through — and never the file behind it: cutting a row takes it out of the
        project, and duplicating one gives the project a second way in to bytes
        that were only ever written once.

        **Duplicate is for the kinds that can appear twice**: a slice, a
        bookmark, and a composite view — the three whose identity is not a path
        (a composite has none at all). A file and a palette are identified by
        theirs (:meth:`~celpix.project.workspace.Workspace.find_file`), and a
        second row over one file would be a second document over one buffer — two
        sets of unsaved edits with one file underneath them. Cut and Copy stay
        live on every kind, since pasting *elsewhere* is exactly what that
        identity permits.

        The shortcuts are display-only here, like Remove's: a closed menu's action
        never fires, so these label the keys while the working bindings are the
        tree's own (:class:`~celpix.ui.entry_tree.EntryTree`).
        """
        menu.addSeparator()
        self._entry_action(
            menu,
            "Cu&t",
            self.cut_requested.emit,
            entry,
            shortcut=QKeySequence.StandardKey.Cut,
        )
        self._entry_action(
            menu,
            "Cop&y",
            self.copy_requested.emit,
            entry,
            shortcut=QKeySequence.StandardKey.Copy,
        )
        self._entry_action(
            menu,
            "&Paste",
            self.paste_requested.emit,
            entry,
            enabled=clipboard.has_entries(),
            shortcut=QKeySequence.StandardKey.Paste,
        )
        self._entry_action(
            menu,
            "Dup&licate",
            self.duplicate_requested.emit,
            entry,
            enabled=entry.kind in (EntryKind.SLICE, EntryKind.BOOKMARK),
            shortcut=DUPLICATE_KEY,
        )

    @staticmethod
    def _only_these_live(menu: QMenu, live: list[QAction]) -> None:
        """Grey out every row of ``menu`` (and of its submenus) except ``live``.

        The multi-row selection gate. Applied over a finished menu rather than
        threaded through the twenty builders above, because the question it asks
        is not any one row's — it is "does this action name one entry?", and the
        answer is yes for all of them but the three handed in. Written the other
        way round, every builder would carry a copy of the same clause and a new
        row would join the menu live by default, which is the wrong default.

        Greyed, not dropped: each of these *is* a thing the clicked row could do,
        just not while it is one of several — the same distinction
        :meth:`_add_order_actions` draws between an absent row and a dead one. A
        submenu goes dead as a whole, its rows with it, so the user is not invited
        to open something with nothing live inside.

        Submenus are reached as the menu's **children**, never through
        ``QAction.menu()``: ``live`` holds a submenu's ``menuAction()``, and asking
        that action for its menu leaves the submenu owned by a Python wrapper, so
        the next garbage collection deletes it — Export vanishing from a menu
        that is still open (``docs/py-qt-reference/pyside6-pitfalls.md``).
        """
        for submenu in menu.findChildren(
            QMenu, options=Qt.FindChildOption.FindDirectChildrenOnly
        ):
            EntryMenuMixin._only_these_live(submenu, live)
        for action in menu.actions():
            if not any(action is spared for spared in live):
                action.setEnabled(False)

    def _show_menu(self, pos) -> None:
        item = self._tree.itemAt(pos)
        if item is None:
            return
        entry: Entry | None = item.data(0, Qt.ItemDataRole.UserRole)
        if entry is None:  # the Palettes header has no actions
            return
        # A right-click inside a multi-row selection keeps it (Qt's own rule), and
        # then the menu is about the set: Remove and the two moves act on all of
        # it, and every other row goes dead. A click *outside* the selection has
        # already collapsed it onto the clicked row by the time this runs, so the
        # ordinary one-entry menu falls out of the same two lines.
        selected = self.selected_entries()
        picked = [e for e in selected if e is entry]
        acting = selected if len(selected) > 1 and picked else [entry]
        multi = len(acting) > 1
        # "&" marks the keyboard mnemonic - the letter that picks the entry once
        # the menu is open. It matches the action's shortcut letter where one is
        # free (Write/Ctrl+W, Edit File Container/Ctrl+E). Each entry kind builds
        # its own menu below, so the letters only need to be unique per branch.
        menu = QMenu(self)
        live: list[QAction] = []
        paste_inputs: QAction | None = None
        new_composite: QAction | None = None
        # Each branch runs in the same groups, top to bottom: the row's own use
        # (open, jump, use as palette); what can be made from it (slices,
        # bookmarks, a composite view); its settings, ending in the Write that
        # commits them. Then the groups every kind shares: order, clipboard,
        # import/export, the file on disk, and Remove. Separators are added
        # freely — QMenu collapses a doubled or trailing one.
        if entry.kind in (EntryKind.FILE, EntryKind.PALETTE):
            # The two kinds that are a file of their own, and one menu with the
            # three places they differ: how the row is used, and a palette file
            # declaring no inputs to bind.
            palette = entry.kind is EntryKind.PALETTE
            if palette:
                # Opening shows the file as swatches; the double-click applies it.
                self._add_open_swatches_action(menu, entry)
                self._entry_action(
                    menu,
                    "&Use as Current Palette",
                    self.use_palette_requested.emit,
                    entry,
                )
            else:
                self._add_use_as_palette_action(menu, entry)
            menu.addSeparator()
            # A palette file is sliced like any file: a run of its colours is a
            # palette of its own, and a piece for a composite to assemble.
            sliceable = self._add_slice_actions(menu, entry)
            self._add_new_bookmark_action(menu, entry, sliceable=sliceable)
            new_composite = self._add_new_composite_action(menu, entry, acting)
            menu.addSeparator()
            self._add_rename_action(menu, entry)
            self._add_edit_container_action(menu, entry)
            self._add_container_info_action(menu, entry)
            if not palette:
                paste_inputs = self._add_inputs_actions(menu, entry)
            self._add_write_action(menu, entry)
            menu.addSeparator()
        elif entry.kind is EntryKind.SLICE:
            # A slice of a palette file sits under Palettes, where a click
            # selects rather than opens — so, as on a swatch composite, Open
            # is spelled out (``docs/design/palette-editing.md`` §2).
            self._add_open_swatches_action(menu, entry)
            # A slice's primary navigation action: reopen its region in the
            # parent file, decoded the slice's way, at the slice's offset.
            self._entry_action(
                menu, "&Jump to Source", self.jump_to_source_requested.emit, entry
            )
            self._add_use_as_palette_action(menu, entry)
            menu.addSeparator()
            # A slice is cut from like a file: the new one windows into this
            # slice's decoded bytes (``docs/design/slices-and-parents.md``).
            self._add_slice_actions(menu, entry)
            new_composite = self._add_new_composite_action(menu, entry, acting)
            menu.addSeparator()
            self._add_rename_action(menu, entry)
            self._entry_action(menu, "&Edit…", self.edit_slice_requested.emit, entry)
            paste_inputs = self._add_inputs_actions(menu, entry)
            self._add_write_action(menu, entry)
            menu.addSeparator()
        elif entry.kind is EntryKind.COMPOSITE:
            # No New Slice: a composite has no file behind it, so there is no
            # coordinate space to anchor a slice in, and Jump to Source is a
            # submenu of its pieces rather than one parent
            # (``docs/design/composite-entry.md``). Edit… re-lists its pieces,
            # and Write goes out through them.
            #
            # Filed under Palettes, a click no longer opens it, so there has to
            # be a way to say so: **Open** is that way, and it is offered only
            # there, since everywhere else the row's own click already is it.
            self._add_open_swatches_action(menu, entry)
            self._add_piece_jump_menu(menu, entry)
            self._add_use_as_palette_action(menu, entry)
            menu.addSeparator()
            # Over a selection only: a composite is never a piece of another.
            new_composite = self._add_new_composite_action(menu, entry, acting)
            menu.addSeparator()
            self._add_rename_action(menu, entry)
            self._entry_action(
                menu, "&Edit…", self.edit_composite_requested.emit, entry
            )
            self._add_write_action(menu, entry)
            menu.addSeparator()
        else:
            # The double-click action, discoverable; a bookmark holds no bytes
            # of its own, so there is no Write here.
            self._entry_action(
                menu, "&Jump to Bookmark", self.jump_to_bookmark_requested.emit, entry
            )
            # Reuse the bookmarked offset as a palette offset — the graphics
            # position often marks where the palette sits. Not on a bookmark of a
            # palette file: its offset marks a place among swatches, and a
            # palette document takes no palette of its own to apply it to.
            if entry.parent_kind is not EntryKind.PALETTE:
                self._entry_action(
                    menu,
                    "&Use as Palette",
                    self.bookmark_as_palette_requested.emit,
                    entry,
                )
            menu.addSeparator()
            new_composite = self._add_new_composite_action(menu, entry, acting)
            menu.addSeparator()
            self._add_rename_action(menu, entry)
            menu.addSeparator()
        if new_composite is not None:
            live.append(new_composite)
        live += self._add_order_actions(menu, entry, acting)
        # Paste Inputs is the fourth row a multi-row selection leaves live: it
        # lands on every selected row, which is the whole reason it exists.
        if paste_inputs is not None:
            live.append(paste_inputs)
        self._add_clipboard_actions(menu, entry)
        if entry.kind.has_document:
            # Import is the mirror of Export ▸ As PNG…, and lands the image at
            # the start of the entry. Unlike export it needs the entry on screen
            # (it is fitted to the view's palette and arrangement), so the window
            # activates it first.
            self._entry_action(
                menu, "&Import from PNG…", self.import_png_requested.emit, entry
            )
            # Export targets the entry the menu was opened on, not the current
            # view, so an entry can leave as an image without being activated
            # first — the window loads it on demand. Always offered: whether the
            # bytes decode is only knowable by trying.
            export = menu.addMenu("E&xport")
            if multi:
                # The set is exported whole, into one folder: the other rows it
                # picked that hold no document (a bookmark) are left out rather
                # than making the whole row go dead. A palette file holds one,
                # and leaves as its swatch sheet.
                targets = [e for e in acting if e.kind.has_document]
                live.append(export.menuAction())
                for label, raw in (
                    (f"{len(targets)} Entries as &PNGs…", False),
                    (f"{len(targets)} Entries as &Raw…", True),
                ):
                    live.append(
                        self._entry_action(
                            export,
                            label,
                            self.export_entries_requested.emit,
                            targets,
                            raw,
                        )
                    )
            else:
                self._entry_action(
                    export, "As &PNG…", self.export_png_requested.emit, entry
                )
                self._entry_action(
                    export, "&Raw…", self.export_raw_requested.emit, entry
                )
            if (
                not multi
                and entry.kind in (EntryKind.FILE, EntryKind.PALETTE)
                and self._has_slices(item)
            ):
                export.addSeparator()
                self._entry_action(
                    export, "&Slices as PNGs…", self.export_slices_requested.emit, entry
                )
            menu.addSeparator()
        if entry.kind in (EntryKind.FILE, EntryKind.PALETTE):
            # The two kinds that *are* a file on disk - a slice or bookmark only
            # borrows its parent's, so revealing one would point at a file the
            # row isn't. Named for the job rather than for any one desktop's
            # file manager, since the same item opens Explorer, Finder or
            # whatever the session runs.
            self._entry_action(
                menu,
                "Show in File &Manager",
                self.show_in_manager_requested.emit,
                entry,
            )
            menu.addSeparator()
        # The Delete hint is display-only: the working binding is the tree's own
        # key handling. Counted in the label when it is about several rows, so the
        # confirmation that follows is not the first the user hears of it.
        live.append(
            self._entry_action(
                menu,
                f"&Remove {len(acting)} Entries" if multi else "&Remove",
                self.remove_requested.emit,
                acting,
                shortcut=QKeySequence.StandardKey.Delete,
            )
        )
        if multi:
            self._only_these_live(menu, live)
        menu.exec(self._tree.viewport().mapToGlobal(pos))
        # Parented to the panel, the menu would outlive this call — and every
        # action's handler closes over the right-clicked entry, so a removed
        # entry (and the document it holds) would stay alive per right-click.
        menu.deleteLater()
