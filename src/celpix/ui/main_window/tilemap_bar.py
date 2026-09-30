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

The **palette row base** is not on this bar, though it is the colour twin of Base
tile. A named row meeting the palette that got loaded is a question a tile bank
has as much as a map — a bank's per-tile rows count from a base too — so the
control belongs where the palette is, on the palette dock
(:meth:`~...palette_dock.PaletteDockMixin._sync_row_base`).

Binding names an entry and never a path. An entry already carries a container, a
reshape, a pixel format and a Write of its own, and the map reads the tiles
through all of it; a path in the binding would have had to restate every one and
would still have gone stale independently. Picking a file that is not open yet
opens it as an entry first — the move registering a palette file already makes.

The bound entry may be **another tilemap**, in which case each cell stamps one of
that map's cells rather than naming a tile, and the tiles come from whatever it is
itself bound to — another map in turn, to any depth. What stops a chain is a loop,
and a map with no cells to lend: one that did not open, or a sprite object
(``docs/design/tilemap-entry.md`` §3.1). What the bar offers is filtered by
:meth:`~...session.SessionMixin._can_supply_tiles`, the gate every hop of the
resolution asks, and nothing here decides it twice.

Every control on the bar that sets **project state the file does not record** is
one undoable step, and the binding, the base and the size pair share one snapshot
type and one apply — so a gesture, an undo and a redo settle a binding by the
same route (:class:`~celpix.ui.undo_commands.TilemapBindingState`,
:meth:`~TilemapBarMixin._apply_tilemap_binding`). Each of those changes what the
document *is*, so landing one drops it and reads the entry again.

Some of the controls sit outside that. **All Frames** and **Transparent 0** say
how much of an already-decoded document to show, which is the reading Show
Rearranged Tiles gets: a view toggle and **no re-read**. They are still undo
steps — the answer is the entry's and the project file keeps it, so it is part
of how the sheet is set up rather than a glance at it.

What a fontmap's codes *say* is not on this bar at all. It is the **font's** own
data, not the map's, and it is typed up against the sheet that draws it — so it
lives on the entry supplying the tiles and is edited in a window of its own
(:mod:`celpix.ui.main_window.font_alphabet`).
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from celpix.core.capabilities import Capability, ContentKind
from celpix.core.errors import Stage
from celpix.core.tilemap import Geometry, IndexAddressing, rebased
from celpix.project import documents
from celpix.project.workspace import (
    Entry,
    PaletteMode,
    TileMode,
    TileSource,
    palette_source_for,
)
from celpix.ui.glyphs import Glyph
from celpix.ui.icon_font import glyph_icon
from celpix.ui.searchable_combo import (
    SearchableComboBox,
    fill_grouped,
    preset_rows,
    tilemap_codec_label,
)
from celpix.ui.undo_commands import (
    TilemapBindingCommand,
    TilemapBindingState,
    ViewToggleCommand,
)
from celpix.ui.widgets import (
    CompactComboBox,
    RunSpinBox,
    add_labelled,
    hex_spin,
    signals_blocked,
    value_spin,
    wrap_lines,
)

# What the "Tiles" combo holds besides the open entries. Distinct objects rather
# than strings so an entry named "From file..." cannot collide with the action.
_NONE = object()
_FROM_FILE = object()


def tile_base_tip(noun: str, chained: bool) -> str:
    """The Base spin's tooltip for a base counting ``noun``.

    The base counts what the index counts (``docs/design/terminology.md``), so
    the sum it states is in that unit: a tile or a source cell, or a whole
    metatile or stamp. Over another tilemap the cells are coordinates, so it
    shifts which source cell or stamp they name (`CellChain.base`) rather than
    which tile they draw.
    """
    verb = "stamps" if chained else "draws"
    what = "source cell" if noun == "cell" else noun
    whole = "" if noun in ("tile", "cell") else f" by whole {noun}s"
    return (
        f"Shifts every cell{whole}:\n"
        f"cell N {verb} {what} base + N\n"
        "Negative when the map starts partway into its source"
    )


# The Index unit combo's rows, in order: the format's own reading, then the
# two the binding can state over it. Matched by position rather than carried as
# item data, because a str-valued enum comes back out of a combo as a plain
# string (``docs/py-qt-reference/pyside6-pitfalls.md``).
_ADDRESSING_CHOICES: tuple[IndexAddressing | None, ...] = (
    None,
    IndexAddressing.CORNER,
    IndexAddressing.ORDINAL,
)


def _counts_elements(geometry: Geometry | None) -> bool:
    """Whether ``geometry`` numbers one element per unit, so an ordinal *is*
    its corner: none at all, or units of one element, packed or on a grid
    alike (:func:`~celpix.core.tilemap.corner_at` is then the identity)."""
    return geometry is None or geometry[:2] == (1, 1)


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
        # The per-cell property row sits above the binding row: it describes the
        # cells' own attributes where the row below describes where their tiles
        # come from, and its controls are grown per format rather than built here
        # (:class:`~celpix.ui.main_window.cell_props_bar.CellPropsMixin`).
        props_row = QHBoxLayout()
        row = QHBoxLayout()
        offset_row = QHBoxLayout()
        rows.addLayout(props_row)
        rows.addLayout(row)
        rows.addLayout(offset_row)
        self._build_cell_props_row(props_row)

        row.addWidget(QLabel("Tiles "))
        # Wider than the mode pickers because it holds *file names*, which have no
        # bound at all — and a stated width is what stops it resizing the bar
        # every time the user opens an entry with a longer name than the last.
        self._tile_binding = SearchableComboBox(160)
        # What the combo's rows were last built from (:meth:`_binding_combo_rows`),
        # so a refresh that changes none of it leaves them alone.
        self._binding_combo_rows_shown: tuple[object, ...] | None = None
        self._tile_binding.setToolTip(
            "Which open entry supplies this map's tiles\n"
            "Its edits follow through live\n"
            "A tilemap works too: each cell stamps one of its cells"
        )
        self._tile_binding.activated.connect(self._on_tile_binding_change)
        row.addWidget(self._tile_binding)

        # Go and look at what the combo names. The binding is the one control on
        # this bar whose value is *another entry*, and "Tiles from x" is not the
        # same as being able to see x - to check a tile, or edit it where it
        # lives, the user had to find that row in the Files dock. Right beside the
        # combo because it opens that combo's own answer, and Back returns
        # (:mod:`celpix.ui.main_window.history`). No keyboard shortcut: it is the
        # one gesture here that is about a different entry, not this map's state.
        #
        # Marked with a ring-and-dot rather than an arrow: an arrow would read as
        # one of the navigation bar's steps - somewhere relative to here - and
        # this opens the one entry the combo beside it already names.
        self._tile_binding_jump = QPushButton()
        self._bake_binding_jump_icon()
        self._tile_binding_jump.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._tile_binding_jump.setFixedWidth(30)
        self._tile_binding_jump.clicked.connect(self._jump_to_bound_tiles)
        row.addWidget(self._tile_binding_jump)

        # Chosen before the base, and so placed before it: what an index counts
        # is also what the base counts, so the base and the cell's number are
        # both read in the unit picked here — and changing it re-counts the
        # base (:meth:`_on_index_addressing_change`). The caption reads on into
        # the item, "Index unit: Stamps", and the items are renamed per binding
        # (:meth:`_sync_index_addressing`). The tooltip here is a placeholder the
        # first sync replaces.
        row.addSpacing(12)
        self._index_addressing = CompactComboBox(165)
        for _ in _ADDRESSING_CHOICES:
            self._index_addressing.addItem("")
        self._index_addressing.activated.connect(self._on_index_addressing_change)
        self._index_addressing_label = add_labelled(
            row,
            "Index unit: ",
            self._index_addressing,
            "What each index counts in the source it draws from",
        )

        row.addSpacing(12)
        # Named for what it counts — "Base stamp", "Base tile" — which the
        # refresh keeps in step with the unit beside it (:meth:`_index_noun`).
        self._tile_base_label = QLabel("Base tile ")
        row.addWidget(self._tile_base_label)
        # Signed, because the useful direction for a slice is the negative one:
        # a map numbering from 0x100 bound to a slice that starts there needs
        # cell 0x100 to draw tile 0 (:class:`TileSource`).
        self._tile_base = hex_spin(-0xFFFF, 0xFFFF, tile_base_tip("tile", False))
        self._tile_base.valueChanged.connect(self._on_tile_base_change)
        row.addWidget(self._tile_base)

        row.addSpacing(12)
        # A **sprite map**'s one piece of geometry that no file records: the pair a
        # subsprite's size bit chooses between was a PPU register the scene set
        # (scgcad-formats.md sec 8.2). Two multiples of the tile size rather than a
        # pick from the console's six pairs - what a subsprite is built from is a
        # square of *tiles*, so the numbers stay meaningful at any tile size. Hidden
        # outright on every other tilemap, where there is no size bit to resolve.
        #
        # Each spin is captioned rather than the pair being written "1 x 2": these
        # are the *small* and *large* alternatives one bit picks between, not a
        # width and a height. A subsprite is always square, so an "x" would name a
        # shape none of them has.
        self._size_pair_label = QLabel("Subsprite ")
        row.addWidget(self._size_pair_label)
        tip = (
            "The two sizes a subsprite's size bit picks between,\n"
            "each a square that many tiles on a side"
        )
        self._size_small = value_spin(1, 8, 1, self._on_size_pair_change)
        self._size_small_label = add_labelled(row, "Sm ", self._size_small, tip)
        self._size_large = value_spin(1, 8, 2, self._on_size_pair_change)
        self._size_large_label = add_labelled(row, " Lg ", self._size_large, tip)

        # Beside the pair because it is the other half of "what is on this
        # sheet", and sprite-only for the same reason: only a sprite map has
        # frame *slots* it may not have filled. A file has room for a fixed 32 or
        # 64 of them and most are empty, so the strip stops after the last one
        # holding a drawn subsprite; this shows the rest. (128 is the *subsprites*
        # per frame in the extended form, not a frame count — ``scgcad.py``.)
        #
        # Not undoable, unlike everything else on this bar. Those set project
        # state the file does not record - a binding, a size pair - and this only
        # says how much of the file to look at, which is the reading Show
        # Rearranged Tiles and Show Palette Regions already get.
        self._all_frames = QCheckBox("All Frames")
        self._all_frames.setToolTip(
            "Show every frame slot the file has room for\n"
            "Off stops after the last frame that draws something"
        )
        self._all_frames.toggled.connect(self._on_all_frames_change)
        row.addWidget(self._all_frames)

        # How these formats say "empty". Index 0 of a BG palette row is the
        # console's transparent colour, so a blank cell is not a special tile
        # number - it names a real tile whose pixels are all 0, and the map is
        # full of them: a screen's backdrop is one such cell repeated over half
        # of it. Drawn opaque that becomes a flat slab of whatever colour sits at
        # row 0, which is the single thing that makes a correctly bound map look
        # wrong.
        #
        # Not undoable and not gated on the format, unlike the two beside it: it
        # says how to *show* a colour every indexed format has, so there is no
        # kind of tilemap it means nothing on - the reading All Frames' comment
        # gives for the same choice.
        self._transparent_zero_box = QCheckBox("Transparent 0")
        self._transparent_zero_box.setToolTip(
            "Draw palette index 0 as nothing, the way the console does\n"
            "Off leaves index 0 an ordinary colour, to see and to edit"
        )
        self._transparent_zero_box.toggled.connect(self._on_transparent_zero_change)
        row.addWidget(self._transparent_zero_box)

        row.addSpacing(12)
        # Reads the selected cell and writes it: the one gesture a tilemap has
        # that no pixel control stands in for. The same number the base shifts,
        # in the same unit, one for the whole map and one for a cell - and it
        # labels the canvas too (View > Show Tile IDs).
        self._cell_index_label = QLabel("Tile ")
        row.addWidget(self._cell_index_label)
        # Its label and tooltip name what the number counts, which moves with
        # the document (:meth:`_index_noun`); a tile until there is one.
        self._cell_index = hex_spin(
            0,
            0xFFFF,
            "The tile the selected cells name - set it to point\nthem somewhere else",
            kind=RunSpinBox,
        )
        self._cell_index.valueChanged.connect(self._on_cell_index_change)
        row.addWidget(self._cell_index)

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

    # -- the swap ------------------------------------------------------------
    def _sync_tilemap_bar(self) -> None:
        """Show whichever bar the current entry has controls for, and fill it.

        Which bar is the capability's answer, asked here rather than left to the
        gating pass: the two are pages of one stack, so what a tilemap needs is
        the *other page* and not a greyed copy of this one — which is all
        :mod:`~celpix.ui.main_window.capability_sync` can express
        (:data:`~celpix.ui.main_window.capability_sync._GATED_IN_PLACE`).

        The document has to be there as well as the capability, and the second
        test is not redundant: a missing-file entry keeps ``current`` on itself
        with nothing loaded (:meth:`~...session.SessionMixin._show_unavailable`),
        and so does closing one — this runs from ``_clear_document_view`` for
        exactly that reason. The capability alone would leave the binding bar on
        screen describing an entry with nothing behind it.
        """
        binding = self._doc is not None and self._can(Capability.TILE_BINDING)
        self._nav_stack.setCurrentWidget(self._tilemap_bar if binding else self._navbar)
        if binding:
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
        # Cells that are coordinates into another tilemap still have a base: it
        # shifts which source cell or stamp a coordinate names rather than which
        # tile (`CellChain.base`), so the spin stays and says which it counts.
        noun = self._index_noun(entry)
        self._tile_base_label.setText(f"Base {noun} ")
        self._tile_base.setToolTip(
            f"{tile_base_tip(noun, self._base_counts_cells(entry, source))} (hex)"
        )
        self._sync_index_addressing(entry, source)
        self._sync_binding_jump(source)
        self._sync_size_pair()
        self._sync_all_frames()
        self._sync_transparent_zero()
        self._sync_cell_index()
        self._sync_cell_props()
        self._tile_binding_note.setText(self._binding_note(entry, source))

    def _base_counts_cells(
        self, entry: Entry, source: TileSource | None = None
    ) -> bool:
        """Whether ``entry``'s index counts in a source **map** — cells or
        stamps — rather than in art, tiles or metatiles.

        The kind half of what the base and the index count, behind every place
        either is named (:meth:`_addressing_nouns`, :meth:`_index_noun`) and
        behind whether a rebind can carry the base across at all
        (:meth:`_rebound_source`). Read off the **binding**, not off whether the
        chain resolved: the number belongs to the binding and is in the unit of
        whatever it names, so a map bound to a tilemap that did not resolve
        still holds a cell offset, one that takes effect the moment the chain
        does. Unbound, the format decides: cells that are coordinates
        (``indirect``) name cells of the map they are waiting for.

        ``source`` is the binding to ask about, where it is not the entry's own
        yet — the one a rebind is about to land.
        """
        source = entry.tile_source if source is None else source
        bound = source.entry if source is not None else None
        if bound is not None and source.mode is TileMode.ENTRY:
            return bound.content_kind is ContentKind.TILEMAP
        return self._tilemap_is_indirect(entry)

    def _index_noun(self, entry: Entry) -> str:
        """What ``entry``'s index counts, and so its base: ``"tile"``,
        ``"metatile"``, ``"cell"`` or ``"stamp"``.

        **The** noun behind every place either number is named — the Base and
        Cell labels, their tooltips, both "set base" undo texts, the tile
        source panel's button and readout — so no two of them can disagree.
        The kind is the binding's (:meth:`_base_counts_cells`); whether it is
        the element or the unit is the addressing **in force**, what the
        document actually reads (:attr:`~celpix.core.document.Document.
        index_addressing`), not what was asked: where counting units is
        refused and each index read as a corner, the base counts elements too.
        A fontmap's glyphs and a sprite object's subsprites never count units,
        so they keep the element's word.
        """
        element, unit = self._addressing_nouns(entry)
        doc = entry.doc
        counted = (
            doc is not None
            and doc.is_tilemap
            and doc.index_addressing is IndexAddressing.ORDINAL
        )
        return unit if counted else element

    def _rebound_source(
        self, entry: Entry, bound: Entry | None, before: TileSource | None = None
    ) -> TileSource:
        """The binding a rebind of ``entry`` to ``bound`` (None: unbind) lands.

        The base and Index unit are the binding's own settings rather than the
        source's, so they carry across — but only where the kind of thing they
        count stays the same, and they start over where it changes: a tile
        offset is not a cell offset, and carrying 3 from a bank onto a tilemap
        would silently start the map three stamps in — the other way, a cell
        offset would start it three tiles into the art. Counting stamps is
        likewise not counting metatiles, so the choice goes back to the
        format's. Kept, a re-pointed map goes on reading its indices the way
        the user said, rather than every one of them silently turning back into
        a corner. Unbinding asks the same question of the unbound state, so an
        indirect layout keeps both for the next map it is pointed at.

        Where the kind stays but the **unit** the base counts in changes — the
        new source's stamps are cut differently, or cannot be counted at all —
        the base is re-counted to start at the same place, or reset to 0 where
        no whole unit starts there (:func:`~celpix.core.tilemap.rebased`, the
        rule Index unit keeps). Only between two readings both known: an
        unbound map, or a source not loaded, reads nothing to count in, so the
        number is kept as it stands.

        ``before`` is the binding the rebind starts from, where the entry no
        longer holds it (:meth:`_bind_tiles_from_file`).
        """
        after = (
            TileSource(mode=TileMode.ENTRY, entry=bound)
            if bound is not None
            else TileSource()
        )
        if self._base_counts_cells(entry, before) != self._base_counts_cells(
            entry, after
        ):
            return after
        was = before if before is not None else entry.tile_source
        base = self._tile_base.value()
        doc = entry.doc
        if bound is not None and doc is not None and was is not None and was.is_bound:
            reading = documents.rebound_reading(self._registry, entry, bound)
            if reading is not None:
                base = rebased(base, doc.addressing_geometry, reading.geometry) or 0
        return replace(
            after,
            base_index=base,
            addressing=was.addressing if was is not None else None,
        )

    # -- what an index counts ------------------------------------------------
    def _addressing_nouns(
        self, entry: Entry, source: TileSource | None = None
    ) -> tuple[str, str]:
        """``(element, unit)``: what ``entry``'s indices can count, singular.

        ``("cell", "stamp")`` over another tilemap and ``("tile", "metatile")``
        over art (``docs/design/terminology.md``), asked of the same predicate
        as the base (:meth:`_base_counts_cells`), since the base counts
        whichever of the two the index does (:meth:`_index_noun`).
        """
        if self._base_counts_cells(entry, source):
            return "cell", "stamp"
        return "tile", "metatile"

    def _addressing_moot(
        self, entry: Entry, source: TileSource
    ) -> tuple[str | None, documents.IndexReading | None]:
        """Why choosing an addressing means nothing for ``entry``, or None where
        it does — with the reading counting units would give, to say more from.

        Asked of the loaded document as it stands
        (:func:`~celpix.project.documents.document_index_reading`), with
        ordinal as the choice being weighed: where that reading has no geometry
        and nothing refused it, a unit is one element and its number *is* its
        corner. A geometry of one-element units counts the same way. A refusal
        is not moot — the choice is real, and the tooltip says why it cannot be
        honoured on this source.
        """
        element, unit = self._addressing_nouns(entry, source)
        doc = entry.doc
        if not source.is_bound or doc is None:
            return "Nothing is bound yet", None
        if doc.is_sprite:
            return "A sprite object's subsprites name their tiles directly", None
        if doc.glyph_layout is not None:
            return "A fontmap's codes already count whole glyphs", None
        bound = self._binding_target(source)
        if bound is None:
            return "The entry it drew from is no longer open", None
        if bound.content_kind is ContentKind.TILEMAP and doc.chain is None:
            return "The source map is not resolved", None
        reading = documents.document_index_reading(
            self._registry, self._workspace, entry, IndexAddressing.ORDINAL
        )
        if reading is None:
            return "The source map is not loaded", None
        if reading.refusal is None and _counts_elements(reading.geometry):
            return (
                f"Each {unit} here is one {element},\n"
                "so both ways count the same number"
            ), None
        return None, reading

    def _sync_index_addressing(self, entry: Entry, source: TileSource) -> None:
        """Name the Index unit items for this binding, and show its choice.

        The items are renamed rather than fixed because what an index counts
        is a fact about the pair: the same map bound to a tile bank counts
        tiles or metatiles, and bound to another map cells or stamps. The
        first names what clearing the override gives back — the **format's**
        word, not what is in force, which a refusal can make differ
        (:func:`~celpix.project.documents.format_addressing`).

        Disabled where the choice cannot mean anything
        (:meth:`_addressing_moot`) — unless the binding holds a choice all the
        same, which stays reachable so it can be cleared: it would otherwise
        take effect unseen the moment the source grew a unit to count.
        """
        element, unit = self._addressing_nouns(entry, source)
        said, _ = documents.format_addressing(self._registry, entry)
        combo = self._index_addressing
        format_noun = unit if said is IndexAddressing.ORDINAL else element
        combo.setItemText(0, f"From format ({format_noun.capitalize()}s)")
        combo.setItemText(1, f"{element.capitalize()}s")
        combo.setItemText(2, f"{unit.capitalize()}s")
        with signals_blocked(combo):
            combo.setCurrentIndex(_ADDRESSING_CHOICES.index(source.addressing))
        moot, reading = self._addressing_moot(entry, source)
        enabled = moot is None or source.addressing is not None
        combo.setEnabled(enabled)
        self._index_addressing_label.setEnabled(enabled)
        tip = self._index_addressing_tip(entry, source, moot, reading)
        combo.setToolTip(tip)
        self._index_addressing_label.setToolTip(tip)

    def _index_addressing_tip(
        self,
        entry: Entry,
        source: TileSource,
        moot: str | None,
        reading: documents.IndexReading | None,
    ) -> str:
        """The Index unit tooltip: what the two readings are, then whatever
        about this binding the user cannot see from the picture.

        That is three things. Why the control is off (``moot``). Why counting
        units is **refused** on this source — the document's refusal where
        units were asked for, adding that each index is being read as its
        unit's corner instead, since the combo still shows what was asked; and
        ``reading``'s forecast where they were not. And where the
        numbering hangs on a width **nobody stated**: a grid of stamps whose
        source publishes no stride and states no width is numbered along the
        source's view, so a different Cols there numbers every stamp here
        differently (:func:`~celpix.project.documents.chain_width_is_stated`).
        """
        element, unit = self._addressing_nouns(entry, source)
        where = "source map" if element == "cell" else "tile bank"
        # The first row is the only one whose name does not say what it does:
        # it states nothing itself, and the brackets are the format's answer.
        lines = [
            f"What each index counts in the {where}",
            "From format: what the cell format says, shown in brackets",
            "The other two override the format, for this map only",
            f"{element.capitalize()}s: the {unit}'s top-left {element}",
            f"{unit.capitalize()}s: the {unit}'s number, 0, 1, 2...",
            f"Base counts {element}s or {unit}s, as the index does;",
            "changing this moves it to the same place, or to 0",
            f"where no whole {unit} starts there",
        ]
        if moot is not None:
            lines.append(moot)
            return "\n".join(lines)
        # Where units are asked for, the refusal is the document's — the one
        # the picture was drawn under. Where they are not, it is the forecast of
        # choosing them, which is worth having before the choice is made.
        doc = entry.doc
        said, _ = documents.stated_addressing(self._registry, entry)
        asked = said is IndexAddressing.ORDINAL
        if asked:
            refusal = doc.addressing_refusal if doc is not None else None
        else:
            refusal = reading.refusal if reading is not None else None
        if refusal is not None:
            lines.append(f"{unit.capitalize()}s cannot be counted here:")
            lines.append(wrap_lines(refusal))
            if asked:
                lines.append(f"Each index is read as its {unit}'s corner instead")
        per_row = self._view_width_numbering(entry, source)
        if per_row:
            lines.append(
                f"{unit.capitalize()}s are numbered {per_row} to a row by the\n"
                "source's Cols - no file states that width, so a\n"
                f"different Cols there numbers every {unit} differently"
            )
        return "\n".join(lines)

    def _view_width_numbering(self, entry: Entry, source: TileSource) -> int:
        """How many stamps to a row ``entry`` numbers along its source's **view**
        width, or 0 where the numbering does not hang on it.

        Only a chained map counting stamps on a grid can: a packed table's
        stamps follow one another whatever the width, a geometry read off the
        format's record keys is its own
        (:func:`~celpix.project.documents.reads_record_keys`), and a source
        that publishes a stride or states a width has said where its rows
        break.
        """
        doc = entry.doc
        chain = doc.chain if doc is not None else None
        geometry = chain.geometry if chain is not None else None
        if geometry is None or not geometry[2]:
            return 0
        if documents.reads_record_keys(self._registry, entry):
            return 0
        bound = self._binding_target(source)
        through = bound.doc if bound is not None else None
        if through is None or documents.chain_width_is_stated(through):
            return 0
        return geometry[2]

    def _bake_binding_jump_icon(self) -> None:
        """Stamp the jump button's ring-and-dot in the theme's button-text color.

        A pixmap, so it is baked and not styled: re-run when the theme or the
        device scale changes (``_rebake_icons``), which is also why it is a
        method rather than two lines at the build site.
        """
        self._tile_binding_jump.setIcon(
            glyph_icon(
                Glyph.TARGET, QApplication.palette(), ratio=self.devicePixelRatioF()
            )
        )

    def _sync_binding_jump(self, source: TileSource) -> None:
        """Arm the jump button iff the binding names an entry, and say which.

        Gated on there being an entry to show rather than on the binding
        *resolving*: an unresolved source (a tilemap whose chain loops, or that
        did not open) is exactly the case where the user needs to go and look
        at it.
        """
        bound = self._binding_target(source)
        self._tile_binding_jump.setEnabled(bound is not None)
        self._tile_binding_jump.setToolTip(
            f"Show {bound.name} - where these tiles come from\n"
            "Back (Alt+Left) returns here"
            if bound is not None
            else "Show where the tiles come from\nNothing is bound yet"
        )

    def _sync_size_pair(self) -> None:
        """Show the subsprite sizes in force, on a sprite map and nowhere else.

        Gated on the **format**'s declaration rather than on the loaded document
        (:meth:`~...session.SessionMixin._tilemap_is_sprite`), so an object with no
        binding yet still offers it — the pair says how its own records are read,
        which is true before it has any art to read them against. The value comes
        off the document, which carries the pair in force.

        Gone entirely on a format whose records **state** each piece's rectangle
        (:meth:`~...session.SessionMixin._tilemap_states_subsprite_size`): the pair
        exists to resolve a size *bit* against, and a control that resolves nothing
        would be a spin that redraws the same picture.
        """
        entry = self._workspace.current
        sprite = (
            entry is not None
            and self._tilemap_is_sprite(entry)
            and not self._tilemap_states_subsprite_size(entry)
        )
        for widget in (
            self._size_pair_label,
            self._size_small_label,
            self._size_small,
            self._size_large_label,
            self._size_large,
        ):
            widget.setVisible(sprite)
        doc = self._doc
        if not sprite or doc is None:
            return
        small, large = doc.sprite_size_pair
        with signals_blocked(self._size_small), signals_blocked(self._size_large):
            self._size_small.setValue(small)
            self._size_large.setValue(large)

    def _on_size_pair_change(self, _value: int) -> None:
        """Redraw at a different size pair — a re-read, unlike the row base.

        The pair decides how many tiles a subsprite covers and how big its bounding
        box is, so the *frames* are built differently: it is decoded geometry rather
        than a render-time shift, and the entry has to load again to pick it up.

        Which is the whole of the route, and worth naming because every hop of it
        is somewhere else: this lands the pair on the entry, the re-read hands it
        to :func:`~celpix.pipeline.pipeline.load_tilemap_data`, and that publishes
        it for the codec to frame by
        (:data:`~celpix.core.context.KEY_TILEMAP_SUBSPRITE_TILES`). A change that
        stops short of the last hop is a spin that moves and a picture that does
        not.

        Both spins arrive here, so a gesture on either lands the pair as it now
        stands rather than only the box that moved — they are two halves of one
        answer, and the format reads both.
        """
        entry = self._workspace.current
        if entry is None or not self._tilemap_is_sprite(entry) or self._applying_undo:
            return
        pair = (self._size_small.value(), self._size_large.value())
        before = self._tilemap_binding_state(entry)
        self._push_tilemap_binding(
            entry,
            before,
            replace(before, size_pair=pair),
            f"set subsprite size to {pair[0]} or {pair[1]} tiles",
        )

    def _sync_all_frames(self) -> None:
        """Show the All Frames box on a sprite map, holding the entry's choice.

        Gated on the **format** exactly as the size pair beside it is, and for
        the same reason it is not in the capability table: having frame slots is
        a property of the cell format, not of the content kind, so a grid tilemap
        is not a document with this switched off — it has no frames to count
        (``docs/design/tilemap-entry.md`` §4). Hidden rather than disabled on that
        rule, which is the one the whole bar is built on.

        Signal-blocked, because this is a restore: the entry switch that brought
        us here has already put the value on the window, and letting the box
        re-emit would push the outgoing entry's answer onto the incoming one.
        """
        entry = self._workspace.current
        sprite = entry is not None and self._tilemap_is_sprite(entry)
        self._all_frames.setVisible(sprite)
        if not sprite:
            return
        with signals_blocked(self._all_frames):
            self._all_frames.setChecked(self._show_all_frames)

    def _on_all_frames_change(self, on: bool) -> None:
        """Redraw with the empty frame slots shown, or without them.

        No **re-read**: the frames are all decoded either way
        (``Document.sprite_frames`` holds every slot), and this only says how
        many of them the sheet lays out. That is what makes it cheap enough to
        be a checkbox rather than a reload the way the size pair beside it is.

        One undo step all the same. The answer is the entry's and the project
        file keeps it, so it is part of how the sheet is set up rather than a
        glance at it - and a sprite sheet resized from 32 slots to 8 and back is
        not something a user should have to remember the number for.

        The refresh is what lands it: the capture at the top of that cycle puts
        the window's answer into ``doc.view``, which is where the sheet geometry,
        the image and an export all read it (``Document.shown_frames``).
        """
        self._push_view_toggle("_show_all_frames", "show all frames", on)

    def _sync_transparent_zero(self) -> None:
        """Hold the entry's backdrop choice on the box.

        Always visible, unlike the two boxes before it: every indexed format has
        an index 0, so there is no tilemap this asks a meaningless question of.

        Signal-blocked for the reason :meth:`_sync_all_frames` is — this is a
        restore, and an echo would push the entry we just left onto this one.
        """
        with signals_blocked(self._transparent_zero_box):
            self._transparent_zero_box.setChecked(self._transparent_zero)

    def _on_transparent_zero_change(self, on: bool) -> None:
        """Redraw with index 0 clear, or as the colour that sits there.

        Like All Frames: nothing is re-read, because no index moves — only the
        colour table the render resolves them through changes, one entry of it
        per palette row (:func:`~celpix.ui.render_bridge.clear_zeros`) — and it
        is one undo step, because which cells read as empty is the entry's
        answer and the project file keeps it. The refresh is what lands it, via
        the view capture at the top of that cycle.
        """
        self._push_view_toggle("_transparent_zero", "clear index 0", on)

    def _push_view_toggle(self, attr: str, text: str, on: bool) -> None:
        """Push one of the bar's view switches, or land it if there is no entry.

        Shared by the two boxes above, which differ only in which field they
        drive and what the step is called. The guard is the ordinary one: a box
        re-ticked to what it already shows is not a gesture, and one moved by an
        undo apply is the apply.
        """
        if getattr(self, attr) == on or self._applying_undo:
            return
        entry = self._workspace.current
        if self._doc is None or entry is None:
            self._apply_view_toggle(attr, on)
            return
        self._push_command(
            ViewToggleCommand(
                self, entry, attr, text, before=getattr(self, attr), after=on
            )
        )

    def _sync_cell_index(self) -> None:
        """Show the selected cell's reference, ranged to what the format allows.

        Hidden where there is nothing to set — a sprite object, or a format with
        no index field: a control that means nothing here is not a feature
        switched off. Disabled rather than hidden
        with no selection, because then it is the *selection* that is missing and
        the control is about to become useful again.

        The one control on this bar driven by the **selection** pass as well as by
        the refresh (:meth:`~...selection.SelectionMixin._sync_selection_actions`),
        and it has to be: every other control here answers to the entry, which only
        changes through a render, while this one answers to what is selected — and a
        selection moves without anything being redrawn.

        Whether it applies at all is three questions in one
        (:meth:`~...tilemap_edit.TilemapEditMixin._cell_reference_settable`), the
        same predicate the Edit Tiles tool arms on. Only the first of the three is
        the capability table's, which is why this gate stays here rather than
        moving into the gating pass: that pass runs after this one, and its
        blanket visibility would put the spin back on a sprite object
        (:mod:`~celpix.ui.main_window.capability_sync`).
        """
        runs = self._cell_index_runs()
        usable = self._cell_reference_settable()
        self._cell_index_label.setVisible(usable)
        self._cell_index.setVisible(usable)
        if not usable or runs is None:
            return
        selected = bool(self._selected_cells())
        self._cell_index.setEnabled(selected)
        entry = self._workspace.current
        noun = self._index_noun(entry) if entry is not None else "tile"
        self._cell_index_label.setText(f"{noun.capitalize()} ")
        self._cell_index.setToolTip(f"{self._cell_index_tip(noun)} (hex)")
        with signals_blocked(self._cell_index):
            # Only what a stored cell can name: a floor, a top and any gap
            # between (:class:`~celpix.ui.widgets.RunSpinBox`).
            self._cell_index.set_runs(runs)
            self._cell_index.setValue(self._selected_cell_index())

    def _cell_index_tip(self, noun: str) -> str:
        """The Cell spin's tooltip, naming what the number counts here.

        ``noun`` is :meth:`_index_noun`'s, the label's own word, so the two
        cannot disagree: the same spin holds a source cell or a stamp number on
        a chained map, and a tile or a metatile number over art, as Index unit
        and the addressing in force say.
        """
        what = "source cell" if noun == "cell" else noun
        return (
            f"The {what} the selected cells name - set it to point\nthem somewhere else"
        )

    def _on_cell_index_change(self, value: int) -> None:
        if self._applying_undo:
            return
        self._set_cell_index(value)

    def _binding_note(self, entry: Entry, source: TileSource) -> str:
        """One line saying where the tiles come from, and how they are read.

        The pixel format is the bound entry's own — a tilemap does not get a
        second opinion about it — so this reports it rather than offering a
        control that would fight the entry's own picker.
        """
        if not source.is_bound:
            return (
                "No source bound - this layout draws nothing until a tilemap is."
                if self._tilemap_is_indirect(entry)
                else "No tiles bound - every cell draws blank."
            )
        bound = self._binding_target(source)
        if bound is None:
            return "The entry it drew from is no longer open."
        if bound.content_kind is ContentKind.TILEMAP:
            doc = entry.doc
            if doc is None or doc.chain is None:
                # Keyed on the document, which is what the resolution produced,
                # so the line cannot claim a stamping that did not happen. Names
                # the broken link rather than repeating "no source": the binding
                # here may be fine, and it is one further down that has to move.
                return f"{self._chain_refusal(entry, bound)} - not resolved."
            # A chained map takes its source's tiles *and* its attributes, so
            # there is no pixel format of its own to report - the source's is the
            # one that matters, and it is on that entry's own bar. What is worth
            # saying instead is which edit lands where: a cell here restamps, and
            # the stamp itself is edited on the entry named - or, down a longer
            # chain, on whichever of the maps named holds the part to change.
            further = [
                hop.name
                for hop in self._chain_entries(entry)[1:]
                if hop.content_kind is ContentKind.TILEMAP
            ]
            if further:
                return (
                    f"Stamped from {bound.name} via {', '.join(further)}"
                    " - edit them to change the stamps."
                )
            return f"Stamped from {bound.name} - edit it there to change the stamps."
        preset = bound.session.pixel_preset_id if bound.session is not None else ""
        try:
            name = self._registry.preset(preset).name
        except KeyError:
            name = preset or "its own format"
        return f"Tiles from {bound.name}, read as {name}."

    def _binding_combo_rows(self, entry: Entry) -> tuple[object, ...]:
        """Everything the binding combo's **rows** are made of, in one cheap pass.

        Refilling is not cheap — :meth:`~...session.SessionMixin._can_supply_tiles`
        resolves each candidate's own binding against the open list, so the scan
        is quadratic in the entries — and the bar is refreshed from every render.
        On a project of several hundred files that was a rebuild per edit, which
        is the lag it buys back.

        The rows change only when the list does, and this says so without
        resolving anything: each entry itself, the name that is shown, the two
        kinds the rule reads, the cell format that says whether a tilemap is a
        sprite object, and the raw binding that decides whether a tilemap can
        pass tiles on. The entries ride in the tuple rather than their ids
        precisely so they stay alive to be compared — :class:`Entry` is
        ``eq=False``, so a comparison of two of these is a row-by-row identity
        check.
        """
        return (
            entry,
            self._tilemap_is_indirect(entry),
            tuple(
                (
                    other,
                    other.name,
                    other.kind,
                    other.content_kind,
                    other.tilemap_preset_id,
                    other.tile_source.entry if other.tile_source is not None else None,
                )
                for other in self._workspace.entries
            ),
        )

    def _fill_binding_combo(self, entry: Entry, source: TileSource) -> None:
        combo = self._tile_binding
        rows = self._binding_combo_rows(entry)
        if rows != self._binding_combo_rows_shown:
            self._binding_combo_rows_shown = rows
            combo.clear()
            combo.addItem("(none)", _NONE)
            # Any tilemap may draw through another one, so the list is both kinds,
            # filtered by the single rule that says which entries qualify
            # (``_can_supply_tiles``). What the format contributes is only the
            # *order*: a map whose cells are coordinates cannot read a tile bank
            # sensibly, so its tilemaps come first and the banks stay reachable
            # instead of being hidden on the strength of a preset flag.
            candidates = [
                candidate
                for candidate in self._workspace.entries
                if self._can_supply_tiles(entry, candidate)
            ]
            if rows[1]:  # the map's cells are coordinates - see _binding_combo_rows
                # Stable, so entry order survives inside each group.
                candidates.sort(key=lambda e: e.content_kind is not ContentKind.TILEMAP)
            for candidate in candidates:
                combo.addItem(candidate.name, candidate)
            combo.addItem("From file...", _FROM_FILE)
        # Always, rows rebuilt or not: which of them is *current* follows the
        # binding, and that moves without the list of candidates moving at all.
        if source.mode is TileMode.ENTRY:
            at = combo.findData(source.entry)
            # A binding whose entry has since been closed keeps holding it, and
            # the combo only lists entries that are open — so it has nothing to
            # select and shows as unbound until the entry comes back or the map
            # is re-pointed.
            combo.setCurrentIndex(at if at >= 0 else 0)
        else:
            combo.setCurrentIndex(0)

    def _preset_name(self, preset_id: str) -> str:
        """What the registry calls ``preset_id``, or the id when it has nothing.

        For the messages that name a format to the user. Reading it back rather
        than taking the combo's text is what keeps the picker free to decorate its
        entries without every message it feeds inheriting the decoration.
        """
        try:
            return self._registry.preset(preset_id).name
        except KeyError:
            return preset_id

    def _fill_codec_combo(self, entry: Entry) -> None:
        """Put the tilemap formats into the codecs toolbar's picker.

        Filled from here rather than at build time because the selection is the
        *entry's*, and this is the pass that runs whenever one is shown.

        The format **in force**, not the entry's stored field: a tilemap no
        container named a codec for is read under the default
        (:meth:`~...session.SessionMixin._tilemap_preset_id`), and showing an
        empty selection over it would name a format the canvas is not drawing.

        Every entry is tagged with the **layout** it declares
        (:func:`~celpix.ui.searchable_combo.tilemap_codec_label`)
        because that is the part of the choice the names do not carry evenly:
        "Text run" says what it is, "Sprite object subsprite (OBJ/OBX)" leaves you
        to know that a subsprite makes it a sprite map, and picking across layouts
        is not a change of byte order — it is a different kind of document, with a
        different bar under it and a different window over it.
        """
        fill_grouped(
            self._tilemap_preset,
            preset_rows(
                self._registry.presets(Stage.INTERPRET_TILEMAP), tilemap_codec_label
            ),
            self._tilemap_preset_id(entry),
        )

    # -- edits ---------------------------------------------------------------
    def _on_tile_binding_change(self, _index: int) -> None:
        entry = self._workspace.current
        if entry is None or self._applying_undo:
            return
        data = self._tile_binding.currentData()
        if data is _FROM_FILE:
            self._bind_tiles_from_file(entry)
            return
        # The base and Index unit ride in the same TileSource as the binding,
        # so a reset of them lands, and comes back off, in the one step
        # (:meth:`_rebound_source`).
        if data is _NONE or data is None:
            source = self._rebound_source(entry, None)
            text = "unbind tiles"
        else:
            if not self._settle_font_declaration(entry, data):
                self._refresh_tilemap_bar()  # cancelled: put the combo back
                return
            source = self._rebound_source(entry, data)
            text = f"bind tiles to {self._tile_binding.currentText()}"
        self._rebind_tiles(entry, source, text)

    def _settle_font_declaration(self, entry: Entry, bound: Entry) -> bool:
        """Offer to declare ``bound`` a font where ``entry`` is a fontmap.

        A string's codes only mean anything through a sheet ticked **Use as
        Font**, and the tick lives on the *sheet* — so binding one is the moment
        the user has said which sheet they mean, and the only moment the question
        can be asked without them having to go and find the other entry to answer
        it. Declining it is a real answer (the bank may be art bound to a map
        that merely reads as text), so the binding is called off rather than
        landed unread: a fontmap drawn through an undeclared sheet shows hex and
        nothing on this bar explains why.

        True where the caller may go ahead. **Two undo steps, not one**, the
        shape a bind from file already has: the declaration is pushed first so
        the map reloads with the alphabet, and one Ctrl+Z takes the binding back
        while a second undeclares the font.
        """
        # The *format's* answer rather than the loaded document's, so it holds on
        # the from-file route as well - opening the sheet activates another entry
        # and this one may not be the one on screen by the time it is asked. Only
        # a pixels entry can be a font: a stamp layout's chained tilemap has no
        # tiles of its own to spell.
        if (
            not self._tilemap_is_fontmap(entry)
            or bound.is_font_sheet
            or bound.content_kind is not ContentKind.PIXELS
        ):
            return True
        if not self._confirm(
            f"{entry.name} reads its cells as text, and {bound.name} is not "
            "marked as a font.\n\nMark it as one so its tiles can be spelled?",
            title="celPix - use as font",
            accept="Use as Font",
        ):
            return False
        self._declare_use_as_font(bound)
        return True

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

        **Two undo steps, not one.** Opening the file is its own
        ``AddEntryCommand`` and the bind is a second step on top of it — the same
        shape registering a palette file and then applying it already has. One
        Ctrl+Z therefore leaves the file open and unbinds, which is the useful
        half to take back; a second closes it.

        The prompt names what this entry is most likely after — a stamp layout
        wants the kind of map it was authored against, and asking it for
        "tiles" would name the thing its coordinates cannot read. The format
        itself supplies the noun (``source_hint`` in its preset params), since
        only it knows what its files are authored against; a stamped format
        that declares none is asked for a tilemap. Only the wording follows
        the format, though: what the dialog *accepts* is whatever a binding
        accepts (``_can_supply_tiles``).
        """
        hint = self._tilemap_declares(entry, "source_hint")
        if self._tilemap_is_indirect(entry):
            wanted = hint if isinstance(hint, str) and hint else "Tilemap"
            title = f"{wanted} for this stamp layout"
        else:
            title = "Tiles for this tilemap"
        # Snapshotted before the open, which activates another entry and can
        # re-read this one: the step being pushed is the *bind*, so its starting
        # point is the binding as the user found it.
        before = self._tilemap_binding_state(entry)
        path, _ = QFileDialog.getOpenFileName(self, title)
        if not path:
            self._refresh_tilemap_bar()  # cancelled: put the combo back
            return
        self._load_pixel(path)
        bound = self._workspace.find_file(path)
        if bound is None or not self._can_supply_tiles(entry, bound):
            # Nothing this map can draw through: not a graphics file, or a tilemap
            # whose chain loops back and so has no tiles to lend. It
            # stays open, since the user asked for it, but the binding is left
            # alone rather than pointed somewhere useless.
            self._activate_entry(entry)
            self._refresh_tilemap_bar()
            return
        if not self._settle_font_declaration(entry, bound):
            # Cancelled. The file stays open - it is the user's own open and the
            # second undo step never existed - but the map is left where it was.
            self._activate_entry(entry)
            self._refresh_tilemap_bar()
            return
        # Asked of the binding as the user found it.
        source = self._rebound_source(entry, bound, before.tile_source)
        entry.tile_source = source  # before the switch back, so the reload reads it
        self._activate_entry(entry)
        if self._workspace.current is not entry:
            # The switch back failed (reported by the load): the map is not on
            # screen, so there is no bind to push. Pushing anyway would leave a
            # dead step — its redo cannot reach the map either, so it applies
            # nothing — with the field it names already moved underneath it.
            entry.tile_source = before.tile_source
            self._refresh_tilemap_bar()
            return
        self._rebind_tiles(entry, source, f"bind tiles to {bound.name}", before=before)

    def _jump_to_bound_tiles(self) -> None:
        """Show the entry this map draws its tiles from - the button beside the combo.

        Navigation and nothing else: the bound entry is already a first-class
        entry with a format, a view and a session of its own, so there is nothing
        to reconfigure on arrival (unlike Jump to Source, which has to re-read a
        parent under its slice's settings) and nothing to push onto the undo
        stack. The way back is the window's own Back, which the switch has just
        recorded (:mod:`celpix.ui.main_window.history`).

        Read off the entry's live binding rather than off the combo: the button is
        armed from the same binding the note describes, and the two must not be
        able to disagree about where "there" is.
        """
        entry = self._workspace.current
        if entry is None:
            return
        bound = self._binding_target(entry.tile_source or TileSource())
        if bound is None or bound is entry:
            return
        self._activate_entry(bound)
        if self._workspace.current is bound:
            self.statusBar().showMessage(
                f"Showing {bound.name} - Back returns to {entry.name}"
            )

    def _on_tile_base_change(self, value: int) -> None:
        entry = self._workspace.current
        if entry is None or self._applying_undo:
            return
        source = entry.tile_source or TileSource()
        self._rebind_tiles(
            entry,
            replace(source, base_index=value),
            f"set base {self._index_noun(entry)} to ${value:X}",
        )

    def _on_index_addressing_change(self, index: int) -> None:
        """Count this map's indices as elements, as units, or as its format says.

        The base spin's route (:meth:`_rebind_tiles`): the choice rides in the
        binding, so it is one undo step, a re-read, and the bar and the tile
        source panel follow from the refresh that ends it. The **base moves
        with it**, in the same step, since it counts what the index counts: it
        is re-counted to start the map at the same place in the source where a
        whole unit starts there, and reset to 0 where none does
        (:func:`~celpix.core.tilemap.rebased`) — from the reading in force to
        the one the choice will be read under, so a refused ordinal counts
        elements on either side. The undo text says so when the base moved.

        The held pick is carried to the number that now names the same place
        (:meth:`~...tile_source_dock.TileSourceDockMixin._repoint_source_pick`):
        the sheet's IDs change meaning under it, and a ring left on the old
        number would sit on a different picture.
        """
        entry = self._workspace.current
        if entry is None or self._applying_undo:
            return
        if not 0 <= index < len(_ADDRESSING_CHOICES):
            return
        choice = _ADDRESSING_CHOICES[index]
        source = entry.tile_source or TileSource()
        if choice == source.addressing:
            return
        element, unit = self._addressing_nouns(entry, source)
        if choice is None:
            text = "count indices as the format says"
        elif choice is IndexAddressing.CORNER:
            text = f"count indices as {element}s"
        else:
            text = f"count indices as {unit}s"
        base, counted = self._recounted_base(entry, source, choice)
        if base == 0 and source.base_index:
            text += " and reset the base"
        elif base != source.base_index:
            text += f" and move the base to {unit if counted else element} ${base:X}"
        origin = self._source_pick_origin()
        self._rebind_tiles(
            entry, replace(source, addressing=choice, base_index=base), text
        )
        # Only where the change landed: a re-read that failed put the entry back
        # as it was, and so is the sheet the pick addresses.
        if (entry.tile_source or TileSource()).addressing is choice:
            self._repoint_source_pick(origin)

    def _recounted_base(
        self, entry: Entry, source: TileSource, choice: IndexAddressing | None
    ) -> tuple[int, bool]:
        """``source``'s base counted the way ``choice`` will read ``entry``'s
        indices — the same place in the source, or 0 where no whole unit starts
        there — and whether that reading counts units.

        Kept as it stands where either reading is unknown: nothing is loaded to
        read the geometry off, so there is no place to carry it to.
        """
        doc = entry.doc
        asked = choice or documents.format_addressing(self._registry, entry)[0]
        reading = documents.document_index_reading(
            self._registry, self._workspace, entry, asked
        )
        if doc is None or reading is None:
            return source.base_index, asked is IndexAddressing.ORDINAL
        base = rebased(source.base_index, doc.addressing_geometry, reading.geometry)
        return base or 0, reading.geometry is not None

    def _on_tilemap_preset_change(self, _index: int) -> None:
        """A different cell format for this entry — re-read it under the new one.

        Unlike a pixel preset, which only re-interprets a buffer already in hand,
        this changes how many bytes a cell is and so what the grid *is*; the load
        path is the only thing that knows how to rebuild that.
        """
        entry = self._workspace.current
        if (
            entry is None
            or entry.content_kind is not ContentKind.TILEMAP
            or self._applying_undo
        ):
            return
        before = self._tilemap_binding_state(entry)
        preset_id = str(self._tilemap_preset.currentData())
        # A format that reads the cells as text takes **Use as Font** off the
        # entry with it: a string is not a sheet of letters, so a map left ticked
        # would offer itself as the font for another map and read that one
        # through its own table. Only stale state can be on to clear — the tick
        # is offered on a pixels entry alone — and it goes in this step, so one
        # Ctrl+Z puts the format and the declaration back together.
        after = replace(before, preset_id=preset_id)
        if self._preset_declares(preset_id, "layout") == "text":
            after = replace(after, use_as_font=False)
        # The format's own name, not the combo's text: that carries the layout tag
        # ("[S] Sprite object subsprite"), which is a column marker and reads as
        # noise in an Undo menu entry.
        self._push_tilemap_binding(
            entry,
            before,
            after,
            f"switch cell format to {self._preset_name(preset_id)}",
        )

    def _rebind_tiles(
        self,
        entry: Entry,
        source: TileSource,
        text: str = "bind tiles",
        *,
        before: TilemapBindingState | None = None,
    ) -> None:
        """Point ``entry`` at ``source`` and re-read it, as one undoable step.

        Through the load path whichever field moved. A change of *source* has to
        go that way — different bytes arrive no other route — and the base index
        follows it rather than being patched onto the document in place, so there
        is one way a binding takes effect instead of two that could disagree
        about what else a rebind settles (:meth:`_apply_tilemap_binding`).

        ``before`` overrides the snapshot this would take for itself, for the one
        caller that has already pointed the entry at the source so the switch
        back to it reads the right bytes (:meth:`_bind_tiles_from_file`).
        """
        if before is None:
            before = self._tilemap_binding_state(entry)
        after = self._seeded_palette(entry, source, replace(before, tile_source=source))
        self._push_tilemap_binding(entry, before, after, text)

    def _seeded_palette(
        self, entry: Entry, source: TileSource, state: TilemapBindingState
    ) -> TilemapBindingState:
        """``state`` carrying the colours its tiles are read in, where it seeds.

        The rule a new slice already follows: an entry is *seeded* from a related
        one at creation and owns its palette from that moment on
        (``docs/design/tilemap-entry.md`` §3). A tilemap's related entry is the
        one it binds to, and the moment is the bind — the tiles were authored
        against the bank's palette, so arriving on the built-in default means
        every freshly bound map opens in colours nothing in the project chose.

        Seeded **only while the map is still on the default palette**, which is
        what keeps this from being a live read-through. Re-pointing the tile
        source later does not re-seed: by then the palette is the tilemap's own
        state, and replacing it would discard work done on it.

        A bank that is itself on the default palette has nothing to give and is
        left alone rather than copied — the two look identical on screen, and
        copying would arm the guard against a later bind that *does* have
        colours to offer.

        Computed rather than applied, so the seed becomes part of the step the
        bind pushes and comes back off with it: what lands where is
        :meth:`_apply_tilemap_binding`'s to say, in both directions.
        """
        # Asked of the snapshot rather than of the session, which is the same
        # answer one switch out of date for the entry on screen
        # (:meth:`_tilemap_binding_state`) - and seeding over a palette the user
        # has just chosen is exactly what this guard exists to prevent.
        if entry.session is None or state.palette_mode is not PaletteMode.DEFAULT:
            return state
        # A palette the project restored but the entry has not loaded yet is
        # still the entry's own answer: pending, not absent.
        if entry.pending_palette is not None or entry.missing_palette is not None:
            return state
        bound = self._binding_target(source)
        if bound is None or bound is entry or bound.session is None:
            return state
        seed = palette_source_for(bound)
        if seed is None:
            return state
        return replace(
            state,
            palette_mode=bound.session.palette_mode,
            palette_preset_id=bound.session.palette_preset_id,
            # Consumed by the reload's _apply_restored_state — the same one-shot
            # hand-off a project-restored palette arrives through, so an Offset
            # seed resolves against the newly bound tiles, the file it came from.
            pending_palette=seed,
        )

    # -- one step, one apply -------------------------------------------------
    def _tilemap_binding_state(self, entry: Entry) -> TilemapBindingState:
        """``entry``'s binding as it stands — the undo *before* of any gesture.

        Read off the **entry** and its session rather than off the loaded
        document, because that is where the binding lives: the document carries
        the size pair *in force*, which is the format's answer where the entry
        states none, and restoring that number would pin a value the user never
        chose (:meth:`~...session.SessionMixin._size_pair_for`).

        The **palette half comes off the window** where this is the entry on
        screen. A session is only captured on the way *out* of an entry
        (:meth:`~...session.SessionMixin._capture_session`), so what it holds for
        the current one is as old as the last switch — and every reader here is
        downstream of this snapshot: it is written back to the session by
        :meth:`_write_tilemap_binding`, from where the reload reads the colours it
        carries across (:func:`~celpix.project.workspace.palette_source_for`). A
        map given a palette and then rebound without leaving the entry would
        otherwise come back on the default one, its dock still naming the palette
        that was dropped.
        """
        session = entry.session
        mode = session.palette_mode if session is not None else PaletteMode.DEFAULT
        preset = session.palette_preset_id if session is not None else ""
        # Gated on there being a document as well, for the reason the capture is:
        # an unavailable entry keeps `current` on itself with nothing loaded, and
        # the widgets are then showing no entry's answer at all.
        if entry is self._workspace.current and entry.doc is not None:
            mode, preset = self._palette_mode, self._palette_preset_id()
        return TilemapBindingState(
            tile_source=entry.tile_source,
            preset_id=entry.tilemap_preset_id,
            size_pair=entry.sprite_size_pair,
            palette_mode=mode,
            palette_preset_id=preset,
            pending_palette=entry.pending_palette,
            use_as_font=entry.use_as_font,
        )

    def _push_tilemap_binding(
        self,
        entry: Entry,
        before: TilemapBindingState,
        after: TilemapBindingState,
        text: str,
    ) -> None:
        """Land ``after`` as one undoable step, unless it changes nothing.

        The no-change guard is what keeps a spin re-entered at the value it
        already held — or a combo put back on the entry it was already bound to
        — from costing a step that would appear to do nothing when it came back.
        """
        if self._applying_undo or after == before:
            return
        self._push_command(TilemapBindingCommand(self, entry, text, before, after))

    def _apply_tilemap_binding(
        self, entry: Entry, state: TilemapBindingState, previously: TilemapBindingState
    ) -> bool:
        """Land ``state`` on ``entry``; False when the re-read it needed failed.

        The single application path, so a gesture, an undo and a redo settle a
        binding identically. Every field here changes what the document *is* — a
        different source or size pair decodes different tiles and different
        frames, a different cell format changes how many bytes a cell even is —
        so landing one always means reading the entry again.

        ``previously`` is the *other end of the step*, not what the entry holds
        now, and the difference is load-bearing: one push site points the entry
        at its new source before this runs, so reading the entry here would say
        the source had not moved and land a bind in place with the tiles unread.

        **All of it or none of it.** A binding the entry cannot be read under —
        a cell format its bytes do not fit — goes straight back, because the
        alternative is a bar describing a read that did not happen: the cell
        format picker on one format and the canvas still drawn in another. There
        is nothing to undo in that case and the caller says so with the ``False``
        (:class:`~celpix.ui.undo_commands.TilemapBindingCommand`).

        The widgets need nothing here — every route ends in a refresh, and the
        bar is filled from the render cycle (:meth:`_sync_tilemap_bar`), so the
        spins and the combo follow whatever has just landed.

        The **file list is the exception**, because it is not on that route: a
        row's icon and tooltip name which of the three layouts the entry holds,
        and that comes off the cell format (:meth:`~celpix.ui.file_list_panel.
        FileListPanel._tilemap_layout`) — which this is the one thing that moves.
        A map carved out by hand starts with no format named, so picking a text
        run here is exactly when a row becomes a fontmap; without this it would
        keep the grid glyph until the project was next opened. Only on the way
        in: the failed path puts the fields back, so its row never moved.
        """
        self._write_tilemap_binding(entry, state, previously)
        if self._reload_tilemap(entry):
            self._files_panel.refresh_entry(entry)
            return True
        # The document the re-read dropped is back (:meth:`_reload_tilemap`), so
        # putting the fields back is the whole of undoing this — and it must not
        # read again, or a binding that fails would fail twice on the way out.
        self._write_tilemap_binding(entry, previously, state)
        if self._doc is not None:
            self._refresh_view()
        return False

    def _write_tilemap_binding(
        self, entry: Entry, state: TilemapBindingState, previously: TilemapBindingState
    ) -> None:
        """Put ``state``'s fields on ``entry``, and nothing else.

        No read and no refresh: what a change of these costs is the caller's to
        decide, and the two callers want opposite things — one is applying a
        binding, the other taking a failed one back off.
        """
        entry.tile_source = state.tile_source
        entry.tilemap_preset_id = state.preset_id
        entry.sprite_size_pair = state.size_pair
        entry.pending_palette = state.pending_palette
        entry.use_as_font = state.use_as_font
        session = entry.session
        if session is None:
            return
        session.palette_mode = state.palette_mode
        session.palette_preset_id = state.palette_preset_id
        # The map is the entry on screen, and a reload does not restore a session
        # — so a seeded (or un-seeded) mode has to move with it, or the dock would
        # read Default over the seeded colours and the next entry switch would
        # capture that back over the seed.
        if (
            state.palette_mode is not previously.palette_mode
            and entry is self._workspace.current
        ):
            self._set_palette_mode(state.palette_mode)

    def _reload_tilemap(self, entry: Entry) -> bool:
        """Re-read ``entry`` under its current binding; False if it could not be.

        The document is dropped rather than patched: which bytes there are comes
        out of Read, and a binding change is a change of *which file*. Its pixel
        half is another entry's, and unsaved edits to it live *there* — the map is
        given that entry's live buffer rather than a re-read of the file
        (:meth:`~...session.SessionMixin._live_bound_tiles`), so a rebind cannot
        take a pixel edit made through the map back out again
        (``docs/design/tilemap-entry.md`` §8.4).

        **The map's own cells are handed to the read the same way.** They live in
        this document and nowhere else until a save, so reading the file for them
        would take an unsaved edit back out — silently, since nothing about a
        binding change says the cells were going to be re-read at all. What goes
        across is the encoded buffer rather than the cell list, so a change of
        *cell format* re-reads the edit under the new codec instead of dropping
        it: the bytes are the edit, and which format they are read in is the
        question that gesture is asking (:func:`~celpix.pipeline.pipeline.
        load_tilemap_data`).

        The **palette is the exception**, and has to be handed across explicitly.
        A Custom palette lives in the document and nowhere else, so dropping the
        document drops the colours with it — nudging the base tile by one would
        cost the user their palette. Carried the way a project restore and a new
        slice already carry one, through the entry's pending source. An
        unconsumed pending palette is left alone: it is a seed, or a restore,
        that is already the answer and has not reached a document yet.

        **The view goes across the same way**, and for a reason the palette's
        does not cover: the width a format states is applied to Cols on load
        (:meth:`~...rendering.RenderingMixin._apply_tilemap_columns`), which a
        re-read is one of — so a map read at any width but its format's would
        snap back to that width every time the base tile moved. Handing the old
        view over is what says this document has been seen before, and it carries
        the rest of the axes with it rather than leaving them to be recaptured
        from the widgets, which only happens for the entry actually on screen.

        The read itself is :meth:`_reread_tilemap`, because it is also what an
        entry *off* screen needs when its binding stops reaching anything; this
        method is that read plus the four lines that put the result on screen.
        """
        if not self._reread_tilemap(entry):
            return False
        self._doc = entry.doc
        # A map drawing through this one snapshotted the document just replaced
        # — its cells, its own chain and its art — so a rebind or a codec switch
        # here is one there too, at every depth above.
        self._reresolve_bound_art(self._chain_dependents(entry))
        # A re-read is where a binding takes effect, so it is where pixel mode can
        # stop being available without the view having moved: unbind a map that is
        # being painted on and the mode would otherwise stay armed with both
        # toggles greyed, leaving no way out of it but switching entries.
        self._drop_unavailable_edit_mode()
        self._refresh_view()
        self._refresh_project_modified()
        return True

    def _reread_tilemap(self, entry: Entry, *, quiet: bool = False) -> bool:
        """Re-read ``entry``'s document under its current binding; False if not.

        The half of :meth:`_reload_tilemap` that touches only the entry, so a map
        that is not on screen can be re-read as well — which is what a bank being
        closed or restored under it needs
        (:meth:`~...session.SessionMixin._reresolve_bound_art`). ``quiet``
        suppresses the failure modal for those callers: the gesture was about
        another entry, and a stack of dialogs about maps the user did not touch
        is not what a removal should produce.

        **A failed read puts the old document back.** Dropping it is how a
        re-read starts, but a drop that is never replaced leaves the entry with
        no document while the window is still showing the one it had: from then
        on the answer to "what is this entry" depends on whether you ask the
        entry or the window, a save writes through a document the entry has
        disowned, and switching away captures nothing and switching back reports
        the file as missing. So the old document is held until the new one is in
        hand, and the entry ends up either re-read or exactly as it was.
        """
        pending = entry.pending_palette
        if pending is None:
            entry.pending_palette = palette_source_for(entry)
        previous, entry.doc = entry.doc, None
        pending_view = entry.pending_view
        # An unconsumed pending view is left alone on the palette's rule above:
        # it is a restore that has not reached a document yet, and so is already
        # the newer answer.
        if pending_view is None and previous is not None:
            entry.pending_view = previous.view
        # Only where the entry actually holds an edit: with nothing unsaved the
        # buffer and the file agree, and reading the file is the plainer answer.
        live = (
            previous.tilemap_data
            if previous is not None and previous.is_tilemap and entry.pixel_dirty
            else None
        )
        if not self._load_entry(entry, quiet=quiet, live=live):
            # The seed goes back with it: it described the read that did not
            # happen, and the document being restored has its palette already.
            self._restore_document(entry, previous)
            entry.pending_palette = pending
            entry.pending_view = pending_view
            return False
        return True
