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
itself bound to. One hop, gated on depth rather than on format
(``docs/design/tilemap-entry.md`` §3.1) — so what the bar offers is filtered by
:meth:`~...session.SessionMixin._can_supply_tiles` and nothing here decides it
twice.

Every control on the bar that sets **project state the file does not record** is
one undoable step, and the binding, the base and the size pair share one snapshot
type and one apply — so a gesture, an undo and a redo settle a binding by the
same route (:class:`~celpix.ui.undo_commands.TilemapBindingState`,
:meth:`~TilemapBarMixin._apply_tilemap_binding`). Each of those changes what the
document *is*, so landing one drops it and reads the entry again.

Some of the controls sit outside that. **All Frames** and **Transparent 0** say
how much of an already-decoded document to show, which is the reading Show
Rearranged Tiles gets: a view toggle, no undo step and no re-read. **Alphabet**
and **Base code** are undoable — they are project state — but still no re-read,
and they are the ones whose value belongs to a *different entry*: a fontmap's
letters, and where they start, are the font's
(:meth:`~TilemapBarMixin._sync_alphabet`). Those two settle one pair together,
since picking an alphabet returns its origin to zero.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from celpix.core.capabilities import Capability, ContentKind
from celpix.core.errors import Stage
from celpix.pipeline import pipeline
from celpix.project.workspace import (
    Entry,
    PaletteMode,
    TileMode,
    TileSource,
    palette_source_for,
)
from celpix.ui.searchable_combo import (
    SearchableComboBox,
    fill_grouped,
    preset_rows,
)
from celpix.ui.undo_commands import (
    AlphabetCommand,
    TilemapBindingCommand,
    TilemapBindingState,
)
from celpix.ui.widgets import (
    signals_blocked,
    source_icon,
)

# What the "Tiles" combo holds besides the open entries. Distinct objects rather
# than strings so an entry named "From file..." cannot collide with the action.
_NONE = object()
_FROM_FILE = object()

# The tag the cell-format picker puts in front of each entry, keyed by the layout
# its preset declares — the same three the Files list draws a glyph for
# (:meth:`~celpix.ui.file_list_panel.FileListPanel._entry_marker`), and read the
# same way: the *format's* answer, available before anything is loaded or bound.
# A single bracketed letter rather than the word, because it prefixes a name that
# already fills the picker and its job is to be scanned down a column, not read.
# A plain grid map is the default, so it is what an unlisted layout gets.
_LAYOUT_TAG = {"sprite": "[S]", "text": "[F]"}
_GRID_TAG = "[T]"


def _codec_label(preset) -> str:  # noqa: ANN001 — a Preset, imported for typing only
    """A cell format's picker text: its layout tag, then its name."""
    return f"{_LAYOUT_TAG.get(preset.params.get('layout'), _GRID_TAG)} {preset.name}"


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
        # Wider than the mode pickers because it holds *file names*, which have no
        # bound at all — and a stated width is what stops it resizing the bar
        # every time the user opens an entry with a longer name than the last.
        self._tile_binding = SearchableComboBox(160)
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

        row.addSpacing(12)
        self._tile_base_label = QLabel("Base tile ")
        row.addWidget(self._tile_base_label)
        # Signed, because the useful direction for a slice is the negative one:
        # a map numbering from 0x100 bound to a slice that starts there needs
        # cell 0x100 to draw tile 0 (:class:`TileSource`).
        self._tile_base = self._tilemap_hex_spin(
            -0xFFFF,
            0xFFFF,
            "Shifts every cell: cell N draws source tile base + N\n"
            "Negative when the map starts partway into its source",
        )
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
            "each a square that many tiles on a side\n"
            "No file records the pair - it was a hardware register"
        )
        self._size_small_label = QLabel("Sm ")
        self._size_small_label.setToolTip(tip)
        row.addWidget(self._size_small_label)
        self._size_small = self._spin(1, 8, 1, self._on_size_pair_change)
        self._size_small.setToolTip(tip)
        row.addWidget(self._size_small)
        self._size_large_label = QLabel(" Lg ")
        self._size_large_label.setToolTip(tip)
        row.addWidget(self._size_large_label)
        self._size_large = self._spin(1, 8, 2, self._on_size_pair_change)
        self._size_large.setToolTip(tip)
        row.addWidget(self._size_large)

        # Beside the pair because it is the other half of "what is on this
        # sheet", and sprite-only for the same reason: only a sprite map has
        # frame *slots* it may not have filled. A file has room for a fixed 32 or
        # 128 of them and most are empty, so the strip stops after the last one
        # holding a drawn subsprite; this shows the rest.
        #
        # Not undoable, unlike everything else on this bar. Those set project
        # state the file does not record - a binding, a size pair - and this only
        # says how much of the file to look at, which is the reading Show
        # Rearranged Tiles and Show Palette Regions already get.
        self._all_frames = QCheckBox("All Frames")
        self._all_frames.setToolTip(
            "Show every frame slot the file has room for\n"
            "Off stops after the last frame that draws something -\n"
            "a file holds 32 or 64 and most are empty"
        )
        self._all_frames.toggled.connect(self._on_all_frames_change)
        row.addWidget(self._all_frames)

        # A **fontmap**'s one piece of state no file records: what its font's
        # tiles spell (``docs/design/fontmap-entry.md`` §3). Fontmap-only and
        # hidden elsewhere, the rule this whole bar follows - an ordinary tilemap
        # indexes arbitrary art, and asking which letter tile 5 is would be a
        # question about nothing.
        #
        # It sits here, on the *map*, and writes to the entry the Tiles combo
        # names - the one control on this bar whose value belongs to another
        # entry. That is deliberate on both counts: the fact is the **font's**,
        # since the tile-to-letter mapping is decided when the sheet is drawn and
        # binds every string that uses it, but the only place it can be *judged*
        # is against a string. Picking it on the font sheet itself would mean
        # setting it where the effect is invisible. The caption says whose it is.
        self._alphabet_label = QLabel(" Alphabet ")
        row.addWidget(self._alphabet_label)
        self._alphabet = SearchableComboBox(150)
        self._alphabet.setToolTip(
            "What this font's tiles spell, for reading the run as text\n"
            "Belongs to the entry supplying the tiles, so every string\n"
            "drawn through that font shares it\n"
            "View > Text is where the answer shows"
        )
        self._alphabet.activated.connect(self._on_alphabet_change)
        row.addWidget(self._alphabet)

        # Where that alphabet starts. The picker says which characters the font
        # spells and in what order - which the sheet itself shows, so it is
        # usually right first try - and this says which code the run begins at,
        # which nothing shows: it lives in the game's code, not in the art
        # (``docs/graphics-formats-reference/text-formats.md`` §3.2). Two
        # independent unknowns, so two controls, and this is the one the user
        # dials while watching the text window rather than picks.
        #
        # Named for **Base tile** at the other end of this bar, because it is the
        # same gesture one reading over: that one shifts what a cell *draws*,
        # this one shifts what it *says*, and the symptom of getting either wrong
        # is identical - right word shapes, consistently wrong letters. Which of
        # the two is off is answered by where the wrongness is, so both tooltips
        # name the other.
        #
        # Signed, like Base tile and for the mirror of its reason: a table
        # written from a sheet that starts partway into the run numbers every
        # glyph too high.
        self._alphabet_base_label = QLabel(" Base code ")
        row.addWidget(self._alphabet_base_label)
        self._alphabet_base = self._tilemap_hex_spin(
            -0xFFFF,
            0xFFFF,
            "Added to every code in the alphabet, shifting what the\n"
            "text says without touching what the cells draw\n"
            "Dial it when the text reads as near-words; when the\n"
            "picture does instead, it is Base tile that is off",
        )
        self._alphabet_base.valueChanged.connect(self._on_alphabet_base_change)
        row.addWidget(self._alphabet_base)

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
            "A blank cell names a real tile whose pixels are all 0 -\n"
            "opaque, a backdrop covers half a screen in one flat colour\n"
            "Off leaves index 0 an ordinary colour, to see and to edit"
        )
        self._transparent_zero_box.toggled.connect(self._on_transparent_zero_change)
        row.addWidget(self._transparent_zero_box)

        row.addSpacing(12)
        # Reads the selected cell and writes it: the one gesture a tilemap has
        # that no pixel control stands in for. It lives beside Base tile because
        # both are "which tile", one for the whole map and one for a cell - and
        # next to a canvas that can now label every cell with the number this
        # sets (View > Show Tile IDs).
        self._cell_index_label = QLabel("Cell ")
        row.addWidget(self._cell_index_label)
        self._cell_index = self._tilemap_hex_spin(
            0,
            0xFFFF,
            "The tile the selected cells name - set it to point\n"
            "them somewhere else\n"
            "On a chained map it is which source cell to stamp",
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
        # Cells that are coordinates into another tilemap have no tile numbering
        # for a base to shift: the map draws through that source and takes its
        # base with it (``_load_tilemap_entry``). True once a tilemap is bound,
        # and before that for a format that says its cells are coordinates -
        # which is the whole of what `indirect` decides. Hidden rather than
        # disabled, on the rule this bar exists for: a control that means nothing
        # here is not a feature switched off, and the offset controls were
        # replaced rather than greyed for exactly that reason.
        chained = self._draws_through_tilemap(entry) or (
            not source.is_bound and self._tilemap_is_indirect(entry)
        )
        self._tile_base_label.setVisible(not chained)
        self._tile_base.setVisible(not chained)
        self._sync_binding_jump(source)
        self._sync_size_pair()
        self._sync_all_frames()
        self._sync_alphabet()
        self._sync_transparent_zero()
        self._sync_cell_index()
        self._tile_binding_note.setText(self._binding_note(entry, source))

    def _bake_binding_jump_icon(self) -> None:
        """Paint the jump button's ring-and-dot in the theme's button-text color.

        A pixmap, so it is baked and not styled: re-run when the theme or the
        device scale changes (``_rebake_icons``), which is also why it is a
        method rather than two lines at the build site.
        """
        self._tile_binding_jump.setIcon(
            source_icon(
                self.palette().color(QPalette.ColorRole.ButtonText),
                ratio=self.devicePixelRatioF(),
            )
        )

    def _sync_binding_jump(self, source: TileSource) -> None:
        """Arm the jump button iff the binding names an entry, and say which.

        Gated on there being an entry to show rather than on the binding
        *resolving*: an unresolved source (a tilemap that draws through a tilemap
        itself) is exactly the case where the user needs to go and look at it.
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

    def _sync_alphabet(self) -> None:
        """Fill and show the alphabet controls, on a fontmap and nowhere else.

        Gated on the **format**'s declaration like the two sprite controls above,
        and disabled — rather than hidden — while nothing is bound: a fontmap
        with no font is the ordinary first moment of one, and the box greyed
        beside a full Tiles combo is what says which of the two to set first.
        Base code follows the picker one step further and is disabled until an
        alphabet is chosen too, since shifting nothing is nothing.

        Both values are read off the **bound entry**, not off this one, because
        that is where they live. So two fontmaps sharing a font show the same
        answer without either having stored it.
        """
        entry = self._workspace.current
        font = entry is not None and self._tilemap_is_font(entry)
        for widget in (
            self._alphabet_label,
            self._alphabet,
            self._alphabet_base_label,
            self._alphabet_base,
        ):
            widget.setVisible(font)
        if not font:
            return
        bound = self._binding_target(entry.tile_source) if entry.tile_source else None
        self._alphabet.setEnabled(bound is not None)
        self._alphabet_base.setEnabled(
            bound is not None and bool(bound.alphabet_preset_id)
        )
        with signals_blocked(self._alphabet):
            # "None" is a real choice here, not an absent selection — a font with
            # no alphabet is the normal state — so it leads the list as an
            # uncategorised row, above whatever groups the presets bring.
            fill_grouped(
                self._alphabet,
                [
                    ("", "None", None),
                    *preset_rows(self._registry.presets(Stage.ALPHABET)),
                ],
                bound.alphabet_preset_id if bound is not None else None,
            )
        with signals_blocked(self._alphabet_base):
            self._alphabet_base.setValue(
                bound.alphabet_base if bound is not None else 0
            )

    def _on_alphabet_change(self, _index: int) -> None:
        """Point the bound font at a different alphabet — one undoable step.

        Unlike everything else on this bar it does **not** re-read the entry. An
        alphabet is a reading of cells that are already decoded, so nothing about
        the bytes, the picture or the geometry moves; what changes is what the
        text window says, and recomputing that is a pass over one preset
        (:meth:`_apply_alphabet`).

        The origin goes **back to zero** with the alphabet, because it was dialled
        against the one being replaced: a shift that made one table read is
        meaningless on the next, and carrying it over would greet the new pick
        with a wrongness the user has to undo before they can judge it.
        """
        self._push_alphabet(lambda bound: (self._alphabet.currentData(), 0))

    def _on_alphabet_base_change(self, value: int) -> None:
        """Slide the bound font's alphabet along the code space — one step.

        Its own command per settled value rather than per tick: ``keyboardTracking``
        is off on this spin (:meth:`_tilemap_hex_spin`), so holding the arrow key
        reports once at the end and the undo stack gets the gesture instead of
        the path it took.
        """
        self._push_alphabet(lambda bound: (bound.alphabet_preset_id, value))

    def _push_alphabet(self, wanted) -> None:  # noqa: ANN001 - Callable[[Entry], tuple]
        """Push an alphabet change on the bound font, if there is one and it moves.

        Both controls settle the same pair, so they share the gate — a fontmap
        with nothing bound has no entry to write to, and a value that already
        matches is a sync echo rather than a gesture.
        """
        entry = self._workspace.current
        if entry is None or self._applying_undo:
            return
        bound = self._binding_target(entry.tile_source) if entry.tile_source else None
        if bound is None:
            return
        before = (bound.alphabet_preset_id, bound.alphabet_base)
        after = wanted(bound)
        if after == before:
            return
        self._push_command(AlphabetCommand(self, bound, before, after))

    def _apply_alphabet(self, font: Entry, state: tuple[str | None, int]) -> None:
        """Land an alphabet and its origin on ``font`` — both command directions.

        Every **open fontmap drawn through** ``font`` is re-read for it, not only
        the one on screen: the alphabet is the font's, so a second string bound to
        the same sheet is just as wrong until it picks the change up, and it is
        the one on screen that would otherwise be the only one right. The same
        rule the binding follows when an entry leaves the list
        (``docs/design/tilemap-entry.md`` §1).
        """
        preset_id, base = state
        font.alphabet_preset_id = preset_id
        font.alphabet_base = base
        for entry in self._workspace.entries:
            doc = entry.doc
            if doc is None or not doc.is_font:
                continue
            if self._binding_target(entry.tile_source) is not font:
                continue
            doc.alphabet = pipeline.load_alphabet(
                preset_id,
                self._registry,
                doc.pixel_ctx,
                controls=self._tilemap_declares(entry, "controls") or (),
                code_digits=max(1, doc.cell_bytes) * 2,
                base=base,
                flag_break=self._tilemap_flag_break(entry),
            )
        # Puts both controls back where an undo or a preset-driven origin reset
        # left them, and re-reads the text window through the new lookup: the
        # refresh drives ``_sync_tilemap_bar`` and ``_sync_text`` in turn, so
        # neither is called here (:meth:`~...rendering.RenderingMixin._refresh_view`).
        self._refresh_view()

    def _on_all_frames_change(self, on: bool) -> None:
        """Redraw with the empty frame slots shown, or without them.

        A **view** toggle, so no re-read and no undo step: the frames are all
        decoded either way (``Document.sprite_frames`` holds every slot), and
        this only says how many of them the sheet lays out. That also makes it
        cheap enough to be a checkbox rather than a reload the way the size pair
        beside it is.

        The refresh is what lands it: the capture at the top of that cycle puts
        the window's answer into ``doc.view``, which is where the sheet geometry,
        the image and an export all read it (``Document.shown_frames``).
        """
        if self._show_all_frames == on:
            return
        self._show_all_frames = on
        if self._doc is not None:
            self._refresh_view()

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

        A **view** toggle like All Frames: nothing is re-read and nothing is
        undoable, because no index moves — only the colour table the render
        resolves them through changes, one entry of it per palette row
        (:func:`~celpix.ui.render_bridge._clear_zeros`). The refresh is what lands
        it, via the view capture at the top of that cycle.
        """
        if self._transparent_zero == on:
            return
        self._transparent_zero = on
        if self._doc is not None:
            self._refresh_view()

    def _sync_cell_index(self) -> None:
        """Show the selected cell's reference, ranged to what the format allows.

        Hidden where there is nothing to set — a sprite object, or a format with
        no index field — on the same rule as Base tile above: a control that means
        nothing here is not a feature switched off. Disabled rather than hidden
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
        limit = self._cell_index_limit()
        usable = self._cell_reference_settable()
        self._cell_index_label.setVisible(usable)
        self._cell_index.setVisible(usable)
        if not usable:
            return
        selected = bool(self._selected_cells())
        self._cell_index.setEnabled(selected)
        with signals_blocked(self._cell_index):
            self._cell_index.setMaximum(limit)
            self._cell_index.setValue(self._selected_cell_index())

    def _on_cell_index_change(self, value: int) -> None:
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
            if not self._can_supply_tiles(entry, bound):
                # Gated on the same rule the binding itself uses, so the line
                # cannot claim a resolution that did not happen. Names the broken
                # link rather than repeating "no source": the binding here is
                # fine, it is the source's own that has to move.
                return (
                    f"{bound.name} draws through a tilemap itself - not resolved."
                    if self._draws_through_tilemap(bound)
                    else f"{bound.name} cannot supply tiles - not resolved."
                )
            # A chained map takes its source's tiles *and* its attributes, so
            # there is no pixel format of its own to report - the source's is the
            # one that matters, and it is on that entry's own bar. What is worth
            # saying instead is which edit lands where: a cell here restamps, and
            # the stamp itself is edited on the entry named.
            return f"Stamped from {bound.name} - edit it there to change the stamps."
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
        # Any tilemap may draw through another one, so the list is both kinds,
        # filtered by the single rule that says which entries qualify
        # (``_can_supply_tiles``). What the format contributes is only the
        # *order*: a map whose cells are coordinates cannot read a tile bank
        # sensibly, so its tilemaps come first and the banks stay reachable
        # instead of being hidden on the strength of a preset flag.
        candidates = [
            (index, candidate)
            for index, candidate in enumerate(self._workspace.entries)
            if self._can_supply_tiles(entry, candidate)
        ]
        if self._tilemap_is_indirect(entry):
            # Stable, so entry order survives inside each group.
            candidates.sort(
                key=lambda pair: pair[1].content_kind is not ContentKind.TILEMAP
            )
        for _index, candidate in candidates:
            combo.addItem(candidate.name, candidate)
        combo.addItem("From file...", _FROM_FILE)
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

        Every entry is tagged with the **layout** it declares (:data:`_LAYOUT_TAG`)
        because that is the part of the choice the names do not carry evenly:
        "Text run" says what it is, "Sprite object subsprite (OBJ/OBX)" leaves you
        to know that a subsprite makes it a sprite map, and picking across layouts
        is not a change of byte order — it is a different kind of document, with a
        different bar under it and a different window over it.
        """
        fill_grouped(
            self._tilemap_preset,
            preset_rows(self._registry.presets(Stage.INTERPRET_TILEMAP), _codec_label),
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
        if data is _NONE or data is None:
            source = TileSource(base_index=self._tile_base.value())
            text = "unbind tiles"
        else:
            source = TileSource(
                mode=TileMode.ENTRY,
                entry=data,
                base_index=self._tile_base.value(),
            )
            text = f"bind tiles to {self._tile_binding.currentText()}"
        self._rebind_tiles(entry, source, text)

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
        wants a panel, and asking it for "tiles" would name the thing its
        coordinates cannot read. Only the wording follows the format, though: what
        the dialog *accepts* is whatever a binding accepts (``_can_supply_tiles``).
        """
        title = (
            "Panel for this stamp layout"
            if self._tilemap_is_indirect(entry)
            else "Tiles for this tilemap"
        )
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
            # that draws through a tilemap itself and so has no tiles to lend. It
            # stays open, since the user asked for it, but the binding is left
            # alone rather than pointed somewhere useless.
            self._activate_entry(entry)
            self._refresh_tilemap_bar()
            return
        source = TileSource(
            mode=TileMode.ENTRY,
            entry=bound,
            base_index=self._tile_base.value(),
        )
        entry.tile_source = source  # before the switch back, so the reload reads it
        self._activate_entry(entry)
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
            entry, replace(source, base_index=value), f"set base tile to ${value:X}"
        )

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
        # The format's own name, not the combo's text: that carries the layout tag
        # ("[S] Sprite object subsprite"), which is a column marker and reads as
        # noise in an Undo menu entry.
        self._push_tilemap_binding(
            entry,
            before,
            replace(before, preset_id=preset_id),
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
        """
        self._write_tilemap_binding(entry, state, previously)
        if self._reload_tilemap(entry):
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
            entry.doc, entry.pending_palette = previous, pending
            entry.pending_view = pending_view
            return False
        return True
