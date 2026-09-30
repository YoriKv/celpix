"""What a tilemap entry's cell format declares, and how its indices are read.

Everything here is answerable before anything is loaded or bound, from the
entry, its preset and — for a chained map — the document it draws through: the
format's own declarations (``layout``, strides, ``flag_break``), the stamp a
chain lays out (:class:`CellStamp`), how a map's indices count units of what
they address (:class:`IndexReading`), and which open entry a binding names
(:func:`binding_target`). The binding bar describes an entry it has not read yet
from these, and :mod:`celpix.project.documents` builds the document from the
same answers.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, NamedTuple

from celpix.core.capabilities import ContentKind
from celpix.core.context import (
    KEY_TILEMAP_CELL_TILES,
    KEY_TILEMAP_RECORD_HEADER,
    KEY_TILEMAP_RECORD_SHAPE,
    KEY_TILEMAP_STAMP_CELLS,
    KEY_TILEMAP_STAMP_COLUMN_MAJOR,
    KEY_TILEMAP_STAMP_STRIDE,
)
from celpix.core.document import Document
from celpix.core.errors import Stage
from celpix.core.tilemap import (
    LAYOUT_SPRITE,
    LAYOUT_TEXT,
    RECORD_KEYS,
    VRAM_ROW_STRIDE,
    Geometry,
    IndexAddressing,
    containing_at,
    corner_at,
    metatile_geometry,
    record_geometry,
    stamp_geometry,
    unit_at,
)
from celpix.plugins.base import DEFAULT_TILEMAP_PRESET, Preset
from celpix.plugins.registry import Registry
from celpix.project.workspace import Entry, TileMode, TileSource, Workspace

if TYPE_CHECKING:
    from celpix.project.projectfile import LoadedProject

# ---------------------------------------------------------------------------
# What a cell format declares about itself
#
# Declarations are readable before anything is loaded or bound, which is the
# whole of what they are for: the binding bar has to describe an entry it has not
# read yet. An object with no tile source is still an object, and a stamp layout
# with none is still a stamp layout.


def tilemap_preset_id(entry: Entry) -> str:
    """The cell format ``entry``'s own file is read under.

    A container names one when the file is opened
    (``detect.tilemap_preset_for``), so the fallback is for a tilemap carved
    out by hand, which had no container to have said.
    """
    return entry.tilemap_preset_id or DEFAULT_TILEMAP_PRESET


def preset_declares(registry: Registry, preset_id: str, name: str) -> object:
    """What the **format** ``preset_id`` declares under ``name``, or None.

    None for a preset id nothing is registered under, which is the same answer
    as a preset that declares nothing: a format celPix does not have cannot have
    claimed anything about its cells.
    """
    try:
        preset = registry.preset(preset_id)
    except KeyError:
        return None
    return preset.params.get(name)


def tilemap_declares(registry: Registry, entry: Entry, name: str) -> object:
    """What ``entry``'s cell format declares under ``name``, or None."""
    return preset_declares(registry, tilemap_preset_id(entry), name)


def is_fontmap(registry: Registry, entry: Entry) -> bool:
    """Whether ``entry``'s format says its cells are character codes
    (``docs/design/fontmap-entry.md``)."""
    return tilemap_declares(registry, entry, "layout") == LAYOUT_TEXT


def is_sprite(registry: Registry, entry: Entry) -> bool:
    """Whether ``entry``'s format says its cells are subsprites grouped into
    frames rather than positions in a grid (``docs/design/sprite-map.md`` §1)."""
    return tilemap_declares(registry, entry, "layout") == LAYOUT_SPRITE


def states_subsprite_size(registry: Registry, entry: Entry) -> bool:
    """Whether ``entry``'s format gives each subsprite its own rectangle.

    Most sprite records hold a size *bit* picking between two squares the file
    never records, so the pair is a setting the user supplies; a Mega Drive
    record holds the console's own size nibble and states a rectangle outright.
    """
    return tilemap_declares(registry, entry, "subsprite_size") == "stated"


def is_indirect(registry: Registry, entry: Entry) -> bool:
    """Whether ``entry``'s format says its cells are coordinates into a map.

    It decides how the bar reads while nothing is bound, never what a map may
    draw through — chaining is generic and gated on depth
    (:func:`~celpix.project.documents.bound_tilemap`).
    """
    return bool(tilemap_declares(registry, entry, "indirect"))


def is_dense(registry: Registry, entry: Entry) -> bool:
    """Whether ``entry``'s format holds one entry per stamp.

    The referring half of a stamped chain, and the only part of one that is not
    the source's to answer (:attr:`~celpix.core.cellchain.CellChain.dense`). A
    stamp layout has a slot per drawn position and only its corners are read; a
    map of 16x16 metatiles over an 8x8 bank has a slot per stamp. Which shape a
    file is, is fixed by its format, so it is declared beside ``indirect``.
    False for a format that declares nothing: a wrong guess would expand a map to
    four times its size.
    """
    return bool(tilemap_declares(registry, entry, "stamp_dense"))


def declared_cell_row_stride(registry: Registry, entry: Entry) -> int:
    """The stride between a metatile cell's tile rows, in tiles of the bank.

    ``cell_row_stride`` in the preset params, for a format whose metatile's rows
    are not a VRAM row apart — a table of consecutive tiles is its own width (2
    for a 2x2 cell). The default stays the console VRAM row.
    """
    try:
        stride = int(tilemap_declares(registry, entry, "cell_row_stride"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return VRAM_ROW_STRIDE
    return stride if stride > 0 else VRAM_ROW_STRIDE


def declared_cell_column_stride(registry: Registry, entry: Entry) -> int:
    """The stride between a metatile cell's tile columns, in tiles of the bank.

    ``cell_column_stride`` in the preset params, for a format that fills a cell
    down each column: 16x16 objects drawn as four consecutive tiles, the left
    column first, are ``cell_row_stride = 1`` and ``cell_column_stride = 2``. The
    default is 1, the next tile to the right, which is every row-major cell.
    """
    try:
        stride = int(tilemap_declares(registry, entry, "cell_column_stride"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1
    return stride if stride > 0 else 1


def flag_break(registry: Registry, entry: Entry) -> bool:
    """Whether ``entry``'s format ends a line on a bit rather than a code.

    Asked of the **codec**, the only thing that knows where a cell's bits go.
    False for a codec that was never asked: a line break that costs a cell is
    always writable, where a bit inferred onto a format that has not got one
    would be written into its bytes.
    """
    try:
        preset = registry.preset(tilemap_preset_id(entry))
        engine = registry.plugin(Stage.INTERPRET_TILEMAP, preset.engine_id)
    except KeyError:
        return False
    ask = getattr(engine, "has_line_flag", None)
    return bool(ask is not None and ask(preset.params))


class CellStamp(NamedTuple):
    """A referring map's own cell size, read as a stamp over a tilemap source.

    ``cell_tiles`` says one cell draws several of what it draws through. Over
    art those are *tiles*, a metatile; over another tilemap they can only be
    that map's *cells*, which is a stamp — so a preset stating ``cell_tiles =
    [2, 2]`` draws 2x2 whichever kind of source it is bound to, rather than
    shrinking to one cell the moment the source is a map
    (``docs/design/tilemap-entry.md`` §3.1).

    Only the size carries over. The metatile's strides, ``cell_row_stride``
    and ``cell_column_stride``, are steps through a tile bank, and a stamp is a
    rectangle cut out of the source as the *source* lays its cells out — its
    width, or the stride and order it publishes for its records
    (:func:`chain_source_columns`, :func:`chain_stamp_column_major`). Half of a
    bank's walk applied to a map is what draws overlapping cells: a cell filled
    down each column is a row stride of 1, which over a map would step each of
    the stamp's rows one cell along the row above.
    """

    cells: tuple[int, int]


def cell_stamp(registry: Registry, entry: Entry, ctx) -> CellStamp | None:  # noqa: ANN001
    """The stamp ``entry``'s own cell size states over a tilemap source, or None.

    None where its format declares ``stamp_cells``, which is the stamp spelled
    outright and wins, and where its cells cover one unit. The size is the
    container's answer over the codec's, as it is for a map drawn from art
    (:func:`~celpix.project.documents.tilemap_document`) — ``ctx`` is the
    referrer's own tilemap context.
    """
    if tilemap_declares(registry, entry, "stamp_cells"):
        return None
    stated = ctx.get(KEY_TILEMAP_CELL_TILES)
    try:
        if not stated:
            preset = registry.preset(tilemap_preset_id(entry))
            engine = registry.plugin(Stage.INTERPRET_TILEMAP, preset.engine_id)
            stated = engine.cell_tiles(preset.params)
        across, down = max(1, int(stated[0])), max(1, int(stated[1]))
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if (across, down) == (1, 1):
        return None
    return CellStamp((across, down))


def chain_stamp_cells(
    registry: Registry,
    entry: Entry,
    through: Document,
    stamp: CellStamp | None = None,
) -> tuple[int, int]:
    """How many of ``through``'s **cells** one of ``entry``'s coordinates names.

    Whichever side states it, the referrer first: a format whose coordinates
    always name a fixed stamp declares ``stamp_cells``, or states its cells
    cover several units (``stamp``, :func:`cell_stamp`); otherwise the source's
    published answer (:data:`~celpix.core.context.KEY_TILEMAP_STAMP_CELLS`).
    ``(1, 1)`` for a pair that states nothing, and for a malformed declaration,
    since a wrong guess would expand the map to a multiple of its size — the
    rule :func:`~celpix.pipeline.table_layout.publish_table_layout` keeps for
    the same declaration on the source's side.
    """
    stated = (
        tilemap_declares(registry, entry, "stamp_cells")
        or (stamp.cells if stamp is not None else None)
        or through.tilemap_ctx.get(KEY_TILEMAP_STAMP_CELLS)
    )
    try:
        across, down = stated or (1, 1)
        return max(1, int(across)), max(1, int(down))
    except (TypeError, ValueError):
        return (1, 1)


def chain_source_columns(through: Document) -> int:
    """The stride between a stamp's rows, in cells of the **source**.

    The source's answer, whoever stated the stamp's size: a stamp is a
    rectangle cut out of the source as it lays its cells out (:class:`CellStamp`).
    First what it publishes for its records
    (:data:`~celpix.core.context.KEY_TILEMAP_STAMP_STRIDE`): a table of packed
    records stamps at the record's width whatever it is displayed at. Else the
    width its **entries** are laid at (:attr:`~celpix.core.document.Document.
    entry_columns`) — its format's stated width, or the view's Cols, where file
    order is drawn order. A stride of 1 in that case would walk a stamp's second
    row along the same source row instead of down one.

    Entries, not drawn positions, because the hop's source list is the
    source's entries (``through.cells``). They are the same width until the
    source is itself densely stamped: then Cols counts the positions its stamps
    draw, a stamp's width times its entries, and striding by it would put a
    stamp's second row that many stamps too far along.
    """
    return _published_stride(through) or through.entry_columns


def chain_width_is_stated(through: Document) -> bool:
    """Whether :func:`chain_source_columns` is the source's own statement — a
    published stride or a stated width — rather than the width its view is
    laid at, which the user moves with Cols."""
    return bool(_published_stride(through) or through.stated_columns)


def _published_stride(through: Document) -> int:
    """The stamp stride ``through`` publishes, or 0 for none
    (:data:`~celpix.core.context.KEY_TILEMAP_STAMP_STRIDE`)."""
    stride = through.tilemap_ctx.get(KEY_TILEMAP_STAMP_STRIDE)
    try:
        return int(stride) if stride and int(stride) >= 1 else 0
    except (TypeError, ValueError):
        return 0


def chain_stamp_column_major(through: Document) -> bool:
    """Whether the source stores a stamp's cells down each column.

    Only ever the source's own statement
    (:data:`~celpix.core.context.KEY_TILEMAP_STAMP_COLUMN_MAJOR`), published
    beside the stride it changes the meaning of: a table viewed at some width is
    laid out row by row, whatever order its records keep inside.
    """
    return bool(through.tilemap_ctx.get(KEY_TILEMAP_STAMP_COLUMN_MAJOR))


class IndexReading(NamedTuple):
    """How ``entry``'s indices number what they draw, and who said so.

    ``stated`` is the addressing asked for and ``stated_by`` whose word it was:
    ``"binding"`` (:attr:`~celpix.project.workspace.TileSource.addressing`),
    ``"preset"`` (``index_addressing``), ``"engine"`` (an engine whose index
    counts records, :func:`engine_counts_records`) or ``"default"``.

    ``geometry`` is what is **in force**: None for corner addressing, a
    :data:`~celpix.core.tilemap.Geometry` for ordinal. An ordinal can still be
    read as a corner — a unit of one element, whose count *is* its corner, and a
    source whose units no geometry can number, where ``refusal`` says why.
    """

    stated: IndexAddressing
    stated_by: str
    geometry: Geometry | None = None
    refusal: str | None = None

    @property
    def in_force(self) -> IndexAddressing:
        """The addressing the model reads the indices in."""
        if self.geometry is None:
            return IndexAddressing.CORNER
        return IndexAddressing.ORDINAL


def stated_addressing(registry: Registry, entry: Entry) -> tuple[IndexAddressing, str]:
    """The addressing asked for of ``entry``'s indices, and whose word it is.

    Most specific first: the **binding**'s override, then the referring
    format's ``index_addressing``, then its engine where that engine's index
    counts records, and otherwise corner (:class:`IndexReading` names the
    four). A word ``index_addressing`` does not have refuses the load earlier
    (``pipeline._check_declarations``), so one reaching here reads as unset.
    """
    source = entry.tile_source
    if source is not None and source.addressing is not None:
        return source.addressing, "binding"
    return format_addressing(registry, entry)


def format_addressing(registry: Registry, entry: Entry) -> tuple[IndexAddressing, str]:
    """:func:`stated_addressing` with the binding's override left out — what
    ``entry``'s **format** alone says, for a control that offers the override
    and has to name what clearing it gives back."""
    word = tilemap_declares(registry, entry, "index_addressing")
    if word in (IndexAddressing.CORNER.value, IndexAddressing.ORDINAL.value):
        return IndexAddressing(word), "preset"
    if engine_counts_records(registry, _tilemap_preset(registry, entry)):
        return IndexAddressing.ORDINAL, "engine"
    return IndexAddressing.CORNER, "default"


def _tilemap_preset(registry: Registry, entry: Entry) -> Preset | None:
    """The preset ``entry``'s cells are read under, or None where none is
    registered under its id."""
    try:
        return registry.preset(tilemap_preset_id(entry))
    except KeyError:
        return None


def engine_counts_records(registry: Registry, preset: Preset | None) -> bool:
    """Whether ``preset``'s engine reads every index as a record number
    (:meth:`~celpix.plugins.base.TilemapCodecPlugin.counts_records`).

    The one question behind both things such an engine is owed: its preset is
    ordinal where it states no ``index_addressing``, and its
    :data:`~celpix.core.tilemap.RECORD_KEYS` are read at all
    (:func:`reads_record_keys`). They are that engine's parameters, so over any
    other they are no statement — the load says so
    (``pipeline._check_declarations``). An engine that raises is read as one
    that never answered, the rule every optional probe follows. False for no
    preset, which has no engine to ask.
    """
    if preset is None:
        return False
    try:
        engine = registry.plugin(Stage.INTERPRET_TILEMAP, preset.engine_id)
    except KeyError:
        return False
    ask = getattr(engine, "counts_records", None)
    try:
        return bool(ask is not None and ask(preset.params))
    except Exception:  # noqa: BLE001 — a plugin's crash reads as silence
        return False


def reads_record_keys(registry: Registry, entry: Entry) -> bool:
    """Whether an ordinal of ``entry``'s counts in the shape its format's record
    keys give (:func:`~celpix.core.tilemap.record_geometry`, each at its default
    where unset) rather than one derived from its source.

    Only over an engine counting records (:func:`engine_counts_records`), and
    there unless the preset states ``index_addressing`` and no record key: that
    preset has handed the shape to what the map is bound to, so defaults
    standing in for keys it never wrote would overrule the table's own stride
    and records. A binding that asks for ordinal is counted in the same shape,
    since the keys describe the table the format was written against.
    """
    preset = _tilemap_preset(registry, entry)
    if preset is None or not engine_counts_records(registry, preset):
        return False
    params = preset.params
    return "index_addressing" not in params or any(key in params for key in RECORD_KEYS)


def _published_header(through: Document) -> int:
    """How many cells of the record ``through`` publishes come before its
    stamp, or 0 for none (:data:`~celpix.core.context.KEY_TILEMAP_RECORD_HEADER`)."""
    try:
        return max(0, int(through.tilemap_ctx.get(KEY_TILEMAP_RECORD_HEADER) or 0))
    except (TypeError, ValueError):
        return 0


def _published_pitch(through: Document) -> int:
    """How many cells the record ``through`` publishes holds, or 0 for none
    (:data:`~celpix.core.context.KEY_TILEMAP_RECORD_SHAPE`)."""
    shape = through.tilemap_ctx.get(KEY_TILEMAP_RECORD_SHAPE)
    try:
        return max(0, int(shape[0]) * int(shape[1])) if shape else 0
    except (TypeError, ValueError, IndexError):
        return 0


def index_reading(
    registry: Registry,
    entry: Entry,
    *,
    through: Document | None = None,
    stamp: tuple[int, int] = (1, 1),
    cell_tiles: tuple[int, int] = (1, 1),
    cell_row_stride: int = 0,
    cell_column_stride: int = 0,
    addressing: IndexAddressing | None = None,
) -> IndexReading:
    """How ``entry``'s indices number the units they draw — the one derivation.

    Asked of a map drawing **through** another (``through``, the source's
    document, and ``stamp``, the chain's stamp in its cells) or over a **bank**
    (``cell_tiles`` and the two strides its metatile steps). ``addressing``
    stands in for the binding's override, for a caller asking what a choice
    would give before making it; left None, the binding's own is read.

    The addressing is :func:`stated_addressing`'s. The geometry an ordinal
    counts in is the referring format's record keys where the host reads them
    (:func:`reads_record_keys`); otherwise it is derived from how the source
    lays the units out (:func:`~celpix.core.tilemap.stamp_geometry`,
    :func:`~celpix.core.tilemap.metatile_geometry`).
    """
    if addressing is not None:
        stated, stated_by = addressing, "binding"
    else:
        stated, stated_by = stated_addressing(registry, entry)
    if stated is IndexAddressing.CORNER:
        return IndexReading(stated, stated_by)
    if reads_record_keys(registry, entry):
        preset = _tilemap_preset(registry, entry)
        params = preset.params if preset is not None else {}
        try:
            return IndexReading(stated, stated_by, record_geometry(params))
        except (TypeError, ValueError) as exc:
            return IndexReading(stated, stated_by, None, str(exc))
    if through is not None:
        geometry, refusal = stamp_geometry(
            stamp,
            chain_source_columns(through),
            column_major=chain_stamp_column_major(through),
            pitch=_published_pitch(through),
            header=_published_header(through),
        )
    else:
        geometry, refusal = metatile_geometry(
            cell_tiles, cell_row_stride, cell_column_stride
        )
    return IndexReading(stated, stated_by, geometry, refusal)


def document_index_reading(
    registry: Registry,
    workspace: Workspace,
    entry: Entry,
    addressing: IndexAddressing | None = None,
) -> IndexReading | None:
    """:func:`index_reading` for ``entry`` as its loaded document stands.

    Reads the chain's stamp and source, or the bank's metatile, off
    ``entry.doc`` — what a control asking "what would ordinal give here" needs,
    with ``addressing`` as the choice it is asking about. None where there is no
    tilemap document to ask, and a corner answer for the two shapes whose
    indices never count units of the bank: a sprite object's subsprites, and a
    fontmap over glyphs, whose codes already count whole glyphs. None too for a
    chained map whose source has no document to read the stamps' layout off.
    """
    doc = entry.doc
    if doc is None or not doc.is_tilemap:
        return None
    if doc.chain is not None:
        source = entry.tile_source
        bound = binding_target(workspace, source) if source is not None else None
        through = bound.doc if bound is not None else None
        if through is None:
            return None
        return index_reading(
            registry,
            entry,
            through=through,
            stamp=doc.chain.stamp,
            addressing=addressing,
        )
    if doc.is_sprite or doc.glyph_layout is not None:
        if addressing is not None:
            return IndexReading(addressing, "binding")
        return IndexReading(*stated_addressing(registry, entry))
    return index_reading(
        registry,
        entry,
        cell_tiles=doc.cell_tiles,
        cell_row_stride=doc.cell_row_stride,
        cell_column_stride=doc.cell_column_stride,
        addressing=addressing,
    )


def rebound_reading(
    registry: Registry, entry: Entry, bound: Entry
) -> IndexReading | None:
    """:func:`index_reading` for ``entry`` as it will read once bound to
    ``bound`` — asked before the rebind lands, for a base to be re-counted in
    the unit it will count (:func:`~celpix.core.tilemap.rebased`).

    Read off ``entry``'s loaded document for its own cell size and stamp, and
    off ``bound``'s for how the source lays its units out, with the binding's
    Index unit carried as it stands. None where either is not loaded, or where
    a chained map would be bound to art — its document's cell size is its
    source's, not its own — and so the reading is unknown.
    """
    doc, through = entry.doc, bound.doc
    if doc is None or through is None or not doc.is_tilemap:
        return None
    if doc.is_sprite or doc.glyph_layout is not None:
        return IndexReading(*stated_addressing(registry, entry))
    if bound.content_kind is ContentKind.TILEMAP:
        stamp = chain_stamp_cells(
            registry, entry, through, cell_stamp(registry, entry, doc.tilemap_ctx)
        )
        return index_reading(registry, entry, through=through, stamp=stamp)
    if doc.chain is not None:
        return None
    return index_reading(
        registry,
        entry,
        cell_tiles=doc.cell_tiles,
        cell_row_stride=doc.cell_row_stride,
        cell_column_stride=doc.cell_column_stride,
    )


def count_bases_in_units(registry: Registry, loaded: LoadedProject) -> list[str]:
    """Re-count, in units, every base a project from before format version 7
    counted in elements; a line for each map that could not keep its picture.

    A base counts what its map's index counts (:func:`~celpix.core.tilemap.
    index_corner`). Until version 7 it counted cells or tiles whichever way the
    index was read, and one kind of map read its index as units then: an
    engine whose index is always a record number, which turned the record into
    its corner by the record keys before the base was added. That is the one
    map whose base changes meaning, and the record keys are all its geometry
    was — so the conversion needs the registry and the entry's preset, and no
    document. :func:`reads_record_keys` and an ordinal stated by the format are
    that map exactly; any other kept its base in elements then and now.

    The migration that walks the file forward has no registry to ask, so this
    is for whoever opens the project to call once its registry is final — the
    window before it shows an entry, a script before its first
    :func:`~celpix.project.documents.load_document` — and nothing else calls
    it. A base that is a whole number of units and shifts every index alike
    becomes that number, and the map draws what it drew. One that is not
    cannot be said in units: it becomes the unit its old start falls inside,
    and the line says how far the picture moved, for the user to check before
    saving.

    Does nothing for a project whose bases already count units — a current
    file, or one this has been called on
    (:attr:`~celpix.project.projectfile.LoadedProject.bases_count_elements`):
    a base in units read as elements would be divided a second time.
    """
    notes: list[str] = []
    if not loaded.bases_count_elements:
        return notes
    loaded.bases_count_elements = False
    for entry in loaded.entries:
        source = entry.tile_source
        if source is None or not source.base_index:
            continue
        if entry.content_kind is not ContentKind.TILEMAP:
            continue
        base = source.base_index
        if not reads_record_keys(registry, entry):
            continue
        if stated_addressing(registry, entry)[0] is not IndexAddressing.ORDINAL:
            continue
        preset = _tilemap_preset(registry, entry)
        try:
            geometry = record_geometry(preset.params if preset is not None else {})
        except (TypeError, ValueError):
            continue  # refused: read as corners, where a base counts elements
        units = unit_at(base, geometry)
        across = geometry[2]
        # Adding a whole number of units moves every corner alike only where a
        # row of units does not wrap between them: always when packed, and on a
        # grid in whole rows.
        exact = units is not None and (not across or units % across == 0)
        if units is None:
            units = containing_at(abs(base), geometry) * (1 if base > 0 else -1)
        entry.tile_source = replace(source, base_index=units)
        if exact:
            continue
        over_map = source.entry is not None and (
            source.entry.content_kind is ContentKind.TILEMAP
        )
        element, unit = ("cell", "stamp") if over_map else ("tile", "metatile")
        moved = corner_at(units, geometry) - corner_at(0, geometry) - base
        how = (
            f"starts {abs(moved)} {element}{'s' if abs(moved) != 1 else ''} "
            f"{'later' if moved > 0 else 'earlier'} than it did"
            if moved
            else f"moves some {unit}s, since {unit}s here wrap every {across}"
        )
        notes.append(
            f"{entry.name}: base {element} ${base:X} is not a whole number of "
            f"{unit}s, so it is now base {unit} ${units:X} and the map {how}."
        )
    return notes


# ---------------------------------------------------------------------------
# Which open entry a binding names


def binding_target(workspace: Workspace, source: TileSource) -> Entry | None:
    """The open entry ``source`` names, or None when it names nothing usable.

    The one place a binding becomes a usable entry. The check is that the entry
    is still **open**, which holding it by identity cannot answer on its own: a
    closed entry is a live object that undo may yet put back. Scanned by
    identity rather than by ``in``, which would ask :class:`Entry` for an
    equality it deliberately does not have.
    """
    if source.mode is not TileMode.ENTRY:
        return None
    entry = source.entry
    if entry is None or not any(open_ is entry for open_ in workspace.entries):
        return None
    return entry
