"""Edit ▸ Copy View Settings / Paste View Settings (Ctrl+Shift+C / Ctrl+Shift+V).

Copies how the entry on screen is read and coloured — its pixel format and
arrangement or its cell format, the compression preview, and the palette — and
lands that answer on whichever entry is on screen at the paste: a file, a slice,
a composite view. What the halves are, and which of them lands where, is
:mod:`celpix.project.view_settings`.

A paste is the same operation as Jump to Bookmark, aimed at the entry on screen
rather than at a parent: the settings are installed as the entry's restore
state and the entry is read again under them, its unsaved bytes carried into
the new document, as one step
(:class:`~celpix.ui.undo_commands.EntryStateCommand`). One re-read rather than a
command per control, because the controls do not settle independently — a
palette format is checked against the pixel format's colour count, and a bitmap
width re-cuts the codec's tiles — and the view stays on the byte it was
standing on, whatever size a tile has become.

Reads, through ``self``: ``_doc`` and ``_workspace`` (created in
``MainWindow.__init__``), and the session capture and re-read of
:mod:`~celpix.ui.main_window.session` and :mod:`~celpix.ui.main_window.jumps`.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence

from celpix.core.capabilities import ContentKind
from celpix.core.document import ViewOptions
from celpix.project.view_settings import (
    Arrangement,
    PaletteSettings,
    ViewSettings,
    view_settings_from_payload,
    view_settings_payload,
)
from celpix.project.workspace import Entry, EntryKind, PaletteMode, palette_source_for
from celpix.ui import clipboard
from celpix.ui.undo_commands import EntryStateCommand, ParentState


class ViewSettingsMixin:
    """Copy / Paste View Settings.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object. See the module docstring for what it owns and which
    attributes it reads that others create.
    """

    # Built by name in the loop below, so declared here for the readers.
    _copy_view_settings_action: QAction
    _paste_view_settings_action: QAction

    def _build_view_settings_actions(self) -> None:
        """The two Edit-menu rows and their window-wide keys.

        Window-scoped like the tile clipboard's, so the keys work from the
        canvas, a toolbar or a dock alike. Ctrl+Shift rather than a bare letter:
        C and V are the transform bar's. No panel claims the pair, so it reaches
        here whatever has focus.
        """
        for attr, text, key, tip, slot in (
            (
                "_copy_view_settings_action",
                "Copy &View Settings",
                "Ctrl+Shift+C",
                "Copy this entry's pixel format and arrangement\n"
                "(or cell format), compression preview and palette",
                self._copy_view_settings,
            ),
            (
                "_paste_view_settings_action",
                "&Paste View Settings",
                "Ctrl+Shift+V",
                "Read this entry with the copied view settings\n"
                "A pixel format lands on pixels, a cell format on a map;\n"
                "the compression preview and palette land on either",
                self._paste_view_settings,
            ),
        ):
            action = QAction(text, self)
            action.setShortcut(QKeySequence(key))
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            action.setToolTip(tip)
            action.triggered.connect(slot)
            action.setEnabled(False)
            setattr(self, attr, action)
            self.addAction(action)

    def _view_settings_actions(self) -> tuple[QAction, QAction]:
        return (self._copy_view_settings_action, self._paste_view_settings_action)

    def _sync_view_settings_actions(self) -> None:
        """Copy wants an entry on screen; Paste one and a copy to paste."""
        shown = self._doc is not None and self._workspace.current is not None
        self._copy_view_settings_action.setEnabled(shown)
        self._paste_view_settings_action.setEnabled(
            shown and clipboard.has_view_settings()
        )

    # -- copy ----------------------------------------------------------------
    def _copy_view_settings(self) -> None:
        entry = self._workspace.current
        if entry is None or self._doc is None:
            return
        self._capture_session()  # the live toolbar is newer than the session
        settings = self._view_settings_of(entry)
        palette = settings.palette
        source = palette.source.entry if palette and palette.source else None
        clipboard.put_view_settings(
            view_settings_payload(settings, clipboard.SESSION_TOKEN),
            {0: source} if source is not None else {},
        )
        self.statusBar().showMessage(f"Copied the view settings of {entry.name}.")

    def _view_settings_of(self, entry: Entry) -> ViewSettings:
        """``entry``'s settings as they stand; its session freshly captured."""
        session, view = entry.session, self._doc.view
        assert session is not None  # _capture_session just wrote it
        # A palette file's colours are its own bytes: nothing to hand on.
        palette = (
            None
            if entry.kind is EntryKind.PALETTE
            else PaletteSettings(
                preset_id=session.palette_preset_id,
                mode=session.palette_mode,
                source=palette_source_for(entry),
                row=view.palette_row,
            )
        )
        if entry.content_kind is ContentKind.TILEMAP:
            return ViewSettings(
                tilemap=True,
                compression_id=session.preview_compression_id,
                palette=palette,
                # The format in force, not the entry's own answer, which is
                # empty while the map reads under its default.
                tilemap_preset_id=self._tilemap_preset_id(entry),
            )
        return ViewSettings(
            tilemap=False,
            compression_id=session.preview_compression_id,
            palette=palette,
            pixel_preset_id=session.pixel_preset_id,
            palette_view_preset_id=session.palette_view_preset_id,
            arrangement=Arrangement(
                block_columns=view.block_columns,
                block_rows=view.block_rows,
                block_order=view.block_order,
                two_dimensional=view.two_dimensional,
                bitmap_width=view.bitmap_width,
            ),
        )

    # -- a file opening --------------------------------------------------------
    def _inherit_view_settings(self, entry: Entry) -> None:
        """Seed ``entry``, a file about to open, with the view settings of the
        entry on screen — when both are the same content kind.

        Opening the next file of a set is the commonest way to arrive somewhere
        that reads exactly like where the user already is: the next bank of a
        ROM dump, the next screen of a game. So it arrives the way a paste would
        leave it, through the restore state a first load consumes — the
        mechanism a new slice is seeded from its parent by
        (:meth:`~...slices.SlicesMixin._seed_slice_from_parent`).

        What the file says about itself still wins: a container that names its
        cell format keeps it, and one that names its pixel format re-reads the
        payload in it, the session being marked as only handed on
        (:attr:`~celpix.project.entry.EntrySession.inherited`).

        A tilemap is given no view: a map whose file states its width seeds Cols
        from it only when it opens with no view of its own, and a Cols carried
        over from another map would draw this one at the wrong stride.
        """
        current = self._workspace.current
        if (
            current is None
            or current.doc is None
            or current.kind is EntryKind.PALETTE
            or current.content_kind is not entry.content_kind
            or entry.content_kind not in (ContentKind.PIXELS, ContentKind.TILEMAP)
        ):
            return
        self._capture_session()  # the live toolbar is newer than the session
        settings = self._view_settings_of(current)
        session = replace(self._seed_session(entry), inherited=True)
        changes: dict[str, object] = {"preview_compression_id": settings.compression_id}
        palette = settings.palette
        if palette is not None:
            changes |= {
                "palette_preset_id": palette.preset_id,
                "palette_mode": palette.mode,
            }
            entry.pending_palette = (
                replace(palette.source) if palette.source is not None else None
            )
        if settings.tilemap:
            if entry.tilemap_preset_id is None:
                entry.tilemap_preset_id = settings.tilemap_preset_id or None
        else:
            changes |= {
                "pixel_preset_id": settings.pixel_preset_id,
                "palette_view_preset_id": settings.palette_view_preset_id,
            }
            arrangement = settings.arrangement or Arrangement()
            entry.pending_view = ViewOptions(
                palette_row=palette.row if palette is not None else 0,
                block_columns=arrangement.block_columns,
                block_rows=arrangement.block_rows,
                block_order=arrangement.block_order,
                two_dimensional=arrangement.two_dimensional,
                bitmap_width=arrangement.bitmap_width,
            )
        entry.session = replace(session, **changes)

    # -- paste ---------------------------------------------------------------
    def _paste_view_settings(self) -> None:
        entry = self._workspace.current
        if entry is None or self._doc is None or self._applying_undo:
            return
        payload = clipboard.take_view_settings()
        settings = view_settings_from_payload(payload)
        if settings is None:
            self.statusBar().showMessage("Nothing to paste: no view settings copied.")
            return
        notes: list[str] = []
        palette = self._pasted_palette(entry, settings, payload, notes)
        self._capture_session()  # the target reads the live view and toolbar
        target = self._settings_target(entry, settings, palette)
        if target is None:
            self.statusBar().showMessage(
                " ".join([f"{entry.name} already reads this way.", *notes])
            )
            return
        was = entry.doc
        self._push_command(
            EntryStateCommand(
                self,
                entry,
                "paste view settings",
                target,
                position=self._recorded_byte_position(),
            )
        )
        if entry.doc is was:
            return  # the re-read refused, and has said why
        if not settings.reads_onto(entry.content_kind is ContentKind.TILEMAP):
            notes.append(
                "The cell format only fits a tilemap."
                if settings.tilemap
                else "The pixel format and arrangement only fit pixels."
            )
        self.statusBar().showMessage(
            " ".join([f"Pasted view settings onto {entry.name}.", *notes])
        )

    def _pasted_palette(
        self,
        entry: Entry,
        settings: ViewSettings,
        payload: object,
        notes: list[str],
    ) -> PaletteSettings | None:
        """The palette half as it can land on ``entry``, or None to leave its own.

        An ENTRY palette's source is an object only the copying process holds
        (:func:`~celpix.ui.clipboard.take_view_settings_sources`), so a copy from
        another window, or one whose source has since closed, cannot name it —
        and neither can one whose source is the entry being pasted onto, which
        would read its own colours out of itself.
        """
        palette = settings.palette
        if palette is None or entry.kind is EntryKind.PALETTE:
            return None
        if palette.mode is not PaletteMode.ENTRY or palette.source is None:
            return palette
        live = (
            clipboard.take_view_settings_sources()
            if isinstance(payload, dict)
            and payload.get("session") == clipboard.SESSION_TOKEN
            else {}
        )
        source = live.get(0)
        if (
            not isinstance(source, Entry)
            or source is entry
            or not any(e is source for e in self._workspace.entries)
        ):
            notes.append("The palette's source entry is not open, so it was kept.")
            return None
        return replace(palette, source=replace(palette.source, entry=source))

    def _settings_target(
        self, entry: Entry, settings: ViewSettings, palette: PaletteSettings | None
    ) -> ParentState | None:
        """The state ``entry`` reads under once ``settings`` land, or None when
        that is the state it is already in — which costs no step."""
        session, view = entry.session, self._doc.view
        assert session is not None  # captured by the caller
        tilemap = entry.content_kind is ContentKind.TILEMAP
        reads = settings.reads_onto(tilemap)
        changes: dict[str, object] = {"preview_compression_id": settings.compression_id}
        view_changes: dict[str, object] = {}
        if palette is not None:
            changes |= {
                "palette_preset_id": palette.preset_id,
                "palette_mode": palette.mode,
            }
            view_changes["palette_row"] = palette.row
        if reads and not tilemap:
            changes |= {
                "pixel_preset_id": settings.pixel_preset_id,
                "palette_view_preset_id": settings.palette_view_preset_id,
            }
            arrangement = settings.arrangement or Arrangement()
            view_changes |= {
                "block_columns": arrangement.block_columns,
                "block_rows": arrangement.block_rows,
                "block_order": arrangement.block_order,
                "two_dimensional": arrangement.two_dimensional,
                "bitmap_width": arrangement.bitmap_width,
            }
        cells = settings.tilemap_preset_id if reads and tilemap else None
        current_palette = palette_source_for(entry)
        pending_palette = (
            (replace(palette.source) if palette.source is not None else None)
            if palette is not None
            else current_palette
        )
        unchanged = (
            all(getattr(session, k) == v for k, v in changes.items())
            and all(getattr(view, k) == v for k, v in view_changes.items())
            and pending_palette == current_palette
            and (cells is None or cells == self._tilemap_preset_id(entry))
        )
        if unchanged:
            return None
        new_session = replace(session, **changes)
        # The selection names tiles counted in the old format's size; under
        # another one the same numbers name other bytes.
        if new_session.pixel_preset_id != session.pixel_preset_id:
            new_session = replace(
                new_session,
                selected_tile=None,
                selected_last=None,
                selection_slots=None,
            )
        return ParentState(
            session=new_session,
            pending_view=replace(view, **view_changes),
            pending_palette=pending_palette,
            inputs=entry.inputs,
            tilemap_preset_id=cells if cells is not None else entry.tilemap_preset_id,
            reread=True,
        )
