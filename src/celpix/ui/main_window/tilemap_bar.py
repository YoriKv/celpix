"""The bar under a tilemap's canvas: where its tiles come from, and how its
cells are read.

Takes the place of the navigation bar rather than sitting beside it. A tilemap
is always shown entire (``docs/design/tilemap-entry.md`` §8), so it has no view
window to move and the offset controls address a coordinate space it does not
have — leaving them there disabled would be a row of dead widgets claiming the
entry has a position to jump to. The two bars are pages of one stack, swapped by
:meth:`~TilemapBarMixin._sync_tilemap_bar` from the render cycle.

What replaces them is the binding: which **open entry** supplies the tiles the
cells index into, where in it tile 0 sits, and which codec reads the cells. None
of that is recoverable from the file — a screen names a *bank slot* in a tool
that had four loaded at once — so it is project state the user sets here and the
project remembers (§3, §7).

Binding names an entry and never a path. An entry already carries a container, a
reshape, a pixel format and a Write of its own, and the map reads the tiles
through all of it; a path in the binding would have had to restate every one and
would still have gone stale independently. Picking a file that is not open yet
opens it as an entry first — the move registering a palette file already makes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from celpix.core.capabilities import ContentKind
from celpix.core.errors import Stage
from celpix.project.workspace import Entry, EntryKind, TileMode, TileSource
from celpix.ui.widgets import CompactComboBox, signals_blocked

# What the "Tiles" combo holds besides the open entries. Distinct objects rather
# than strings so an entry named "From file..." cannot collide with the action.
_NONE = object()
_FROM_FILE = object()


class TilemapBarMixin:
    """The tilemap binding bar, and the swap that puts it on screen.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object. See the module docstring for why it replaces the
    navigation bar rather than joining it.
    """

    # -- construction --------------------------------------------------------
    def _build_tilemap_bar(self) -> QWidget:
        bar = QWidget()
        rows = QVBoxLayout(bar)
        rows.setContentsMargins(6, 2, 6, 2)
        rows.setSpacing(2)
        row = QHBoxLayout()
        offset_row = QHBoxLayout()
        rows.addLayout(row)
        rows.addLayout(offset_row)

        row.addWidget(QLabel("Tiles "))
        self._tile_binding = CompactComboBox(0.6)
        self._tile_binding.setToolTip(
            "Where this map draws from: an open entry, whose\n"
            "live edits it follows. A stamp layout names a panel\n"
            "instead, and takes that panel's tiles and attributes."
        )
        self._tile_binding.activated.connect(self._on_tile_binding_change)
        row.addWidget(self._tile_binding)

        row.addSpacing(12)
        self._tile_base_label = QLabel("Base tile ")
        row.addWidget(self._tile_base_label)
        # Signed, because the useful direction for a slice is the negative one:
        # a map numbering from 0x100 bound to a slice that starts there needs
        # cell 0x100 to draw tile 0 (:class:`TileSource`).
        self._tile_base = self._tilemap_hex_spin(
            -0xFFFF,
            0xFFFF,
            "Shifts every cell: cell N draws source tile base + N.\n"
            "Use it when the map and its tiles number from\n"
            "different places — negative when the map starts\n"
            "partway into a bank the source slice begins at.",
        )
        self._tile_base.valueChanged.connect(self._on_tile_base_change)
        row.addWidget(self._tile_base)

        # The cell codec is not here: it is a *format* picker, so it sits on the
        # codecs toolbar in the place the pixel format has on a pixel entry
        # (:meth:`~...interpretation.InterpretationMixin._build_toolbar`). What
        # is left on this bar is the binding, which no other kind of entry has.
        row.addStretch(1)

        # Says which entry the tiles are coming from without making the user
        # open the combo to find out, and reads as the sentence the binding is.
        self._tile_binding_note = QLabel()
        self._tile_binding_note.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        offset_row.addWidget(self._tile_binding_note)
        offset_row.addStretch(1)
        return bar

    def _tilemap_hex_spin(self, low: int, high: int, tip: str) -> QSpinBox:
        """A hex spin matching the navigation bar's, so the two bars read alike
        where they show the same kind of number."""
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setDisplayIntegerBase(16)
        spin.setPrefix("$")
        spin.setKeyboardTracking(False)
        spin.setToolTip(f"{tip} (hex)")
        return spin

    # -- the swap ------------------------------------------------------------
    def _sync_tilemap_bar(self) -> None:
        """Show whichever bar the current entry has controls for, and fill it."""
        doc = self._doc
        is_tilemap = doc is not None and doc.is_tilemap
        self._nav_stack.setCurrentWidget(
            self._tilemap_bar if is_tilemap else self._navbar
        )
        if is_tilemap:
            self._refresh_tilemap_bar()

    def _refresh_tilemap_bar(self) -> None:
        """Put the current entry's binding into the widgets.

        Signal-blocked throughout: this is a restore, not a user change, and a
        combo repopulated mid-refresh would otherwise re-enter as an edit and
        rebind the entry to whatever landed at index 0.
        """
        entry = self._workspace.current
        if entry is None:
            return
        source = entry.tile_source or TileSource()
        with signals_blocked(self._tile_binding):
            self._fill_binding_combo(entry, source)
        with signals_blocked(self._tilemap_preset):
            self._fill_codec_combo(entry)
        with signals_blocked(self._tile_base):
            self._tile_base.setValue(source.base_index)
        self._tile_binding_note.setText(self._binding_note(entry, source))

    def _binding_note(self, entry: Entry, source: TileSource) -> str:
        """One line saying where the tiles come from, and how they are read.

        The pixel format is the bound entry's own — a tilemap does not get a
        second opinion about it — so this reports it rather than offering a
        control that would fight the entry's own picker.
        """
        stamps = self._tilemap_is_indirect(entry)
        if not source.is_bound:
            return (
                "No panel bound - this layout draws nothing until one is."
                if stamps
                else "No tiles bound - every cell draws blank."
            )
        entries = self._workspace.entries
        index = source.entry_index
        if index is None or not 0 <= index < len(entries):
            return "The entry it drew from is no longer open."
        bound = entries[index]
        if stamps:
            # A stamp layout takes the panel's tiles *and* its attributes, so
            # there is no format of its own to report - the panel's is the one
            # that matters, and it is on the panel's own bar.
            return f"Stamped from {bound.name}, view-only."
        preset = bound.session.pixel_preset_id if bound.session is not None else ""
        try:
            name = self._registry.preset(preset).name
        except KeyError:
            name = preset or "its own format"
        return f"Tiles from {bound.name}, read as {name}."

    def _fill_binding_combo(self, entry: Entry, source: TileSource) -> None:
        combo = self._tile_binding
        combo.clear()
        combo.addItem("(none)", _NONE)
        # A stamp layout draws through a *panel*, so its candidates are the other
        # tilemaps; everything else takes pixel entries. Never the map itself,
        # which would bind an entry to its own bytes.
        wants = (
            ContentKind.TILEMAP
            if self._tilemap_is_indirect(entry)
            else ContentKind.PIXELS
        )
        for index, candidate in enumerate(self._workspace.entries):
            if candidate is entry or candidate.content_kind is not wants:
                continue
            if candidate.kind is EntryKind.BOOKMARK:
                continue
            combo.addItem(candidate.name, index)
        combo.addItem("From file...", _FROM_FILE)
        if source.mode is TileMode.ENTRY:
            at = combo.findData(source.entry_index)
            # A binding whose entry has since been closed keeps its stored index
            # but has nothing to select: show it as unbound rather than silently
            # landing on whichever entry now sits at that position.
            combo.setCurrentIndex(at if at >= 0 else 0)
        else:
            combo.setCurrentIndex(0)

    def _fill_codec_combo(self, entry: Entry) -> None:
        """Put the tilemap formats into the codecs toolbar's picker.

        Filled from here rather than at build time because the selection is the
        *entry's*, and this is the pass that runs whenever one is shown.
        """
        combo = self._tilemap_preset
        combo.clear()
        for preset in sorted(
            self._registry.presets(Stage.INTERPRET_TILEMAP), key=lambda p: p.name
        ):
            combo.addItem(preset.name, preset.id)
        at = combo.findData(entry.tilemap_preset_id)
        if at >= 0:
            combo.setCurrentIndex(at)

    # -- edits ---------------------------------------------------------------
    def _on_tile_binding_change(self, _index: int) -> None:
        entry = self._workspace.current
        if entry is None:
            return
        data = self._tile_binding.currentData()
        if data is _FROM_FILE:
            self._bind_tiles_from_file(entry)
            return
        if data is _NONE or data is None:
            source = TileSource(base_index=self._tile_base.value())
        else:
            source = TileSource(
                mode=TileMode.ENTRY,
                entry_index=int(data),
                base_index=self._tile_base.value(),
            )
        self._rebind_tiles(entry, source)

    def _bind_tiles_from_file(self, entry: Entry) -> None:
        """Open a file of tiles as an entry, then bind this map to it.

        The file becomes a first-class entry rather than a path hidden inside
        the binding — the same move registering a palette file makes. It gets a
        row in the list, its own pixel format, its own Write, and the map reads
        it through all of that; a path stored in the binding would have had to
        restate every one of those and would still have gone stale on its own.

        Opening activates the new entry (every file open does), so the view is
        put back on the map afterwards: the user asked for tiles *for this map*,
        not to go and look at them.
        """
        path, _ = QFileDialog.getOpenFileName(self, "Tiles for this tilemap")
        if not path:
            self._refresh_tilemap_bar()  # cancelled: put the combo back
            return
        self._load_pixel(path)
        bound = self._workspace.find_file(path)
        wants = (
            ContentKind.TILEMAP
            if self._tilemap_is_indirect(entry)
            else ContentKind.PIXELS
        )
        if bound is None or bound.content_kind is not wants:
            # A file of the wrong kind cannot supply what this map needs — tiles
            # for an ordinary tilemap, a panel for a stamp layout. It is still
            # open, since the user asked for it, but the binding is left alone
            # rather than pointed somewhere useless.
            self._activate_entry(entry)
            self._refresh_tilemap_bar()
            return
        source = TileSource(
            mode=TileMode.ENTRY,
            entry_index=self._workspace.entries.index(bound),
            base_index=self._tile_base.value(),
        )
        entry.tile_source = source
        self._activate_entry(entry)
        self._rebind_tiles(entry, source)

    def _on_tile_base_change(self, value: int) -> None:
        entry = self._workspace.current
        if entry is None:
            return
        source = entry.tile_source or TileSource()
        self._rebind_tiles(entry, _replaced(source, base_index=value))

    def _on_tilemap_preset_change(self, _index: int) -> None:
        """A different cell format for this entry — re-read it under the new one.

        Unlike a pixel preset, which only re-interprets a buffer already in hand,
        this changes how many bytes a cell is and so what the grid *is*; the load
        path is the only thing that knows how to rebuild that.
        """
        entry = self._workspace.current
        if entry is None or entry.content_kind is not ContentKind.TILEMAP:
            return
        entry.tilemap_preset_id = str(self._tilemap_preset.currentData())
        self._reload_tilemap(entry)

    def _rebind_tiles(self, entry: Entry, source: TileSource) -> None:
        """Point ``entry`` at ``source`` and re-read it.

        The base index rides on the document as well as the entry, so a change
        to it takes effect without a reload — but a change of *source* means
        different bytes, and those only arrive through the load path.
        """
        entry.tile_source = source
        self._reload_tilemap(entry)

    def _reload_tilemap(self, entry: Entry) -> None:
        """Re-read ``entry`` under its current binding and put it back on screen.

        The document is dropped rather than patched: which bytes there are comes
        out of Read, and a binding change is a change of *which file* — there is
        nothing in the old document worth carrying over. A tilemap holds no
        unsaved pixel edits to lose, since its pixel half is another entry's.
        """
        entry.doc = None
        if not self._load_entry(entry):
            return
        self._doc = entry.doc
        self._refresh_view()
        self._refresh_project_modified()


def _replaced(source: TileSource, **changes) -> TileSource:
    """``source`` with ``changes`` applied — ``dataclasses.replace`` by another
    name, kept local so the call sites read as edits to a binding."""
    from dataclasses import replace

    return replace(source, **changes)
