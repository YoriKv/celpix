"""Editing a tilemap: flipping cells, and moving them around within the app.

The tilemap half of the shared controls. A flip and a copy exist on both kinds
of document and mean different things on each — flipping a *tile* rewrites
pixels, flipping a *cell* toggles the attribute bit hardware put there for
exactly this — so the control is one and the behaviour resolves here
(``docs/design/tilemap-entry.md`` §4).

Two rules the pixel side does not need:

- **Flipping a cell selection is two operations.** Reversing the cells' order
  mirrors the layout while leaving each tile facing its original way; toggling
  each cell's bit mirrors every tile in place. A mirrored picture needs both,
  which is why :meth:`~TilemapEditMixin._transform_cell_selection` does the
  permutation *and* the toggle, exactly as
  :meth:`~celpix.core.tilemap.CellGrid.flipped_h` does.
- **The clipboard stays inside celPix.** Cells are indices into a tile source
  that the receiving program knows nothing about — pasted into another editor
  they would be meaningless numbers, and pasted back from one they could name
  anything. So a cell copy goes to an in-app buffer and the system clipboard is
  left alone, holding whatever the user last put there deliberately.

  A **sprite object** is the exception, and for the reason the rule is stated
  that way: it has no cells on the canvas to lift, so what a copy there takes is
  the *pixels of the drawn sheet* — which are a picture, and travel as one
  (:meth:`~TilemapEditMixin._copy_sprite_pixels`).

An edit replaces the whole cell list through one undo command. A map is a few
thousand frozen cells, so a snapshot is cheap next to the bookkeeping a delta
would need — the same trade
:class:`~celpix.ui.undo_commands.TileRearrangementCommand` already makes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from celpix.core import ceil_div
from celpix.core.arrangement import BlockLayout, compose_window, split_grid
from celpix.core.capabilities import Capability, ContentKind
from celpix.core.context import KEY_TILEMAP_SUBSPRITE_TILES
from celpix.core.document import Document
from celpix.core.errors import PipelineError
from celpix.core.notices import Notice, notices
from celpix.core.sprite import (
    DEFAULT_SUBSPRITE_TILES,
    SizeChoice,
    Subsprite,
    SubspriteLimits,
)
from celpix.core.sprite_edit import reflect_group
from celpix.core.tilemap import Cell, CellGrid, CellOp
from celpix.pipeline import pipeline
from celpix.project.workspace import Entry, EntryKind
from celpix.ui import clipboard, render_bridge
from celpix.ui.undo_commands import TilemapCellsCommand
from celpix.ui.widgets import counted

# What each writable cell attribute is called in a sentence, and which of them
# a Cell stores as a bool. The names are the Cell's own — the vocabulary
# `cell_fields` answers in — so a field a format grows arrives here already
# spelt right or not at all.
_FIELD_LABELS = {
    "index": "cell reference",
    "palette_row": "palette row",
    "priority": "priority",
    "flip_h": "horizontal flip",
    "flip_v": "vertical flip",
    "visible": "drawn flag",
    "ends_line": "line end",
    "flags": "flags",
}
_BOOL_FIELDS = frozenset({"flip_h", "flip_v", "visible", "ends_line"})

# The attributes a cell brings from whatever map it was lifted off, fitted to
# the destination's format on the way in (:func:`fit_cell`). Not ``index``,
# which is refused by name rather than fitted (``_unnameable_reference``); not
# ``ends_line``, which a name table's record fit sets without the format
# declaring it; and not ``flags``, where a codec carries bits it declares no
# field for (a stamp layout's stored byte).
_CARRIED_FIELDS = ("palette_row", "priority", "flip_h", "flip_v", "visible")


def fit_cell(cell: Cell, fields: dict[str, int], under: Cell) -> Cell:
    """``cell`` holding only what a format storing ``fields`` can store.

    A declared field is clamped to its limit. An undeclared one takes
    ``under``'s value — the cell already at that position, which is what the file
    reads back there after a save, whether the format has no such field or keeps
    it in a side array an edit cannot write. Left as copied, a flip lifted off an
    SNES map would draw on a Game Boy one until the save dropped it.

    ``flags`` is masked to its limit where declared and left alone where not
    (:data:`_CARRIED_FIELDS`).
    """
    changes: dict[str, Any] = {}
    for name in _CARRIED_FIELDS:
        value = getattr(cell, name)
        limit = fields.get(name)
        if limit is None:
            want = getattr(under, name)
        elif name in _BOOL_FIELDS:
            want = value
        else:
            want = max(0, min(value, limit))
        if want != value:
            changes[name] = want
    limit = fields.get("flags")
    if limit is not None and cell.flags & ~limit:
        changes["flags"] = cell.flags & limit
    return replace(cell, **changes) if changes else cell


# Sentinels for :meth:`TilemapEditMixin._probe_cell_codec`: "``failed`` was not
# given", and "the codec has no such method" where None is a real answer.
_AS_DEFAULT = object()
_NO_ANSWER = object()


if TYPE_CHECKING:
    # For the checker only: a mixin is a slice of the window, so its
    # ``self`` is the window, widgets, state and all (see the package
    # docstring). At runtime it is a plain mixin.
    from celpix.ui.main_window.window import MainWindow as _Host
else:
    _Host = object


class TilemapEditMixin(_Host):
    """Cell flips and the in-app cell clipboard.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object.
    """

    def _init_tilemap_edit(self) -> None:
        """Seed the cell clipboard; called from the window's ``__init__``.

        Empty until the first cell copy. It lives on the window rather than an
        entry, so a copy off one map pastes onto another, and it never reaches
        the system clipboard (the module docstring says why).
        """
        self._cell_clipboard: CellGrid | None = None

    # -- addressing ----------------------------------------------------------
    def _cells_per_row(self) -> int:
        """How many cells the map is drawn across — the layout's own width."""
        return max(1, self._tilemap_columns())

    def _selected_cells(self) -> list[int]:
        """The cell indices the selection covers, in order and without repeats.

        The canvas selects *tiles*, and a cell may be several of them (a PNL
        panel's cell is a 2x2 metatile), so a selection can name the same cell
        four times. Cells are emitted in order with their tiles consecutive
        (:func:`~celpix.pipeline.pipeline.tilemap_tiles`), which is what makes
        the slot-to-cell step a division rather than a layout question.

        Indices into :attr:`~celpix.core.document.Document.cells` — the file's own
        order — resolved from the drawn positions through
        :meth:`~celpix.core.document.Document.cell_at`, so a selection over an
        assembled screen file names the cells that are actually under it. They come
        back in **screen** order rather than sorted, because that is the order a
        copy of them travels in and the first of them is the one a paste anchors
        on; on an unassembled map the two orders are the same list.

        The repeats the docstring promises to drop are two different repeats, and
        both have to go. A cell drawn as several tiles is one already, which
        :meth:`_selected_positions` handles. A **stamped** map adds the other
        direction: several drawn positions share one entry, so a selection over a
        stamp would otherwise name it once per position — and an operation that
        *toggles* rather than sets would cancel itself out on the second visit.

        On a **sprite map** the picked pieces *are* the selection: the answer is
        their records (:mod:`~celpix.ui.main_window.sprite_select`), in pick
        order, so the property row, the pin gesture, the Cell spin and the
        transforms act on them through the paths they already have
        (``docs/design/sprite-map.md`` §10.1). The sheet tiles under the
        press are a picture's squares, not cells, and name no record.
        """
        doc = self._doc
        if doc is None or not doc.is_tilemap:
            return []
        count = len(doc.cells or [])
        if doc.is_sprite:
            if self._picked_entry is not self._workspace.current:
                return []
            return [r for r in self._picked_records if 0 <= r < count]
        seen: dict[int, None] = {}
        for at in self._selected_positions():
            index = doc.cell_at(at)
            if 0 <= index < count:
                seen.setdefault(index, None)
        return list(seen)

    def _selected_positions(self) -> list[int]:
        """The **drawn** positions the selection covers, in screen order.

        The half of the answer above that is about the picture rather than the
        file: where a paste starts, and which cell of the map a rectangle's corner
        is. Kept apart because an assembly makes the two different numbers, and
        every site that mixed them up would look right on every unpaged file.

        Empty on a **sprite object**, whose slots are squares of the drawn sheet
        rather than cells (:meth:`~...selection.SelectionMixin._view_layout`): the
        division below would turn one into a subsprite record it has no relation to.
        """
        doc = self._doc
        if doc is None or not doc.is_tilemap or doc.is_sprite:
            return []
        per_cell = doc.tiles_per_cell
        seen: dict[int, None] = {}
        for slot in self._selection_tiles():
            seen.setdefault(slot // per_cell, None)
        return sorted(seen)

    def _cell_rect(self) -> tuple[int, int, int, int] | None:
        """The selected rectangle in **cell** coordinates, or None.

        The selected rectangle arrives in canvas **slots**, which are tiles
        (:meth:`~...transform.TransformMixin._block_geometry`); a metatile map
        divides down to its own cell grid. A rectangle narrower than one cell
        holds no cell to flip, so it comes back None rather than as an empty one.
        """
        doc = self._doc
        geom = self._block_geometry()
        if doc is None or geom is None:
            return None
        cols, rows, x0, y0 = geom
        across, down = max(1, doc.cell_tiles[0]), max(1, doc.cell_tiles[1])
        return (cols // across, rows // down, x0 // across, y0 // down)

    # -- asking the format ---------------------------------------------------
    def _tilemap_engine(self):  # noqa: ANN201 — an (engine, Preset) pair
        """The current entry's cell codec and its preset, or None for neither.

        Every question the editor asks a **format** about its cells goes through
        here — which transforms it can express, how wide its index field is, what
        it is called — and through the same preset id the entry was *read* under
        (:meth:`~...session.SessionMixin._tilemap_preset_id`). That is the point
        of it being one lookup: a probe answering for a different format than the
        document holds would disable the flips and the Cell spin over a map that
        is drawing perfectly well. A tilemap carved out by hand carries no preset
        id of its own — no container declared one — and is read under the default,
        so reading the entry's field raw would answer "no format" for it.

        None off a tilemap entirely, and for a preset id the registry no longer
        has: a format celPix does not have cannot have been asked what its cells
        can do, which is the safe direction for every caller here.
        """
        entry = self._workspace.current
        if entry is None or entry.content_kind is not ContentKind.TILEMAP:
            return None
        try:
            return self._registry.engine_for(self._tilemap_preset_id(entry))
        except KeyError:
            return None

    def _probe_cell_codec(
        self,
        name: str,
        *args: Any,
        read: Callable[[Any], Any] | None = None,
        default: Any = None,
        failed: Any = _AS_DEFAULT,
    ) -> Any:  # whatever the codec answers, which the caller knows the shape of
        """Ask this entry's cell codec ``name``, or answer ``default`` for it.

        **The one probe protocol** every "what can this format's cells do"
        question follows: no tilemap, or a codec written before the method
        existed, answers ``default`` — each caller's safe direction, since a
        format that was never asked cannot have said where its field sits. A
        method that *raises* is reported through :meth:`_codec_fault` and read
        as ``failed`` (``default`` unless the caller must tell "cannot answer"
        from "does not answer"): a broken plugin must not break the bar.

        ``read`` shapes the answer inside the same guard, so a codec that hands
        back something malformed is caught as the fault it is rather than
        escaping from the caller's own iteration.
        """
        found = self._tilemap_engine()
        if found is None:
            return default
        engine, preset = found
        ask = getattr(engine, name, None)
        if ask is None:
            return default
        try:
            answer = ask(*args, preset.params)
            return answer if read is None else read(answer)
        except Exception as exc:  # noqa: BLE001 — a probe must not break the bar
            self._codec_fault(preset.id, name, exc)
            return default if failed is _AS_DEFAULT else failed

    # -- transforms ----------------------------------------------------------
    def _cell_transform(self, op) -> Callable[[Cell], Cell] | None:  # noqa: ANN001
        """``op`` as this entry's *format* performs it, or None if it cannot.

        The dispatch the whole tilemap transform story hangs on: which of these a
        map supports is a property of its format — a console BG entry has both
        mirror bits, a Game Boy map entry has neither — and only the codec knows
        which bits say it. So the tool names the operation and the codec answers
        (``docs/design/tilemap-entry.md`` §4).

        Probed with a blank cell, so asking costs nothing and changes nothing —
        which is what lets the toolbar ask on every sync and *disable* the
        buttons a format has no bit for, instead of offering them and refusing
        the click (:meth:`~...transform.TransformMixin._transform_allowed`). The
        probe still runs here on the way in, as the backstop for the window
        between a format changing and the bar being re-armed.

        A codec with no answer at all — one written before the method existed —
        refuses, which is the safe direction: it cannot have been asked which of
        its fields a flip means.
        """
        found = self._tilemap_engine()
        if found is None:
            return None
        engine, preset = found
        apply = getattr(engine, "transform_cell", None)
        if apply is None or apply(Cell(), op.cell_op, preset.params) is None:
            return None
        return lambda cell: apply(cell, op.cell_op, preset.params)

    def _tilemap_format_name(self) -> str:
        """This entry's cell format, named for a sentence about what it cannot do."""
        found = self._tilemap_engine()
        return found[1].name if found is not None else "This tilemap format"

    def _refuse_transform(self, op) -> None:  # noqa: ANN001
        """Say which format cannot do this, and why it is the format's answer.

        The button is disabled where this is known in advance, so reaching here
        means the format moved under an armed bar — named rather than silent,
        because a click that does nothing with no reason given is the worst of
        the three outcomes.
        """
        self.statusBar().showMessage(
            f"{self._tilemap_format_name()} has no {op.cell_op.label}"
            " - nothing changed."
        )

    # -- cell references -----------------------------------------------------
    def _cell_index_limit(self) -> int | None:
        """The highest reference this entry's cells can hold, or None for none.

        The codec's answer (:meth:`~celpix.plugins.base.TilemapCodecPlugin.
        index_limit`), on the same protocol the flips follow: a format that does
        not answer has its references left alone rather than clamped to a guess,
        because it cannot have been asked where its index field sits.
        """
        top = self._probe_cell_codec("index_limit")
        return top if top and top > 0 else None

    def _cell_index_runs(self) -> tuple[range, ...] | None:
        """The references this entry's cells can hold, as ascending runs, or None.

        :meth:`_cell_index_limit` with what no stored cell can name taken out
        (:meth:`~celpix.plugins.base.TilemapCodecPlugin.index_runs`): the one
        answer every place a reference is chosen reads — the tile source panel's
        offer, the stamp tool's pick, the Cell spin's range, and the check every
        edit passes on its way in (:meth:`_unnameable_reference`). A format that
        does not answer names every index up to its limit, as a field holding a
        plain number does; one that cannot answer is read the same way, with the
        fault noticed. None where there is no limit, and so no reference at all.
        """
        limit = self._cell_index_limit()
        if limit is None:
            return None
        whole = (range(limit + 1),)
        runs = self._probe_cell_codec(
            "index_runs",
            # Cut to the limit so the runs can bound the spin on their own.
            read=lambda answer: tuple(
                cut
                for run in answer
                if (cut := range(max(0, run.start), min(limit + 1, run.stop)))
            ),
            default=whole,
        )
        # An empty answer names nothing and bounds nothing; read as no answer.
        return runs or whole

    def _unnameable_reference(self, cells: list[Cell]) -> str | None:
        """Why ``cells`` cannot be committed, or None where every reference fits.

        **The one check every cell edit passes** — the stamp, the Cell spin, a
        paste, a clear, typed text — made in :meth:`_apply_cells`, so no gesture
        has to remember it. A reference outside :meth:`_cell_index_runs` would be
        written as a stored value that names something else, and the save would
        report success over it; the gestures that choose a reference offer only
        these already, and this catches the rest (a paste from another map, a
        clear writing 0 on a map whose records start higher).

        Only a reference the edit **changes** is asked about, so a cell already
        holding an unnameable one does not block an edit elsewhere on the map.
        """
        doc = self._doc
        runs = self._cell_index_runs()
        if doc is None or doc.cells is None or runs is None:
            return None
        before = doc.cells
        for at, cell in enumerate(cells):
            index = cell.index
            if at < len(before) and before[at].index == index:
                continue
            if not any(index in run for run in runs):
                what = "stamp" if doc.is_indirect else "tile"
                named = ", ".join(f"${run.start:X}-${run.stop - 1:X}" for run in runs)
                return (
                    f"{self._tilemap_format_name()} cannot store {what} ${index:X}"
                    f" - its cells name {named}. Nothing changed."
                )
        return None

    def _cell_reference_settable(self) -> bool:
        """Whether this entry has a cell reference to point somewhere at all.

        **Three levels, and they are three different questions**
        (``docs/design/tilemap-entry.md`` §4). ``STAMP`` is the **kind**'s: only a
        tilemap has a cell that names a tile. ``cells_editable`` is this
        **file**'s: a sprite map whose codec declared no record it can write has
        nothing to set, where an editable one's picked pieces are the selection
        the spin points (:meth:`_selected_cells`). The limit is the **format**'s:
        a cell with no index field has no number to hold.

        One predicate because there are two ways to ask it — the binding bar's
        Cell spin types the number and the Edit Tiles tool points at it
        (:meth:`~...stamp_tool.StampToolMixin._stamp_available`) — and a control
        that offered one gesture while the other was refused would be describing
        two different documents.
        """
        doc = self._doc
        return (
            doc is not None
            and self._can(Capability.STAMP)
            and doc.cells_editable
            and self._cell_index_limit() is not None
        )

    # -- cell palette rows ---------------------------------------------------
    def _cell_palette_row_limit(self) -> int | None:
        """The highest palette row this entry's own cells can hold, or None.

        :meth:`_cell_index_limit` for the colour field, on the same protocol
        (:meth:`~celpix.plugins.base.TilemapCodecPlugin.palette_row_limit`) and
        refusing on the same terms: a codec that does not answer has its rows
        left alone rather than clamped to a guess.

        It answers **this file's** format, which is what makes it the right test
        for a chained map: a stamp layout's word is a coordinate with no palette
        field, so the row a position draws through is the *PNL panel's*, and that
        panel is where it can be changed (``docs/design/tilemap-entry.md`` §3.1).
        The drawn row and the writable row are two different questions there, and
        only this one is about bytes this entry owns.

        None too on a view-only sprite map, whose records this path may not
        write (:attr:`~celpix.core.document.Document.cells_editable`) — stated
        here so the caller has one predicate rather than two.
        """
        doc = self._doc
        if doc is None or not doc.cells_editable:
            return None
        top = self._probe_cell_codec("palette_row_limit")
        return top if top and top > 0 else None

    def _cells_have_visibility(self) -> bool:
        """Whether this entry's own cells carry a drawn bit — Clear's question.

        The same probe protocol as :meth:`_cell_palette_row_limit` and refusing
        on the same terms: a codec that does not answer
        (:meth:`~celpix.plugins.base.TilemapCodecPlugin.has_visibility`) has no
        bit invented for it, since a hide the encode drops would leave the
        picture lying against the bytes.
        """
        return bool(self._probe_cell_codec("has_visibility"))

    def _cell_fields(self) -> dict[str, int]:
        """Every per-cell field this entry's format stores, with each one's limit.

        The whole-table probe the property row is generated from
        (:meth:`~celpix.plugins.base.TilemapCodecPlugin.cell_fields`), keyed by
        :class:`Cell` attribute name. Empty off a tilemap, and empty for a
        format that answers nothing — no field, no control.

        A codec without the method has the answer **assembled from its other
        probes**, each already refusing in its own safe direction, so a plugin
        written before the method existed keeps exactly the controls it earned.
        What the assembly never grants is ``priority`` or ``flags``: no older
        probe speaks for either, and a control over a field the encode drops
        would write bits that vanish on save.
        """
        if self._tilemap_engine() is None:
            return {}
        stated = self._probe_cell_codec(
            "cell_fields", read=dict, default=_NO_ANSWER, failed={}
        )
        if stated is not _NO_ANSWER:
            return {
                name: limit
                for name, limit in stated.items()
                if isinstance(limit, int) and limit > 0
            }

        probe = self._probe_cell_codec
        fields: dict[str, int] = {}
        top = probe("index_limit")
        if isinstance(top, int) and top > 0:
            fields["index"] = top
        row = probe("palette_row_limit")
        if isinstance(row, int) and row > 0:
            fields["palette_row"] = row
        for op, name in ((CellOp.FLIP_H, "flip_h"), (CellOp.FLIP_V, "flip_v")):
            if probe("transform_cell", Cell(), op) is not None:
                fields[name] = 1
        if probe("has_visibility"):
            fields["visible"] = 1
        if probe("has_line_flag"):
            fields["ends_line"] = 1
        return fields

    # -- a sprite map's record fields ----------------------------------------
    def _subsprite_limits(self) -> SubspriteLimits | None:
        """What this sprite map's records can hold, or None to write nothing.

        None off a sprite map, and on one whose codec declared nothing — the
        view-only answer (:attr:`~celpix.core.document.Document.cells_editable`).
        Read off the document, where the load put it, rather than asked again:
        the answer is partly the file's (a slot stride the container stated).
        """
        doc = self._doc
        if doc is None or not doc.is_sprite:
            return None
        return doc.subsprite_limits

    def _place_subsprite(self, cell: Cell, **fields: object) -> Cell:
        """``cell`` with the record ``fields`` written by this entry's codec.

        The codec's ``place_subsprite`` bound to the preset and the document's
        context — the one way the host writes an offset, a size or the drawn
        bit, since only the codec knows where they live in ``flags``
        (:meth:`~celpix.plugins.base.TilemapCodecPlugin.place_subsprite`).
        Raises ``ValueError`` for a value the field cannot hold, worded for the
        status line, as the codec does. A codec that faults otherwise is
        reported once and read as the same refusal: a broken plugin must not
        leave half an edit behind.
        """
        doc = self._doc
        found = self._tilemap_engine()
        place = getattr(found[0], "place_subsprite", None) if found else None
        if doc is None or place is None:
            raise ValueError(f"{self._tilemap_format_name()} cannot place subsprites")
        try:
            return place(cell, found[1].params, doc.tilemap_ctx, **fields)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 — a plugin fault, reported
            self._codec_fault(found[1].id, "place_subsprite", exc)
            raise ValueError(
                f"{self._tilemap_format_name()} could not place the subsprite"
            ) from exc

    def _assign_frames(self, cells: list[Cell], frames: Sequence[int]) -> list[Cell]:
        """``cells`` restamped so cell *i* is in ``frames[i]``, by the codec.

        Unchanged where the codec has no frame marks to write (``slots`` and
        ``run`` formats omit the method). Over a **copy** of the context: a
        counted format publishes its frame sizes while restamping, and an op
        refused after this call must not leave those on the document — the
        encode that lands an accepted one republishes them from the cells.
        """
        doc = self._doc
        found = self._tilemap_engine()
        assign = getattr(found[0], "assign_frames", None) if found else None
        if doc is None or assign is None:
            return list(cells)
        return assign(
            list(cells), list(frames), found[1].params, doc.tilemap_ctx.copy()
        )

    def _subsprite_pair(self) -> tuple[int, int]:
        """The ``(small, large)`` squares, in tiles, a size bit picks between.

        The pair the frames were built with — the load publishes it on the
        context — then the document's setting, then the default.
        """
        doc = self._doc
        stated = doc.tilemap_ctx.get(KEY_TILEMAP_SUBSPRITE_TILES) if doc else None
        for pair in (stated, doc.sprite_size_pair if doc else None):
            try:
                small, large = (int(side) for side in pair)
            except (TypeError, ValueError):
                continue
            if small >= 1 and large >= 1:
                return small, large
        return DEFAULT_SUBSPRITE_TILES

    def _size_choice_of(self, sub: Subsprite) -> SizeChoice | None:
        """The size choice ``sub``'s shape spells under the limits, or None.

        ``"small"`` / ``"large"`` where the record holds a size *bit*: the shape
        is matched against the pair in force, small first, so under a pair
        whose two sides agree the answer is ``"small"`` — the bit itself never
        reaches a :class:`Subsprite`, and both choices draw the same then. An
        ``(across, down)`` choice where the record states its rectangle. None
        where the limits fix the size, or the shape is none of the choices.
        """
        limits = self._subsprite_limits()
        if limits is None or not limits.size:
            return None
        shape = sub.size()
        small, large = self._subsprite_pair()
        for choice, side in (("small", small), ("large", large)):
            if choice in limits.size and shape == (side, side):
                return choice
        return shape if shape in limits.size else None

    def _size_caption(self, choice: SizeChoice) -> str:
        """``choice`` as the Size control and the status line say it.

        A bit is captioned with the shape it spells at the pair in force —
        ``Small (1x1)`` — since the pair is the entry's setting and not the
        file's; a rectangle is its own caption.
        """
        if isinstance(choice, str):
            small, large = self._subsprite_pair()
            side = small if choice == "small" else large
            return f"{choice.capitalize()} ({side}x{side})"
        return f"{choice[0]}x{choice[1]}"

    def _place_subsprites(self, fields: dict[str, object], text: str) -> bool:
        """Write record ``fields`` into every picked record — one undo step.

        :meth:`_set_cell_property`'s shape for the fields a :class:`Cell` has
        no attribute for (``x``, ``y``, ``size``, ``drawn``), written through
        the codec (:meth:`_place_subsprite`). The offsets and the size go to
        the **drawn** picked pieces only: an undrawn slot's content is residue
        nothing shows, and a move written into it would surface the next time
        the slot is drawn. ``drawn`` goes to every picked record, which is what
        lets the box that cleared a slot put it back.

        A value the field cannot hold is refused whole, with the codec's reason
        on the status line, and nothing is pushed: one piece moved and the next
        refused would change the selection's shape.
        """
        doc = self._doc
        if doc is None or doc.cells is None or self._refuse_view_only():
            return False
        picked = self._selected_cells()
        if "drawn" in fields:
            targets = picked
        else:
            targets = [record for record, _sub in self._picked_subsprites()]
        if not targets:
            return False
        cells = list(doc.cells)
        try:
            for at in targets:
                cells[at] = self._place_subsprite(cells[at], **fields)
        except ValueError as exc:
            reason = str(exc)
            self.statusBar().showMessage(
                f"{reason[:1].upper()}{reason[1:]} - nothing changed."
            )
            return False
        if not self._apply_cells(cells, text, pick=(picked, picked)):
            return False
        many = counted(len(targets), "subsprite")
        if "drawn" in fields:
            said = "on" if fields["drawn"] else "off"
            shown = f"Turned drawn {said} for {many}"
        elif "size" in fields:
            shown = f"Set size to {self._size_caption(fields['size'])} for {many}"
        elif "x" in fields and "y" in fields:
            shown = f"Moved {many} to ({fields['x']:+d}, {fields['y']:+d})"
        else:
            axis = "x" if "x" in fields else "y"
            shown = f"Moved {many} to {axis} = {fields[axis]:+d}"
        self.statusBar().showMessage(f"{shown}.")
        return True

    def _pick_carried(self) -> tuple[list[int], list[int]] | None:
        """The pick an edit of the selected cells carries through its undo
        step: a sprite map's records, unchanged by the edit; None on a grid,
        whose selection is not the step's to restore."""
        doc = self._doc
        if doc is None or not doc.is_sprite:
            return None
        picked = self._selected_cells()
        return picked, list(picked)

    def _selected_noun(self) -> str:
        """What one selected cell is called in a status sentence: a sprite
        map's are its picked pieces."""
        doc = self._doc
        return "subsprite" if doc is not None and doc.is_sprite else "cell"

    def _set_cell_property(self, field: str, value: bool | int) -> None:
        """Write one attribute into every selected cell — one undoable step.

        The property row's write funnel, and deliberately one method for every
        field it shows: the shape is :meth:`_set_cell_index`'s — clamp to what
        the format can hold, replace over a copied list, one command through
        :meth:`_apply_cells` — and eight near-copies of it would be eight
        places for that shape to drift.

        Refused with a message when the format lacks the field. The control is
        hidden where that is known in advance, so reaching the refusal means
        the format moved under an armed row — same backstop as
        :meth:`_refuse_transform`.
        """
        doc = self._doc
        indices = self._selected_cells()
        if doc is None or doc.cells is None or not indices:
            return
        label = _FIELD_LABELS.get(field, field)
        limit = self._cell_fields().get(field)
        if limit is None:
            self.statusBar().showMessage(
                f"{self._tilemap_format_name()} has no {label} to set"
                " - nothing changed."
            )
            return
        if field in _BOOL_FIELDS:
            value = bool(value)
        else:
            value = max(0, min(int(value), limit))
        cells = list(doc.cells)
        for at in indices:
            cells[at] = replace(cells[at], **{field: value})
        if self._apply_cells(cells, f"set cell {label}", pick=self._pick_carried()):
            if field in _BOOL_FIELDS:
                said = "on" if value else "off"
                shown = f"Turned {label} {said}"
            elif field == "flags":
                shown = f"Set {label} to ${value:X}"
            else:
                shown = f"Set {label} to {value}"
            self.statusBar().showMessage(
                f"{shown} for {counted(len(indices), self._selected_noun())}."
            )

    def _assign_cell_palette_row(self) -> None:
        """The tilemap reading of the pin gesture: write the row into the cells.

        Same question as pinning a region and the same row picked the same way
        (:meth:`~...palette_regions.PaletteRegionsMixin._named_row_picked`) —
        what differs is where it is kept. A pixel document has nothing in its
        bytes that could say a row, so a pin is display state the project
        carries; a cell has the field already, so this is an ordinary cell edit
        and the file keeps the answer.

        Clamped to what the field can hold rather than left for :meth:`encode` to
        mask down later, the rule :meth:`_set_cell_index` follows: a row wider
        than three bits would come back as a row nobody asked for.

        **Grown to whole groups where the format stores one row for several
        cells** (:meth:`~celpix.core.document.Document.palette_row_group`). An
        NES nametable colours a 2x2 square of cells from one two-bit field, so
        setting one cell's row sets its neighbours' whether or not they were
        selected — the only choice is whether they change *now*, on screen and in
        one undo step, or silently on the next save. They change now, and the
        status line says how many cells it took, because the alternative is an
        edit whose picture is a lie until the file is reloaded.
        """
        doc = self._doc
        picked = self._selected_cells()
        if doc is None or doc.cells is None or not picked:
            return
        limit = self._cell_palette_row_limit()
        if limit is None:
            self.statusBar().showMessage(
                f"{self._tilemap_format_name()} has no palette row to set"
                " - nothing changed."
            )
            return
        row = max(0, min(self._named_row_picked(), limit))
        # Every distinct cell the selection reaches. **De-duplicated because it
        # is counted**: on a format storing one row for several cells, two
        # selected cells can share a field, and the group reached from both would
        # otherwise report more cells changed than changed. A set and not the
        # ordered dict :meth:`_selected_cells` builds, because nothing here reads
        # an order — every write puts the same row in, and the status line
        # reports a count.
        spread = {member for at in picked for member in doc.palette_row_group(at)}
        cells = list(doc.cells)
        for at in spread:
            cells[at] = replace(cells[at], palette_row=row)
        if self._apply_cells(cells, "set cell palette row", pick=self._pick_carried()):
            shown = self._drawn_palette_row(row)
            noun = self._selected_noun()
            note = ""
            # `picked` has no repeats of its own and every cell of it is in its
            # own group, so a difference here is the grouping and nothing else.
            if len(spread) > len(picked):
                across, down = doc.palette_row_granularity
                note = (
                    f" This format colors {across}x{down} cells at a time,"
                    f" so {counted(len(spread) - len(picked), 'more cell')} changed."
                )
            self.statusBar().showMessage(
                f"Set {counted(len(spread), noun)} to palette row {shown}.{note}"
            )

    def _set_cell_index(self, value: int) -> None:
        """Point every selected cell at reference ``value`` — one undoable step.

        What the number *means* follows the document, and both readings are the
        same edit to the same field. On an ordinary map it is a tile in the bound
        source. On a **chained** map it is a position in the map being drawn
        through, so setting it is the **restamp**: this position now takes that
        source cell's tile and attributes, and the stamp itself stays editable on
        the map it came from (``docs/design/tilemap-entry.md`` §3.1).

        Clamped to what the format can hold rather than left for :meth:`encode` to
        mask down later, so the cell that lands is the one that was asked for.
        A value inside that range no stored cell can name is refused on the way
        in (:meth:`_unnameable_reference`); the spin does not offer one.
        """
        doc = self._doc
        indices = self._selected_cells()
        if doc is None or doc.cells is None or not indices:
            return
        limit = self._cell_index_limit()
        if limit is None:
            self.statusBar().showMessage(
                f"{self._tilemap_format_name()} has no cell reference to set"
                " - nothing changed."
            )
            return
        value = max(0, min(value, limit))
        cells = list(doc.cells)
        for at in indices:
            cells[at] = replace(cells[at], index=value)
        if self._apply_cells(cells, "set cell reference", pick=self._pick_carried()):
            what = "stamp" if doc.is_indirect else "tile"
            self.statusBar().showMessage(
                f"Pointed {counted(len(indices), self._selected_noun())} at {what}"
                f" ${value:X}."
            )

    def _selected_cell_index(self) -> int:
        """The reference the first selected cell holds — what the spin shows."""
        doc = self._doc
        indices = self._selected_cells()
        if doc is None or doc.cells is None or not indices:
            return 0
        return doc.cells[indices[0]].index

    def _transform_cells(self, op) -> None:  # noqa: ANN001 — a TransformOp
        """Apply ``op`` to every selected cell, in place.

        No permutation: the cells stay where they are and each one's own tile is
        transformed, which is what the Tile group means on the pixel side too.
        """
        doc = self._doc
        indices = self._selected_cells()
        if doc is None or doc.cells is None or not indices:
            return
        apply = self._cell_transform(op)
        if apply is None:
            self._refuse_transform(op)
            return
        if doc.is_sprite:
            # The drawn pieces only: an undrawn slot's bits are residue, and a
            # flip toggled into one would surface when the slot is next drawn.
            drawn = {record for record, _sub in self._picked_subsprites()}
            indices = [at for at in indices if at in drawn]
        cells = list(doc.cells)
        for index in indices:
            cells[index] = apply(cells[index])
        if indices and self._apply_cells(
            cells, f"{op.verb} cells", pick=self._pick_carried()
        ):
            self.statusBar().showMessage(
                f"{op.past} {counted(len(indices), self._selected_noun())}."
            )

    def _transform_cell_selection(self, op) -> None:  # noqa: ANN001 — a TransformOp
        """Transform the selected rectangle: reorder the cells **and** transform each.

        ``op.cell_src`` gives the permutation, shared with the pixel side's
        rectangle transform so the two cannot disagree about direction; the
        per-cell half goes through the format, and a format that cannot do it
        stops the whole selection rather than leaving it reordered but unturned.

        On a **sprite map** there is no rectangle of cells to permute, and the
        picked pieces are mirrored as one picture instead
        (:meth:`_mirror_subsprites`).
        """
        doc = self._doc
        if doc is not None and doc.is_sprite:
            self._mirror_subsprites(op)
            return
        rect = self._cell_rect()
        if doc is None or doc.cells is None or rect is None:
            return
        cols, rows, x0, y0 = rect
        if cols <= 0 or rows <= 0:
            return
        apply = self._cell_transform(op)
        if apply is None:
            self._refuse_transform(op)
            return
        width = self._cells_per_row()
        cells = list(doc.cells)
        original = list(doc.cells)
        moved = 0
        # In placed units, like every sibling that walks a rectangle (the copy's
        # lattice, the paste's step): on a stamped chain several positions share
        # one entry, and a per-position walk would compute the permutation in
        # position space and land it in entry space — each entry written once
        # per position its stamp covers. The unit is one cell everywhere else,
        # and the loops read as they always did.
        unit_w, unit_h = doc.stamp_cells
        ucols, urows = max(1, cols // unit_w), max(1, rows // unit_h)
        for dy in range(urows):
            for dx in range(ucols):
                sx, sy = op.cell_src(dx, dy, ucols, urows)
                # Both ends through the document, because the selection is a
                # rectangle of the *picture* and the cells it holds need not be a
                # run of the file: on an assembled screen a selection spanning two
                # pages moves cells between them, which is what the user drew over.
                dest = doc.cell_at((y0 + dy * unit_h) * width + (x0 + dx * unit_w))
                src = doc.cell_at((y0 + sy * unit_h) * width + (x0 + sx * unit_w))
                if 0 <= dest < len(cells) and 0 <= src < len(original):
                    cells[dest] = apply(original[src])
                    moved += 1
        if moved and self._apply_cells(cells, f"{op.verb} cell selection"):
            self.statusBar().showMessage(f"{op.past} the {cols}x{rows} cell selection.")

    def _mirror_subsprites(self, op) -> None:  # noqa: ANN001 — a TransformOp
        """The Block group on a sprite map: the picked pieces mirrored as one picture.

        Two halves, as on a grid: each piece's own flip bit toggled through the
        format (:meth:`_cell_transform`), and each piece moved to where the
        mirror puts it — reflected about the pick's own box, so the box maps
        onto itself (:func:`~celpix.core.sprite_edit.reflect_group`). The
        reflection is per piece, ``x' = left + right - x - w`` with ``w`` its
        own width, because reflecting left edges alone is exact only when every
        piece is one size and puts a mixed frame's small piece a tile off
        (``docs/design/sprite-map.md`` §10.2).

        Flips only: a quarter turn of a group of pieces at pixel offsets is no
        record's bit to set, and the format refuses it as it does on a grid.
        A piece the mirror would carry outside the offset range stops the whole
        group, with the range said, rather than one piece pinned at the edge.
        """
        doc = self._doc
        pieces = self._picked_subsprites()
        if doc is None or doc.cells is None or not pieces or self._refuse_view_only():
            return
        if op.cell_op not in (CellOp.FLIP_H, CellOp.FLIP_V):
            self._refuse_transform(op)
            return
        apply = self._cell_transform(op)
        if apply is None:
            self._refuse_transform(op)
            return
        placed = reflect_group(
            [sub for _record, sub in pieces],
            doc.tile_width,
            doc.tile_height,
            horizontal=op.cell_op is CellOp.FLIP_H,
        )
        cells = list(doc.cells)
        try:
            for (record, _sub), (x, y) in zip(pieces, placed, strict=True):
                cells[record] = self._place_subsprite(apply(cells[record]), x=x, y=y)
        except ValueError as exc:
            reason = str(exc)
            self.statusBar().showMessage(
                f"{reason[:1].upper()}{reason[1:]} - nothing changed."
            )
            return
        picked = self._selected_cells()
        if self._apply_cells(cells, f"{op.verb} subsprites", pick=(picked, picked)):
            self.statusBar().showMessage(
                f"{op.past} {counted(len(pieces), 'subsprite')} as one picture."
            )

    # -- placed units --------------------------------------------------------
    def _lift_cell_rect(
        self, doc: Document, x0: int, y0: int, x1: int, y1: int
    ) -> tuple[CellGrid, int, int]:
        """The drawn rectangle ``x0,y0``-``x1,y1`` as one record per placed unit.

        With the corner it was snapped to. **The one lift** both rectangle
        pickups share — the cell copy (:meth:`_copy_cells`) and the right drag's
        area pick (:meth:`~...stamp_tool.StampToolMixin._on_stamp_area_picked`)
        — so what a copy holds and what a sweep holds cannot drift apart.

        On a stamped chain several drawn positions share one entry, so a lift
        per position would hold every stamp once per position it covers. The
        rectangle grows out to the stamp lattice instead — the corner floors
        onto it, and counting units from there to the far edge is the round
        *up*, so a rectangle that enters a stamp holds all of it — and each
        unit is read at its corner. The lattice sits on the resolved grid
        (:meth:`~celpix.core.document.Document.cell_at` snaps to the same one),
        so a sweep and a click cannot disagree about which entry a position
        is. Everywhere else the unit is one cell and this is the plain lift.
        """
        width = self._cells_per_row()
        unit_w, unit_h = doc.stamp_cells
        x0 -= x0 % unit_w
        y0 -= y0 % unit_h
        lifted = CellGrid((x1 - x0) // unit_w + 1, (y1 - y0) // unit_h + 1)
        for dy in range(lifted.height):
            for dx in range(lifted.width):
                at = doc.cell_at((y0 + dy * unit_h) * width + (x0 + dx * unit_w))
                if 0 <= at < len(doc.cells):
                    lifted.set(dx, dy, doc.cells[at])
        return lifted, x0, y0

    def _lay_positions(
        self, doc: Document, anchor: int, cols: int, rows: int
    ) -> Iterator[tuple[int, int, int]]:
        """Where a ``cols``x``rows`` grid of records lands from drawn ``anchor``.

        ``(dx, dy, file cell index)`` for every unit that lands, row by row:
        **the one lay** the cell paste (:meth:`_paste_cells`) and the stamp
        brush (:meth:`~...stamp_tool.StampToolMixin._stamp_into`) share. Each
        record steps a whole placed unit, the counterpart of
        :meth:`_lift_cell_rect`, so on a stamped chain each lands on exactly
        one stamp — an unaligned anchor snaps to the stamp holding it through
        :meth:`~celpix.core.document.Document.cell_at`, where a per-position
        lay would write each entry into every stamp its cells graze. Clipped
        at the row's end rather than wrapped, and at the file's end.
        """
        width = self._cells_per_row()
        unit_w, unit_h = doc.stamp_cells
        x0, y0 = anchor % width, anchor // width
        for dy in range(rows):
            for dx in range(cols):
                x = x0 + dx * unit_w
                if x >= width:
                    continue
                at = doc.cell_at((y0 + dy * unit_h) * width + x)
                if 0 <= at < len(doc.cells):
                    yield dx, dy, at

    # -- the in-app clipboard ------------------------------------------------
    def _copy_cells(self) -> bool:
        """Lift the selected rectangle of cells into the in-app buffer.

        A rectangle so a paste can put it back with its shape; a linear
        selection copies as one row, which is what it looks like on screen.

        The rectangle is read in **placed units** (:meth:`_lift_cell_rect`,
        the right drag's area pick reads its sweep the same way): on a stamped
        chain it holds one record per stamp — what was swept over, once each —
        and :meth:`_paste_cells` steps by the same unit, so a paste writes back
        exactly the area that was copied. On every other map the unit is one
        cell and this is the per-cell lift it reads as.

        A **sprite object** copies its pixels instead. Its cells are not what is
        on screen — a canvas position there is a *subsprite* through an overlap
        order,
        so lifting cells would take records the user never pointed at — but the
        picture under the selection is perfectly well defined, and copying what
        you can see is the gesture that was missing rather than one to refuse
        (:meth:`_copy_sprite_pixels`).
        """
        doc = self._doc
        if doc is not None and doc.is_sprite:
            # Inside Edit Subsprites the four verbs mean *records*, through the
            # in-app cell buffer, for the reason pixel mode re-aims them: a
            # record is an index into a bank the receiving program has never
            # heard of (``docs/design/sprite-map.md`` §10.1). Outside the tool a
            # copy is still the picture.
            if self._stamping:
                return self._copy_subsprites()
            return self._copy_sprite_pixels()
        if doc is None or doc.cells is None or self._refuse_view_only():
            return False
        rect = self._cell_rect()
        if rect is not None and rect[0] > 0 and rect[1] > 0:
            cols, rows, x0, y0 = rect
            lifted, _x, _y = self._lift_cell_rect(
                doc, x0, y0, x0 + cols - 1, y0 + rows - 1
            )
        else:
            indices = self._selected_cells()
            if not indices:
                return False
            lifted = CellGrid.from_cells(
                len(indices), 1, [doc.cells[i] for i in indices]
            )
        self._cell_clipboard = lifted
        self._sync_edit_actions()
        what = "stamp" if doc.is_indirect else "cell"
        self.statusBar().showMessage(f"Copied {counted(len(lifted), what)}.")
        return True

    def _copy_sprite_pixels(self) -> bool:
        """Lift the selected tiles of a sprite sheet as pixels; False if none.

        The one tilemap copy that goes out to the **system** clipboard, and the
        module docstring's rule is why: what this lifts is not cells naming tiles
        in a file the receiving program has never heard of, it is the picture on
        screen. So it travels the way a pixel document's copy does — the tiles
        themselves for a lossless paste back into celPix, and a rendered image so
        every other program sees an ordinary sprite.

        Cut out of the **composed sheet** rather than fetched from the tile bank,
        which is what makes it the picture and not the ingredients: a subsprite sits
        at a signed pixel offset and they overlap, so an 8x8 of the sheet is
        generally pieces of two source tiles and neither of them whole
        (:func:`~celpix.pipeline.pipeline.sprite_image`). ``split_grid`` undoes
        exactly the placement that composed it, given the same layout the
        selection reads its slots off.

        The indices carry their subsprites' palette rows already folded in, as they do
        everywhere a tilemap is drawn, so the colours that go with them are the
        whole window those rows reach across — the same table an export sizes —
        and the image renders through the pinned path that expects them.
        """
        doc = self._doc
        slots = self._selection_tiles()
        if doc is None or not slots:
            return False
        drawn = pipeline.tilemap_image(doc, self._registry, self._tilemap_columns())
        tile_w, tile_h = doc.tile_width, doc.tile_height
        sheet = split_grid(drawn.grid, tile_w, tile_h, self._view_layout())
        tiles = [sheet[slot] for slot in slots if 0 <= slot < len(sheet)]
        if not tiles:
            return False
        space = self._index_space()
        colors = tuple(
            doc.palette.color(at) for at in range(drawn.palette_rows * space)
        )
        columns = self._copy_columns(len(tiles))
        rows = ceil_div(len(tiles), columns)
        picture = compose_window(tiles, columns, 0, rows, BlockLayout(columns))
        clipboard.put(
            clipboard.TilePayload.from_tiles(tiles, colors, columns=columns),
            render_bridge.render(picture, doc.palette),
        )
        self._sync_edit_actions()
        self.statusBar().showMessage(f"Copied {counted(len(tiles), 'tile')}.")
        return True

    def _cut_cells(self) -> None:
        # Up front, so a view-only map refuses here rather than copying its
        # pixels and then discovering there is nothing to blank behind them.
        if self._refuse_view_only() or self._refuse_outside_subsprite_tool("cut"):
            return
        if self._doc is not None and self._doc.is_sprite:
            self._cut_subsprites()
            return
        if self._copy_cells():
            self._clear_cells("cut cells")

    def _refuse_outside_subsprite_tool(self, verb: str) -> bool:
        """True, with the reason on the status bar, for a record verb asked of
        a sprite map while Edit Subsprites is not armed.

        Outside the tool a sprite map's tile selection is sheet tiles, which is
        what pixel Copy lifts and nothing a cut, clear or paste could write back:
        the records are reached through the tool, where the picked pieces are
        the selection (``docs/design/sprite-map.md`` §10.1).
        """
        doc = self._doc
        if doc is None or not doc.is_sprite or self._stamping:
            return False
        self.statusBar().showMessage(
            f"Arm Edit Subsprites (T) to {verb} pieces of a sprite map."
        )
        return True

    def _clear_cells(self, text: str = "clear cells") -> None:
        """Blank the selected cells — index 0, no attributes, and where the
        format can say so, **not drawn**.

        A tilemap has a fixed extent, so clearing is writing the empty cell
        rather than removing anything: there is no shorter map to leave behind.

        Refused up front, as a copy is, rather than on the way into
        :meth:`_apply_cells`: a sprite object's selection names no cells at all
        (:meth:`_selected_positions`), so the empty list below would leave with
        nothing said about why.
        """
        doc = self._doc
        if doc is None or doc.cells is None or self._refuse_view_only():
            return
        if doc.is_sprite:
            if not self._refuse_outside_subsprite_tool("clear"):
                self._clear_subsprites()
            return
        indices = self._selected_cells()
        if not indices:
            return
        cells = list(doc.cells)
        # Hidden too, where the format has a drawn bit to store it: on a stamp
        # layout "cell $0 drawn" and "nothing here" are different words that
        # label the same $0 and look nothing alike, and delete means the second.
        # That makes Clear the stamp tool's inverse - stamping sets the bit
        # (`_stamp_cell`), clearing takes it back. Where the format has no such
        # bit the flag stays as it was rather than being invented: the encode
        # would drop the hide and the picture would lie against the bytes.
        hide = self._cells_have_visibility()
        for index in indices:
            # `flags` stays either way - it carries what celPix does not model,
            # which is not content a clear was asked to remove (bit 15 of a
            # stamp layout's entry rides there).
            cells[index] = Cell(
                visible=False if hide else cells[index].visible,
                flags=cells[index].flags,
            )
        if self._apply_cells(cells, text):
            self.statusBar().showMessage(f"Cleared {counted(len(indices), 'cell')}.")

    def _paste_cells(self) -> None:
        """Lay the buffer over the map from the selection's first cell.

        Overwrite and clipped, never inserting: the map's extent is the file's,
        so a paste replaces exactly as many cells as there is room for.

        Stepped in **placed units** (:meth:`_lay_positions`, the stamp brush's
        lay too): the buffer holds one record per unit (:meth:`_copy_cells`),
        so on a stamped chain each record lands on exactly one stamp rather
        than pasting a wider area than was copied.
        """
        doc = self._doc
        copied = self._cell_clipboard
        if doc is None or doc.cells is None or copied is None or not len(copied):
            self.statusBar().showMessage("No cells copied yet.")
            return
        if doc.is_sprite:
            if not self._refuse_outside_subsprite_tool("paste"):
                self._paste_subsprites()
            return
        # The anchor is where the selection is *drawn*, not which cell of the file
        # it holds: a paste lays a rectangle over the picture, and on an assembled
        # map those are different numbers (:meth:`_selected_positions`).
        positions = self._selected_positions()
        start = positions[0] if positions else 0
        cells = list(doc.cells)
        written = 0
        for dx, dy, at in self._lay_positions(doc, start, copied.width, copied.height):
            cells[at] = copied.get(dx, dy)
            written += 1
        if not written:
            self.statusBar().showMessage("Nothing pasted - no room here.")
            return
        if self._apply_cells(cells, "paste cells"):
            clipped = len(copied) - written
            note = f" ({clipped} clipped)" if clipped else ""
            what = "stamp" if doc.is_indirect else "cell"
            self.statusBar().showMessage(f"Pasted {counted(written, what)}{note}.")

    def _has_cell_clipboard(self) -> bool:
        """Whether a cell paste would have anything to put down."""
        copied = self._cell_clipboard
        return copied is not None and len(copied) > 0

    # -- committing ----------------------------------------------------------
    def _apply_cells(
        self,
        cells: list[Cell],
        text: str,
        *,
        run: int | None = None,
        caret: tuple[int, int] | None = None,
        pick: tuple[Sequence[int], Sequence[int]] | None = None,
    ) -> bool:
        """Push ``cells`` as one undoable edit; False when nothing changed.

        The no-change guard is what keeps a flip of an empty selection, or a
        paste of identical cells, from putting a step on the undo stack that
        would appear to do nothing when it came back.

        ``run`` groups consecutive edits into one step and is the text window's
        alone — typing is the only gesture here that fires per keystroke
        (:class:`~celpix.ui.undo_commands.TilemapCellsCommand`).

        ``caret`` is the text window's too — where the caret stood before and
        after the edit — and it **lifts the no-change guard**, because a
        keystroke changes where the user is standing in the string whether or
        not it changes a cell. A letter typed over itself is a gesture Ctrl+Z
        should answer; there is nothing equivalent for a flip or a paste, which
        is why the guard stays for everything that names no caret.

        ``pick`` is a sprite map's: the picked records before and after the
        edit, which each half of the step puts back (:meth:`_restore_record_pick`)
        — an undo of a move leaves the outline on the piece it moved. It does not
        lift the guard: a pick is not an edit.

        Refused outright only on a sprite map whose codec declared nothing its
        inverse can write (:attr:`~celpix.core.document.Document.cells_editable`).
        A chained map does land here: its cells are coordinates, and writing one
        restamps that position. So does an editable sprite map, whose new cells
        are **framed before the push** (:func:`~celpix.pipeline.pipeline.
        sprite_frames_of`) — the sheet limit refuses an offset the picture cannot
        hold where it is typed, with the reason on the status bar, rather than
        landing an edit the next load of the entry would fail on.

        **Row groups are settled before the guard**, not after, so a paste whose
        only difference from what is there is a row the format cannot store is
        the no-op it will turn out to be
        (:meth:`~celpix.core.document.Document.settle_cells`). Doing it
        here rather than in each gesture is what keeps the coarse formats from
        being a special case in the paste, the stamp, the clear and the fill
        alike — every one of them arrives through this method.

        For the same reason it is where an edit landing a **reference the format
        cannot store** is refused, with the reason on the status bar
        (:meth:`_unnameable_reference`), and where every changed cell's
        attributes are **fitted to this format** (:func:`fit_cell`): the cell
        clipboard, an eyedropped record and a canvas brush all outlive an entry
        switch, so what they lay may come from a format with fields this one
        has not got.
        """
        doc = self._doc
        entry = self._workspace.current
        if self._applying_undo or self._refuse_view_only():
            return False
        if doc is None or doc.cells is None or entry is None:
            return False
        fields = self._cell_fields()
        if fields:
            cells = self._fit_cells(cells, doc.cells, fields)
        said = notices(doc.tilemap_ctx)
        cells = doc.settle_cells(cells)
        self._show_new_notices(entry, said)
        if cells == doc.cells and caret is None:
            return False
        refused = self._unnameable_reference(cells)
        if refused is not None:
            self.statusBar().showMessage(refused)
            return False
        if doc.is_sprite and doc.tilemap_config is not None:
            # Over a copy of the context, encoded first: that is the order
            # `_set_cells` runs in, and a count format's encode is what states
            # the frame sizes these cells are framed by. A trial must also leave
            # no notice of its own behind on the document.
            trial = doc.tilemap_ctx.copy()
            preset = doc.tilemap_config.interpret_preset_id
            try:
                pipeline.encode_cells(cells, preset, self._registry, trial)
                pipeline.sprite_frames_of(cells, preset, self._registry, trial)
            except KeyError:
                pass  # no codec to ask; the save is where that is reported
            except PipelineError as exc:
                self.statusBar().showMessage(str(exc))
                return False
        self._push_command(
            TilemapCellsCommand(
                self,
                entry,
                text,
                list(doc.cells),
                cells,
                run=run,
                caret=caret,
                pick=pick,
            )
        )
        return True

    def _restore_record_pick(self, entry: Entry, records: Sequence[int]) -> None:
        """Put a sprite map's pick where an undo step says it belongs.

        The command's half of the pick travelling with the edit
        (:class:`~celpix.ui.undo_commands.TilemapCellsCommand`), called after
        the cells have landed so the records index the list that is now there.
        Only where the step is visible: the command applies in place, as the
        stamp's does, and the pick of an object nobody is looking at is no pick
        of this window's.
        """
        doc = self._doc
        if entry is self._workspace.current and doc is not None and doc.is_sprite:
            self._set_picked_records(records)

    @staticmethod
    def _fit_cells(
        cells: list[Cell], before: list[Cell], fields: dict[str, int]
    ) -> list[Cell]:
        """``cells`` with each changed one fitted to ``fields`` (:func:`fit_cell`).

        Only the cells that differ from ``before``, by identity first — every
        gesture copies the live list and replaces what it writes — so an edit
        never rewrites a cell it did not touch. The same list comes back where
        nothing needed fitting, which the no-change guard keys off.
        """
        out: list[Cell] | None = None
        blank = Cell()
        for at, cell in enumerate(cells):
            under = before[at] if at < len(before) else blank
            if cell is under:
                continue
            fitted = fit_cell(cell, fields, under)
            if fitted is not cell:
                if out is None:
                    out = list(cells)
                out[at] = fitted
        return cells if out is None else out

    def _refuse_view_only(self) -> bool:
        """True — with the reason on the status bar — when cells cannot be edited.

        Only a **sprite map whose codec declared no limits** reaches this: its
        records have no field the host has been told it may write
        (:attr:`~celpix.core.document.Document.cells_editable`). A mapping table
        is the shipped case — the table's cells are offset words, and the pieces
        they name live in a Frames region another entry owns. A chained map is
        editable — a cell edit restamps it — and what its own format cannot express
        is refused per operation by the codec instead (:meth:`_cell_transform`,
        :meth:`_cell_index_limit`), which is a narrower answer than a whole
        document being read-only.

        The controls this catches are disabled there
        (:meth:`~...selection.SelectionMixin._sync_edit_actions`), so this is the
        guard behind them rather than the way the user finds out. It stays a
        *message* and not an assertion because the reason is not guessable from
        the object on screen.
        """
        doc = self._doc
        if doc is None or not doc.is_tilemap or doc.cells_editable:
            return False
        self.statusBar().showMessage(
            "This sprite map is view-only - its format declares no piece it can "
            "write back (a table's pieces live in a region another entry owns). "
            "Edit the tiles it draws from."
        )
        return True

    def _set_cells(
        self,
        entry: Entry,
        cells: list[Cell],
        revision: int,
        owners: tuple[tuple[Entry, int], ...] = (),
    ) -> None:
        """Land a cell list on ``entry`` — the command's apply, both directions.

        ``revision`` stamps the data pathway, so the entry reads dirty against
        what was last written and an undo back to the saved state reads clean
        again. ``owners`` are the other entries the cells land in — a map slice's
        parent file, into whose buffer the edit is folded — each with the
        revision this side of the step leaves it at. A tilemap's cells are its
        own data, which is the same pathway a pixel entry's bytes use
        (:func:`~celpix.pipeline.pipeline.save`).

        Both directions of the chain are settled here, which is what makes a
        restamp show up: this document re-resolves its own new coordinates, and
        anything drawing *through* it is re-pointed at the cells it now has
        (:meth:`~...bindings.BindingsMixin._rechain_dependents`). A sprite map's
        frames are rebuilt from the new cells the same way
        (:func:`~celpix.pipeline.pipeline.sprite_frames`): they are a function of
        the cells, so the picture follows every edit, undo and redo alike.

        **An entry with no document is read first.** The command applies in
        place, so it can land on a map whose cache a write of another entry on the
        same file dropped (``Workspace.invalidate_path``) — and landing the
        revision without the cells would leave the history and the bytes
        disagreeing: the row reads dirty, a save writes what the file already
        holds, and the edit the step names is nowhere. A map that cannot be read
        takes neither, which is the ``undo-redo.md`` §5 edge rather than a
        silent half-apply.
        """
        if entry.doc is None and not self._load_entry(entry, quiet=True):
            return
        said = notices(entry.doc.tilemap_ctx)
        entry.doc.cells = list(cells)
        entry.doc.resolve()
        self._reencode_cells(entry.doc)
        if entry.doc.is_sprite:
            # After the encode, which is what republishes a count format's frame
            # sizes; a picture built from the old cells would show the edit's
            # bytes under the old drawing. The trial in `_apply_cells` has
            # already framed these cells, so a failure here is an undo onto a
            # state whose context has since moved — said, and the old picture
            # kept rather than none.
            try:
                entry.doc.sprite_frames = pipeline.sprite_frames(
                    entry.doc, self._registry
                )
            except (KeyError, PipelineError) as exc:
                self.statusBar().showMessage(str(exc))
        if entry.kind is EntryKind.SLICE:
            # A map carved from a file is a window of its buffer like any slice,
            # so the file owes it a fold — recorded in *both* directions, since an
            # undo back to the saved cells still has to take the edited ones back
            # out of the parent (``docs/design/slices-and-parents.md`` §2).
            self._propagate_pixel_edit(entry)
        self._show_new_notices(entry, said)
        touched = self._rechain_dependents(entry)
        self._workspace.set_pixel_revision(entry, revision)
        for owner, owner_revision in owners:
            self._workspace.set_pixel_revision(owner, owner_revision)
        if entry is self._workspace.current or touched:
            self._refresh_view()

    def _show_new_notices(self, entry: Entry, said: tuple[Notice, ...]) -> None:
        """Repaint ``entry``'s row if its cells' context has recorded a notice
        since ``said`` was taken.

        A format's settle runs per edit rather than per load
        (:func:`~celpix.pipeline._stage.cell_settler`), so the notice it leaves
        when it crashes and is stood down arrives with no load behind it to
        repaint the row. The dirty marker's repaint happens to catch it on the
        first edit after a save, and on no other. Compared rather than repainted
        unconditionally, because this runs on every committed edit.
        """
        doc = entry.doc
        if doc is not None and notices(doc.tilemap_ctx) != said:
            self._files_panel.refresh_entry(entry)

    def _reencode_cells(self, doc) -> None:  # noqa: ANN001 — a Document
        """Bring ``doc.tilemap_data`` back in step with the cells above it.

        The cells are the source of truth and the buffer is what they were read
        from, so an edit leaves the two disagreeing. Everything that reads the
        *bytes* instead of the cells then shows the file as it was opened rather
        than as it stands — the hex dump under the map, and Export Raw. Doing it
        here, at the one writer of the cells, is what keeps those two from each
        needing their own answer, and costs one encode per committed edit: the
        same order as the whole-list snapshot the undo command already takes.

        **Spliced at the extent, not replaced.** A decode drops a trailing
        partial cell — a file need not hold a whole number of them — so the
        buffer can run past what the cells encode to, and that tail stays as it
        was read. Where the tail starts is the document's
        :attr:`~celpix.core.document.Document.tilemap_extent`, the length the
        cells encoded to last time, and this moves it to the length just
        encoded: an edit that removes a record shortens the encoding, and a tail
        cut at the new length would put the removed record's bytes straight back.
        The save splices by the same rule before it packs
        (``pipeline._tilemap_region``).

        A format that cannot encode what is now in the cells leaves the buffer
        alone rather than emptying it; the save is where that has to be reported,
        and it asks the codec again.
        """
        if not doc.is_tilemap or doc.tilemap_config is None:
            return
        try:
            data = pipeline.encode_cells(
                # The same list a save would encode, so the buffer under the map
                # and the file agree byte for byte
                # (:attr:`~celpix.core.document.Document.settled_cells`).
                doc.settled_cells,
                doc.tilemap_config.interpret_preset_id,
                self._registry,
                doc.tilemap_ctx,
            )
        except (KeyError, PipelineError):
            return
        doc.tilemap_data = doc.spliced_cells(data)
        doc.tilemap_extent = len(data)
