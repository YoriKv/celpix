"""What a tilemap's index counts, and the Index unit choice that says so.

A cell's index — and the binding's base, which counts in the same unit — names
an **element** of what the map is bound to (a tile of a bank, a cell of another
map) or a whole **unit** of them (a metatile, a stamp), and which one is a fact
about the *pair*: the same map counts tiles or metatiles over art, and cells or
stamps over another map (``docs/design/terminology.md``). This module owns that
answer — the nouns every label, tooltip and undo text names the numbers with
(:meth:`~IndexAddressingMixin._index_noun`), whether a rebind can carry the base
across (:meth:`~IndexAddressingMixin._rebound_source`), and the Index unit
combo's items, gating, tooltip and edit, which re-counts the base in the same
step (:meth:`~IndexAddressingMixin._on_index_addressing_change`).

Split from :mod:`~celpix.ui.main_window.tilemap_bar` because it is read as much
from outside the bar as from it: the tile source dock names its button, readout
and held pick in these words too.

Reads, through ``self``, widgets the bar's constructor creates
(:meth:`~...tilemap_bar.TilemapBarMixin._build_tilemap_bar`):
``_index_addressing``, ``_index_addressing_label`` and ``_tile_base``; and the
bar's binding command (:meth:`~...tilemap_bar.TilemapBarMixin._rebind_tiles`)
for its one edit.
"""

from __future__ import annotations

from dataclasses import replace

from celpix.core.capabilities import ContentKind
from celpix.core.tilemap import Geometry, IndexAddressing, rebased
from celpix.project import documents
from celpix.project.workspace import Entry, TileMode, TileSource
from celpix.ui.widgets import signals_blocked, wrap_lines


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
    whole = "" if noun in ("tile", "cell") else f", in whole {noun}s"
    return (
        f"Offset added to every cell index{whole}:\n"
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


class IndexAddressingMixin:
    """What an index counts: the nouns, the Index unit combo, the base re-count.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object. See the module docstring for what it owns and which of
    the bar's widgets it reads.
    """

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

    def _cell_noun(self) -> str:
        """What one entry of the map on screen is called: ``"stamp"`` on a map
        whose cells name cells of another map, ``"cell"`` everywhere else.

        The *entry's* noun, where :meth:`_index_noun` is what the index in one
        counts — a stamped map's entries are stamps whatever they address.
        """
        doc = self._doc
        return "stamp" if doc is not None and doc.is_indirect else "cell"

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
        longer holds it (:meth:`~...tilemap_bar.TilemapBarMixin._bind_tiles_from_file`).
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
        # Off the binding, never off the Base spin: the spin shows whichever
        # entry is *current*, and on the from-file route that is the file just
        # opened - a tilemap there would hand this map its own base. The spin
        # commits on every keystroke, so on the combo route the two agree.
        base = was.base_index if was is not None else 0
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
            return f"Each {unit} is one {element}: both units count the same", None
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
            f"Unit each cell index counts in the {where}:",
            "• From format - the cell format's own unit, in brackets",
            f"• {element.capitalize()}s - index N is the top-left {element} "
            f"of a {unit}",
            f"• {unit.capitalize()}s - index N is {unit} number N",
            "The last two override the format for this map only",
            "Base is re-counted in the new unit, or reset to 0",
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
                f"{unit.capitalize()}s are numbered {per_row} per row from the\n"
                "source's Cols, which no file states: changing it there\n"
                f"renumbers every {unit} here"
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

    def _on_index_addressing_change(self, index: int) -> None:
        """Count this map's indices as elements, as units, or as its format says.

        The base spin's route
        (:meth:`~...tilemap_bar.TilemapBarMixin._rebind_tiles`): the choice
        rides in the binding, so it is one undo step, a re-read, and the bar and
        the tile source panel follow from the refresh that ends it. The **base moves
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
