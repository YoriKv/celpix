"""An entry's :class:`~celpix.core.document.Document`, with or without a window.

An entry's document is assembled from several reads and a long list of
decisions: which cell format its file is read under, what that format declares
about itself, where its tiles come from, whether they come *through* another
map, how big a stamp is and how far apart its rows are, which palette row its
cells count from, whether a font groups its tiles into glyphs. The window made
all of those decisions in its session mixin, which left a script — a verifier,
a renderer, an agent checking a generated project — no way to get the document
the app would have drawn, only a second implementation of it that drifts.

So the decisions live here, Qt-free, and the window calls them
(:mod:`celpix.ui.main_window.session`). Two things are **injected** rather than
decided, because they are the window's alone: how a pixel pathway config is
built (the window's settles a slice's parent first, so a dirty parent's edits
reach the read — ``docs/design/slices-and-parents.md`` §2) and what happens to
a problem (the window alerts; a script collects). :func:`load_document` wires
both to their headless answers and is the one entry point a script needs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

from celpix.core.arrangement import BlockLayout
from celpix.core.capabilities import ContentKind
from celpix.core.context import (
    KEY_PIXEL_PRESET,
    KEY_TILE_PALETTE_ROW_BASE,
    KEY_TILE_PALETTE_ROWS,
    KEY_TILEMAP_CELL_TILES,
    KEY_TILEMAP_PALETTE_ROW_BASE,
    KEY_TILEMAP_STAMP_CELLS,
    KEY_TILEMAP_STAMP_COLUMN_MAJOR,
    KEY_TILEMAP_STAMP_STRIDE,
    PipelineContext,
)
from celpix.core.document import CellChain, Document
from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.core.palette import FULL_PALETTE_COUNT, Palette
from celpix.core.paletteregions import PaletteRegion, PaletteRegions
from celpix.core.tilemap import VRAM_ROW_STRIDE, Cell
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import NO_RESHAPE, STAGE_DEFAULT_PRESET, FileRef
from celpix.project.workspace import (
    Entry,
    EntryKind,
    PaletteMode,
    PaletteSource,
    TileMode,
    TileSource,
    Workspace,
    backfill_slice_length,
    can_supply_palette,
    composite_config,
    composite_layout,
    entry_view_bytes,
    pixel_config_for,
    record_composite_layout,
    reorders_bytes,
    tilemap_config_for,
)

#: What a tilemap entry falls back to when nothing else named a cell codec — a
#: container normally supplies one (``detect.tilemap_preset_for``), so this is
#: for a tilemap that was carved out by hand rather than detected.
DEFAULT_TILEMAP_PRESET = STAGE_DEFAULT_PRESET[Stage.INTERPRET_TILEMAP]
DEFAULT_PIXEL_PRESET = STAGE_DEFAULT_PRESET[Stage.INTERPRET_PIXEL]

#: ``(entry, preset id) -> config``: how the caller builds a pixel pathway.
PixelConfig = Callable[[Entry, str], PathwayConfig]


class BoundTiles(NamedTuple):
    """The art a tilemap entry draws from, ready to become the document's
    pixel half — or an empty stand-in when nothing is bound."""

    data: bytes
    bytes_per_tile: int
    tile_width: int
    tile_height: int
    ctx: PipelineContext
    config: PathwayConfig


def fallback_palette() -> Palette:
    """The generated palette shown until a real one is loaded — full length.

    Sized to the whole 256 rather than one palette row's worth: the generator
    puts a contrasting row first, a **grayscale ramp second** and distinct
    colors after, none of which exists at all if only the format's index
    space is asked for (a 4bpp view would stop at 16 — one row, no ramp).
    At full length every palette row the row spin can reach is populated, so
    single-channel data can be read as a ramp by stepping to row 1, and
    forking Default → Custom keeps the palette exactly the size it was.
    """
    return Palette.default(FULL_PALETTE_COUNT)


def placeholder_palette_config(preset_id: str) -> PathwayConfig:
    """The no-palette-loaded config: empty source, never written back."""
    return PathwayConfig(
        source=FileRef(""), interpret_preset_id=preset_id, write_enabled=False
    )


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


def preset_declares(registry, preset_id: str, name: str) -> object:  # noqa: ANN001
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


def tilemap_declares(registry, entry: Entry, name: str) -> object:  # noqa: ANN001
    """What ``entry``'s cell format declares under ``name``, or None."""
    return preset_declares(registry, tilemap_preset_id(entry), name)


def is_fontmap(registry, entry: Entry) -> bool:  # noqa: ANN001
    """Whether ``entry``'s format says its cells are character codes
    (``docs/design/fontmap-entry.md``)."""
    return tilemap_declares(registry, entry, "layout") == "text"


def is_sprite(registry, entry: Entry) -> bool:  # noqa: ANN001
    """Whether ``entry``'s format says its cells are subsprites grouped into
    frames rather than positions in a grid (``docs/design/tilemap-entry.md`` §6)."""
    return tilemap_declares(registry, entry, "layout") == "sprite"


def states_subsprite_size(registry, entry: Entry) -> bool:  # noqa: ANN001
    """Whether ``entry``'s format gives each subsprite its own rectangle.

    Most sprite records hold a size *bit* picking between two squares the file
    never records, so the pair is a setting the user supplies; a Mega Drive
    record holds the console's own size nibble and states a rectangle outright.
    """
    return tilemap_declares(registry, entry, "subsprite_size") == "stated"


def is_indirect(registry, entry: Entry) -> bool:  # noqa: ANN001
    """Whether ``entry``'s format says its cells are coordinates into a map.

    It decides how the bar reads while nothing is bound, never what a map may
    draw through — chaining is generic and gated on depth (:func:`bound_tilemap`).
    """
    return bool(tilemap_declares(registry, entry, "indirect"))


def is_dense(registry, entry: Entry) -> bool:  # noqa: ANN001
    """Whether ``entry``'s format holds one entry per stamp.

    The referring half of a stamped chain, and the only part of one that is not
    the source's to answer (:attr:`~celpix.core.document.CellChain.dense`). A
    stamp layout has a slot per drawn position and only its corners are read; a
    map of 16x16 metatiles over an 8x8 bank has a slot per stamp. Which shape a
    file is, is fixed by its format, so it is declared beside ``indirect``.
    False for a format that declares nothing: a wrong guess would expand a map to
    four times its size.
    """
    return bool(tilemap_declares(registry, entry, "stamp_dense"))


def declared_cell_row_stride(registry, entry: Entry) -> int:  # noqa: ANN001
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


def flag_break(registry, entry: Entry) -> bool:  # noqa: ANN001
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


def chain_stamp_cells(registry, entry: Entry, through: Document) -> tuple[int, int]:  # noqa: ANN001
    """How many of ``through``'s **cells** one of ``entry``'s coordinates names.

    Whichever side states it, the referrer first: a format whose coordinates
    always name a fixed block declares ``stamp_cells``; otherwise the source's
    published answer (:data:`~celpix.core.context.KEY_TILEMAP_STAMP_CELLS`).
    ``(1, 1)`` for a pair that states nothing, and for a malformed declaration,
    since a wrong guess would expand the map to a multiple of its size.
    """
    stated = tilemap_declares(
        registry, entry, "stamp_cells"
    ) or through.tilemap_ctx.get(KEY_TILEMAP_STAMP_CELLS)
    try:
        across, down = stated or (1, 1)
        return max(1, int(across)), max(1, int(down))
    except (TypeError, ValueError):
        return (1, 1)


def chain_source_columns(through: Document) -> int:
    """The stride between a stamp's rows, in cells of the **source**.

    The source's own answer first (:data:`~celpix.core.context.
    KEY_TILEMAP_STAMP_STRIDE`): a table of packed records stamps at the record's
    width whatever it is displayed at. Else the width its format states;
    otherwise the width its cells are laid at is the view's, and file order is
    drawn order there. A stride of 1 in that case would walk a stamp's second
    row along the same source row instead of down one.
    """
    stride = through.tilemap_ctx.get(KEY_TILEMAP_STAMP_STRIDE)
    try:
        if stride and int(stride) >= 1:
            return int(stride)
    except (TypeError, ValueError):
        pass
    return through.stated_columns or max(1, through.view.columns)


def chain_stamp_column_major(through: Document) -> bool:
    """Whether the source stores a stamp's cells down each column.

    Only ever the source's own statement
    (:data:`~celpix.core.context.KEY_TILEMAP_STAMP_COLUMN_MAJOR`), published
    beside the stride it changes the meaning of: a table viewed at some width is
    laid out row by row, whatever order its records keep inside.
    """
    return bool(through.tilemap_ctx.get(KEY_TILEMAP_STAMP_COLUMN_MAJOR))


def row_base_for(
    entry: Entry, declared: int, *, stated: bool = True, bank: int | None = None
) -> int:
    """The palette row ``entry``'s named rows count their row 0 from.

    Most specific first: the entry's own value wins outright, since what no file
    can know is which palette got loaded
    (:attr:`~celpix.project.workspace.Entry.palette_row_base`). Then the map's
    own header, where its format has one — ``stated`` is whether ``declared``
    came from the file rather than from the preset. Then **the bank's**: a
    sprite object names a row and carries nothing to count it from, and the tile
    bank those subsprites draw from may state one
    (:data:`~celpix.core.context.KEY_TILE_PALETTE_ROW_BASE`). The preset last.
    """
    chosen = entry.palette_row_base
    if chosen is not None:
        return chosen
    if stated or bank is None:
        return declared
    return bank


def glyph_layout_for(entry: Entry) -> BlockLayout | None:
    """How the bound font groups its tiles into glyphs, or None for one each.

    **The font sheet's own arrangement**: its Cols and its Pattern block. That
    is the whole of what an 8x16 font needs said — a glyph is two tiles, and
    which two is decided by how wide the sheet is and whether the bottoms follow
    the tops or sit under them (``docs/design/fontmap-entry.md`` §4). None
    wherever the grouping is 1x1, which is nearly every font. Read off the
    font's document where it has one and off its restored view where it has not,
    because a map routinely loads before the sheet it draws from.
    """
    source = entry.tile_source
    font = source.entry if source is not None else None
    if font is None or not font.is_font_sheet:
        return None
    view = font.doc.view if font.doc is not None else font.pending_view
    if view is None or (view.block_columns <= 1 and view.block_rows <= 1):
        return None
    return BlockLayout(
        max(1, view.columns), view.block_columns, view.block_rows, view.block_order
    )


def seed_tile_palette_rows(doc: Document, table: bytes) -> None:
    """Turn a bank's per-tile palette rows into pinned palette regions.

    The file is saying what pinned regions otherwise have to be told by hand, so
    it seeds them and they behave like any other pin from there. Rows are stored
    as the file states them — **relative to the entry's palette row base** — so
    the base spin can re-aim a whole bank without rewriting a region. Runs of
    equal rows collapse into one region each. Row 0 is pinned like any other: it
    is a row the file *named*, and leaving it out would let those tiles drift
    with the view's row selector while their neighbours stayed put.
    """
    if not table or doc.bytes_per_tile <= 0:
        return
    per_tile = doc.tile_width * doc.tile_height
    if per_tile <= 0:
        return
    regions, start, row = [], 0, table[0]
    for index in range(1, len(table) + 1):
        here = table[index] if index < len(table) else None
        if here != row:
            regions.append(
                PaletteRegion(start * per_tile, (index - start) * per_tile, row)
            )
            start, row = index, here
    if regions:
        doc.view.palette_regions = PaletteRegions.from_regions(regions)


def fit_tile_base(
    entry: Entry,
    cells: list[Cell],
    tiles: BoundTiles,
    cell_tiles: tuple[int, int],
    named: frozenset[int] = frozenset(),
) -> None:
    """Shift a map onto a source it overflows, when its own indices say how.

    A map's cells and the entry supplying its tiles routinely number from
    different places. If the lowest index is the amount by which the highest
    overflows the source, the map is the same picture shifted and ``-min`` lands
    it. Deliberately narrow: only when the map does not fit as it stands and does
    once shifted — a condition an absolutely-indexed map never meets — and never
    over a base the user set. ``named`` is the codes a fontmap's font names past
    the end of its sheet (a terminator, a space with no glyph), which draw
    nothing by design and are not an overflow.
    """
    source = entry.tile_source
    if source is None or not source.is_bound or source.base_index or not cells:
        return
    count = len(tiles.data) // max(1, tiles.bytes_per_tile)
    if not count:
        return  # unreadable binding: nothing to fit against
    indices = [
        cell.index for cell in cells if cell.index < count or cell.index not in named
    ]
    if not indices:
        return
    low, high = min(indices), max(indices)
    # A cell covering several tiles reaches past its own index, so the span has
    # to allow for what the widest of them draws.
    across, down = max(1, cell_tiles[0]), max(1, cell_tiles[1])
    reach = high + (down - 1) * VRAM_ROW_STRIDE + (across - 1)
    if low and reach >= count and reach - low < count:
        entry.tile_source = replace(source, base_index=-low)


# ---------------------------------------------------------------------------
# Where a map's tiles come from


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


def palette_entry_target(
    workspace: Workspace, entry: Entry, source: Entry | None
) -> Entry | None:
    """The open entry an ENTRY palette names, or None when it names nothing usable.

    :func:`binding_target`'s twin, and the same two questions in the same order:
    the entry has to still be **open** — which holding it by identity cannot
    answer, a closed entry being a live object undo may yet put back — and it has
    to be something this consumer may read
    (:func:`~celpix.project.workspace.can_supply_palette`). Scanned by identity
    rather than with ``in``, which would ask :class:`Entry` for an equality it
    deliberately does not have.
    """
    if source is None:
        return None
    if not any(open_ is source for open_ in workspace.entries):
        return None
    return source if can_supply_palette(entry, source) else None


def draws_through_tilemap(workspace: Workspace, entry: Entry) -> bool:
    """Whether ``entry``'s binding names another tilemap rather than art.

    Read off the **binding**, not off a loaded document, so it is safe to ask
    while loading and a pair of maps pointed at each other cannot recurse.
    """
    if entry.content_kind is not ContentKind.TILEMAP:
        return False
    source = entry.tile_source
    bound = binding_target(workspace, source) if source is not None else None
    return (
        bound is not None
        and bound is not entry
        and bound.content_kind is ContentKind.TILEMAP
    )


def can_supply_tiles(workspace: Workspace, entry: Entry, candidate: Entry) -> bool:
    """Whether ``candidate`` is a source ``entry`` could draw through: art always,
    and a tilemap only while it reaches art itself. Never the entry itself and
    never a bookmark."""
    if candidate is entry or candidate.kind is EntryKind.BOOKMARK:
        return False
    if candidate.content_kind is ContentKind.PIXELS:
        return True
    if candidate.content_kind is not ContentKind.TILEMAP:
        return False
    return not draws_through_tilemap(workspace, candidate)


def font_alphabet_for(registry, workspace: Workspace, entry: Entry, cell_bytes: int):  # noqa: ANN001, ANN201
    """The lookup ``entry``'s codes read through — the font's, whole.

    Read only from a sheet that says it is a font (**Use as Font**); unticking
    keeps the table, so reading it anyway would leave the tick meaning nothing.
    ``cell_bytes`` sets how wide an unmapped code prints, which is the stream's
    measure and not the font's.
    """
    if not is_fontmap(registry, entry):
        return None
    bound = binding_target(workspace, entry.tile_source) if entry.tile_source else None
    font = bound if bound is not None and bound.is_font_sheet else None
    return pipeline.load_font_alphabet(
        font.font_chars if font is not None else "",
        font.font_codes if font is not None else (),
        code_digits=max(1, cell_bytes) * 2,
        base=font.font_base if font is not None else 0,
        flag_break=flag_break(registry, entry),
    )


def no_tiles(registry, preset_id: str) -> BoundTiles:  # noqa: ANN001
    """The stand-in for an unbound or unreadable source: geometry, no bytes.

    The tile size still comes from a real codec so the cells have a size to be
    drawn at — a blank map at the right scale reads as "no tiles yet".
    """
    preset_id = preset_id or DEFAULT_PIXEL_PRESET
    cfg = PathwayConfig(
        source=FileRef(""), interpret_preset_id=preset_id, write_enabled=False
    )
    try:
        engine, preset = registry.engine_for(preset_id)
        width, height = engine.tile_size(preset.params)
        per_tile = engine.bytes_per_tile(preset.params)
    except Exception:  # noqa: BLE001 — a stand-in must not be able to fail
        width = height = 8
        per_tile = 32
    return BoundTiles(b"", per_tile, width, height, PipelineContext(), cfg)


def tile_source_config(
    workspace: Workspace,
    entry: Entry,
    source: TileSource,
    pixel_config: PixelConfig,
    fallback_preset: str,
) -> PathwayConfig:
    """The pathway that reads the tiles ``source`` points at.

    Resolved through the bound entry's own config, so the tiles are read exactly
    as that entry reads them — its container, reshape and codec. Refused where
    the bound entry draws through a tilemap itself: a source can gain a binding
    of its own after this one was made, and reading its *coordinates* through a
    pixel codec would draw them as art. Read, but **never written**: the tiles
    belong to the bound entry (``docs/design/tilemap-entry.md`` §3).
    """
    bound = binding_target(workspace, source)
    if bound is None:
        name = source.entry.name if source.entry is not None else "nothing"
        raise KeyError(f"the tiles are bound to {name}, which is not open")
    if not can_supply_tiles(workspace, entry, bound):
        raise KeyError(f"{bound.name} draws through a tilemap itself")
    preset = (
        bound.session.pixel_preset_id if bound.session is not None else fallback_preset
    )
    return replace(
        pixel_config(bound, preset), write_enabled=False, writes_through_parent=False
    )


def live_bound_tiles(
    workspace: Workspace, source: TileSource, cfg: PathwayConfig
) -> BoundTiles | None:
    """The bound entry's **loaded** art, taken from its document rather than read.

    A binding names an entry so the map draws what that entry currently holds,
    and an entry's unsaved edits live only in its document. **The same rule as**
    :func:`~celpix.project.workspace.entry_view_bytes` — "the live document's
    bytes when one is loaded, else the region read fresh" — kept separate because
    that function drops the context a bound read has to carry. None when there is
    no document, the ordinary case on a project load. The geometry comes from the
    document too: a config naming another preset would cut the same buffer into
    different tiles.
    """
    bound = binding_target(workspace, source)
    doc = bound.doc if bound is not None else None
    if doc is None or doc.is_tilemap:
        return None
    return BoundTiles(
        doc.pixel_data,
        doc.bytes_per_tile,
        doc.tile_width,
        doc.tile_height,
        doc.pixel_ctx,
        cfg,
    )


def apply_pixel_preset_hint(
    entry: Entry, px, cfg: PathwayConfig, registry, pixel_config: PixelConfig
):  # noqa: ANN001, ANN201
    """Adopt the format the container says its payload is in, if it says.

    A tile bank that records its own bit depth should not need one guessed: 2bpp,
    4bpp and 8bpp all decode into something that *looks* like graphics. Only the
    geometry is re-derived, and only on a **fresh** entry — once a project has
    stored a format, or the user picked one, that is the answer.
    """
    wanted = str(px.ctx.get(KEY_PIXEL_PRESET, "") or "")
    session = entry.session
    if not wanted or session is None or entry.pending_view is not None:
        return px, cfg
    if wanted == session.pixel_preset_id:
        return px, cfg
    try:
        regeared = pipeline.reinterpret_pixel_data(px.data, px.ctx, cfg, registry)
    except PipelineError:
        return px, cfg  # the hint named something this build hasn't got
    session.pixel_preset_id = wanted
    return regeared, pixel_config(entry, wanted)


# ---------------------------------------------------------------------------
# The documents themselves


def pixel_document(entry: Entry, px, cfg: PathwayConfig) -> Document:  # noqa: ANN001
    """A pixel entry's document, on the placeholder palette until one is applied."""
    assert entry.session is not None
    return Document(
        pixel_data=px.data,
        bytes_per_tile=px.bytes_per_tile,
        tile_width=px.tile_width,
        tile_height=px.tile_height,
        palette=fallback_palette(),
        pixel_config=cfg,
        palette_config=placeholder_palette_config(entry.session.palette_preset_id),
        pixel_ctx=px.ctx,
        # A bank states where its own rows count from, and its per-tile table
        # counts from exactly there. Nothing in a preset can say it — it is a fact
        # about this file — so the declared answer is 0 and the header the only
        # other voice.
        palette_row_base=row_base_for(
            entry, 0, stated=False, bank=px.ctx.get(KEY_TILE_PALETTE_ROW_BASE)
        ),
    )


def chained_document(
    registry, entry: Entry, loaded, cfg: PathwayConfig, through: Document
) -> Document:  # noqa: ANN001
    """A map whose cells are coordinates into ``through``'s cells.

    Two hops, and the second is an ordinary binding — which is as far as it goes
    (:func:`bound_tilemap`). The drawing geometry is the source map's, because
    what is drawn is its cells; this entry's own record size is what the hex dump
    shows. The pixel config stays read-only: the art belongs to the map at the end
    of the chain, and a restamp must never reach it.
    """
    assert entry.session is not None
    return Document(
        pixel_data=through.pixel_data,
        bytes_per_tile=through.bytes_per_tile,
        tile_width=through.tile_width,
        tile_height=through.tile_height,
        palette=fallback_palette(),
        pixel_config=replace(through.pixel_config, write_enabled=False),
        palette_config=placeholder_palette_config(entry.session.palette_preset_id),
        pixel_ctx=through.pixel_ctx,
        cells=loaded.cells,
        # The stamp size is whichever side states it; whether this file holds an
        # entry per stamp or one per drawn position is its own format's constant
        # (`docs/design/tilemap-entry.md` §3.1).
        chain=CellChain(
            through.cells or [],
            loaded.palette_rows,
            stamp=chain_stamp_cells(registry, entry, through),
            source_columns=chain_source_columns(through),
            dense=is_dense(registry, entry),
            stamp_column_major=chain_stamp_column_major(through),
        ),
        tilemap_config=cfg,
        tilemap_ctx=loaded.ctx,
        tilemap_data=loaded.data,
        cell_bytes=loaded.cell_bytes,
        cell_tiles=through.cell_tiles,
        cell_row_stride=through.cell_row_stride,
        tile_base_index=through.tile_base_index,
        index_mask=through.index_mask,
        # The source map's rows are what get drawn, so its base applies — unless
        # this entry states one of its own.
        palette_row_base=row_base_for(entry, through.palette_row_base),
        # Rows are stated if *either* side states them.
        cells_carry_palette_rows=(
            through.cells_carry_palette_rows or loaded.palette_rows
        ),
        # **This entry's own**: what it sizes is a *write*, snapped against the
        # referrer's own cells (`Document.palette_row_group`).
        palette_row_granularity=loaded.row_granularity,
        column_major=loaded.column_major,
    )


def tilemap_document(
    registry,  # noqa: ANN001
    workspace: Workspace,
    entry: Entry,
    loaded,  # noqa: ANN001
    cfg: PathwayConfig,
    tiles: BoundTiles,
) -> Document:
    """A map drawn from art: its own cells over ``tiles``.

    The cell size is the header's answer over the preset's assumption. A
    **fontmap**'s cell draws whatever one glyph of its font is, so the grouping
    comes from the font — but only where the format has not claimed the cell
    covers several tiles itself. The base-fit (:func:`fit_tile_base`) is skipped
    under a glyph layout, where indices are block numbers and the arithmetic
    would be in the wrong unit.
    """
    assert entry.session is not None
    cell_tiles = loaded.ctx.get(KEY_TILEMAP_CELL_TILES) or loaded.cell_tiles
    fontmap = is_fontmap(registry, entry)
    glyph_layout = glyph_layout_for(entry) if fontmap else None
    if glyph_layout is not None and cell_tiles == (1, 1):
        cell_tiles = (glyph_layout.block_columns, glyph_layout.block_rows)
    else:
        glyph_layout = None
    if glyph_layout is None:
        font = entry.tile_source.entry if fontmap and entry.tile_source else None
        named = frozenset(g.code for g in font.font_codes) if font else frozenset()
        fit_tile_base(entry, loaded.cells, tiles, cell_tiles, named)
    return Document(
        pixel_data=tiles.data,
        bytes_per_tile=tiles.bytes_per_tile,
        tile_width=tiles.tile_width,
        tile_height=tiles.tile_height,
        palette=fallback_palette(),
        pixel_config=tiles.config,
        palette_config=placeholder_palette_config(entry.session.palette_preset_id),
        pixel_ctx=tiles.ctx,
        cells=loaded.cells,
        # View-only where the cells are subsprites, for the same reason a stamp
        # layout is: what a canvas gesture would edit is not settled.
        tilemap_config=(replace(cfg, write_enabled=False) if loaded.frames else cfg),
        tilemap_ctx=loaded.ctx,
        tilemap_data=loaded.data,
        cell_bytes=loaded.cell_bytes,
        cell_tiles=cell_tiles,
        # The stride is the fixed-offset way of finding a cell's other tiles and a
        # glyph layout the general one, so they are never both set.
        cell_row_stride=(
            0
            if glyph_layout is not None or cell_tiles == (1, 1)
            else declared_cell_row_stride(registry, entry)
        ),
        glyph_layout=glyph_layout,
        index_mask=loaded.index_mask,
        palette_row_base=row_base_for(
            entry,
            loaded.palette_row_base,
            stated=loaded.ctx.get(KEY_TILEMAP_PALETTE_ROW_BASE) is not None,
            bank=tiles.ctx.get(KEY_TILE_PALETTE_ROW_BASE),
        ),
        tile_base_index=(
            entry.tile_source.base_index if entry.tile_source is not None else 0
        ),
        sprite_frames=loaded.frames,
        # The pair the frames were **built at**, read back rather than re-derived.
        sprite_size_pair=loaded.size_pair,
        cells_carry_palette_rows=loaded.palette_rows,
        palette_row_granularity=loaded.row_granularity,
        column_major=loaded.column_major,
        text_layout=fontmap,
        font_alphabet=font_alphabet_for(registry, workspace, entry, loaded.cell_bytes),
    )


# ---------------------------------------------------------------------------
# Offset palettes, in the owning file's coordinates (docs/design/palette-editing.md §2)


def palette_offset_owner(workspace: Workspace, entry: Entry | None) -> Entry | None:
    """The FILE entry whose coordinates ``entry``'s Offset palette is in.

    ``entry`` itself when it is a whole file; its **parent** when it is a slice,
    because a slice's palette offsets are parent-absolute and deliberately reach
    outside its own window. ``None`` when the parent is not open.

    A **composite** comes from no file, so it borrows the coordinates of its
    first piece that has an entry — the file a VRAM window's first bank sits in,
    which is where a ROM keeps the colours for it. ``None`` for a composite of
    pads only. Reordering the pieces across files moves the owner with them.
    """
    if entry is None:
        return None
    if entry.kind is EntryKind.FILE:
        return entry
    if entry.kind is EntryKind.COMPOSITE:
        first = next((p.entry for p in entry.pieces if p.entry is not None), None)
        return palette_offset_owner(workspace, first) if first is not entry else None
    return workspace.find_file(entry.path)


def offset_palette_files(workspace: Workspace, entry: Entry) -> tuple[str, ...]:
    """The files ``entry``'s Offset palette offsets address: the owner's, or its
    own when the parent is not open — which is that same list, since a slice
    carries the parent's files."""
    owner = palette_offset_owner(workspace, entry)
    return owner.paths if owner is not None else entry.paths


def reordered_view(
    registry,
    workspace: Workspace,
    owner: Entry,
    settle: Callable[[Entry], None] | None = None,  # noqa: ANN001
    fallback_preset: str = DEFAULT_PIXEL_PRESET,
) -> tuple[bytes, int] | None:
    """``owner``'s view buffer and its base, or ``None`` when reading the file
    would give the same bytes. A permuting container or an active reshape makes
    the buffer a different address space from the file, and then the buffer is
    the only place an offset means anything."""
    if not reorders_bytes(owner, registry):
        return None
    if settle is not None:
        settle(owner)
    return entry_view_bytes(
        owner,
        registry,
        owner.session.pixel_preset_id if owner.session is not None else fallback_preset,
        workspace,
    )


def offset_palette_space(
    registry,
    workspace: Workspace,
    entry: Entry,
    settle: Callable[[Entry], None] | None = None,  # noqa: ANN001
    fallback_preset: str = DEFAULT_PIXEL_PRESET,
) -> tuple[tuple[bytes, int] | None, int]:
    """The address space ``entry``'s Offset palette reads: ``(view, end)``.

    ``view`` is the owner's ``(buffer, base)`` when it reorders bytes, else
    ``None``, meaning offsets are file offsets into the joined files. ``base``
    mirrors the owner's own anchor: under an active reshape offsets are 0-based
    buffer positions; under a permuting container they keep the recorded start.
    """
    owner = palette_offset_owner(workspace, entry)
    view = (
        reordered_view(registry, workspace, owner, settle, fallback_preset)
        if owner is not None
        else None
    )
    if view is not None:
        data, base = view
        if owner is not None and owner.reshape_id != NO_RESHAPE:
            base = 0
        return (data, base), base + len(data)
    paths = offset_palette_files(workspace, entry)
    return None, sum(Path(p).stat().st_size for p in paths)


def offset_palette_source(
    registry,  # noqa: ANN001
    workspace: Workspace,
    entry: Entry,
    byte_off: int,
    preset_id: str,
    settle: Callable[[Entry], None] | None = None,
    fallback_preset: str = DEFAULT_PIXEL_PRESET,
) -> tuple[FileRef | None, bool]:
    """The read window for an Offset palette at ``byte_off``, and whether a color
    edit can be written back through it.

    Floored to whole entries — the color codecs reject a partial trailing one —
    and capped at a full palette. ``(None, ...)`` when not even one entry fits.
    Where the owner reorders bytes the window is cut from its view buffer and the
    pathway comes back write-off: a length-bounded ``FileRef`` cannot say where a
    permuted splice belongs.
    """
    view, end = offset_palette_space(
        registry, workspace, entry, settle, fallback_preset
    )
    writable = view is None
    base = 0 if view is None else view[1]
    avail = end - byte_off if byte_off >= base else 0
    colors = min(
        FULL_PALETTE_COUNT, pipeline.palette_entry_capacity(avail, preset_id, registry)
    )
    if colors <= 0:
        return None, writable
    length = pipeline.palette_read_bytes(colors, preset_id, registry)
    paths = offset_palette_files(workspace, entry)
    if view is None:
        return FileRef(paths, offset=byte_off, length=length), True
    data, base = view
    return FileRef(
        paths, offset=byte_off, length=length, data=data, data_base=base
    ), False


def entry_palette_source(
    registry,  # noqa: ANN001
    workspace: Workspace,
    source: Entry,
    byte_off: int,
    preset_id: str,
    settle: Callable[[Entry], None] | None = None,
    fallback_preset: str = DEFAULT_PIXEL_PRESET,
) -> FileRef | None:
    """The read window an ENTRY palette takes out of ``source``'s resolved bytes.

    :func:`offset_palette_source`'s twin for the other cross-entry palette
    reference, sized by the same two rules — floored to whole colour entries,
    because the codecs reject a partial trailing one, and capped at a full
    palette. ``None`` when not even one entry fits.

    ``byte_off`` indexes the source's **resolved** data from 0 — what it looks
    like once its own container, reshape and decompressor have run, which is the
    same space a composite piece's range addresses
    (:class:`~celpix.project.workspace.CompositePiece`). The bytes ride on the
    ref inline, so the read never touches disk and never runs a second set of
    byte stages over a buffer that has already been through its own.

    The window is **not** writable on its own account: the colours belong to the
    source's bytes, and an edit rides that entry's pixel pathway exactly as a
    reordered Offset palette rides its owner's
    (``docs/design/palette-editing.md``).

    ``settle`` pays whatever fold the source owes before its buffer is believed,
    the way :func:`reordered_view` does for an Offset owner.
    """
    if settle is not None:
        settle(source)
    session = source.session
    preset = session.pixel_preset_id if session is not None else fallback_preset
    data, _base = entry_view_bytes(source, registry, preset, workspace)
    avail = len(data) - byte_off if byte_off >= 0 else 0
    colors = min(
        FULL_PALETTE_COUNT, pipeline.palette_entry_capacity(avail, preset_id, registry)
    )
    if colors <= 0:
        return None
    length = pipeline.palette_read_bytes(colors, preset_id, registry)
    return FileRef(
        # A composite comes from no file, so its own name is what the address
        # display and any error message have to call this buffer.
        source.paths or (source.name or "entry",),
        offset=byte_off,
        length=length,
        data=data,
        data_base=0,
    )


def entry_palette_config(
    entry: Entry,
    source: PaletteSource,
    registry,  # noqa: ANN001
    workspace: Workspace,
    settle: Callable[[Entry], None] | None = None,
    preset_id: str | None = None,
) -> PathwayConfig:
    """The pathway ``entry`` reads its ENTRY-mode palette through.

    One builder for every route into the mode — the project restore, the headless
    load and the dock's own re-decode — so what a reopened project shows and what
    the gesture produced cannot differ. Raises where the app degrades to the
    default palette: a source that is no longer open, or one with too few bytes
    left at the stated offset for a single colour.

    The **consumer** owns the colour format, which is the whole difference from
    File mode: these are bytes in somebody else's buffer, and how to read them is
    a fact about the picture being coloured rather than about the entry holding
    them. *Which* of the consumer's two answers is the live one depends on the
    route, so ``preset_id`` is explicit rather than always the session's: a
    loaded entry's format is on its **document's** palette config, and its
    session's copy is only written on an entry switch — so the entry on screen
    would otherwise be re-decoded at whatever format it was opened with, silently
    undoing a Format pick. The session's is right for the restore route alone,
    where there is no live config yet.
    """
    session = entry.session
    assert session is not None
    wanted = preset_id or session.palette_preset_id
    target = palette_entry_target(workspace, entry, source.entry)
    if target is None:
        named = source.entry.name if source.entry is not None else "nothing"
        raise PipelineError(
            Stage.CONTAINER,
            Pathway.PALETTE,
            f"the palette is read from {named}, which is not open",
            plugin=wanted,
        )
    ref = entry_palette_source(
        registry, workspace, target, source.offset, wanted, settle
    )
    if ref is None:
        raise PipelineError(
            Stage.CONTAINER,
            Pathway.PALETTE,
            f"not enough data at that offset in {target.name}",
            plugin=wanted,
        )
    # Never writable on its own account: the bytes are the source's, so a colour
    # edit rides that entry's pixel pathway instead
    # (``docs/design/palette-editing.md``).
    return PathwayConfig(source=ref, interpret_preset_id=wanted, write_enabled=False)


def file_palette_config(
    path: str, offset: int, preset_id: str, container_id: str
) -> PathwayConfig:
    """The writable pathway a PALETTE entry reads and writes its ``.pal`` with.

    Source and dest are the same file; ``container_id`` is what cuts the colors
    out of a file that holds more than colors, and re-wraps them on the way back.
    """
    return PathwayConfig(
        source=FileRef(path, offset=offset),
        dest=FileRef(path, offset=offset),
        interpret_preset_id=preset_id,
        container_id=container_id,
    )


def emulator_palette_config(path: str, registry):  # noqa: ANN001, ANN201
    """Detect the emulator state at ``path``: ``(format, palette config)``.

    The console dictates the codec, so this cannot be set to the wrong colour
    format. View-only: a state is a memory dump, never a palette written back.
    Raises :class:`~celpix.core.emustate.StateError` when nothing is recognised.
    """
    from celpix.core import emustate  # noqa: PLC0415 — only the emulator mode needs it

    data = Path(path).read_bytes()
    fmt, region = emustate.locate_palette(data)
    length = min(
        len(region.data),
        pipeline.palette_read_bytes(region.count, region.preset_id, registry),
    )
    ref = FileRef(path, offset=0, length=length, data=region.data)
    return fmt, PathwayConfig(
        source=ref, interpret_preset_id=region.preset_id, write_enabled=False
    )


# ---------------------------------------------------------------------------
# Headless: the whole load, as the app performs it


@dataclass
class Loaded:
    """What :func:`load_document` produced, and what it had to assume."""

    doc: Document
    #: Each thing the app would have told the user about — a binding it could
    #: not read, a palette it could not restore — rather than an alert.
    problems: list[str] = field(default_factory=list)


def load_document(entry: Entry, registry, workspace: Workspace) -> Loaded:  # noqa: ANN001
    """``entry``'s document as the app builds it, with no window.

    Every decision is the one the app makes (the functions above are what its
    session code calls), and so are the reads — through ``pixel_config_for`` and
    ``tilemap_config_for`` with the workspace, a composite through
    ``composite_layout``, a chained map through its source's own load. The
    restored view and palette are applied the way a project restore applies
    them, so the document carries the entry's columns, palette row and colours.
    What differs is only what has no headless meaning: nothing is settled (there
    are no unsaved edits), and a problem is collected instead of alerted.

    Sets ``entry.doc``, since that is where a bound map finds its source's live
    buffer, and returns it with the problems met on the way. Raises
    :class:`~celpix.core.errors.PipelineError` where the app would have refused
    to open the entry at all.
    """
    problems: list[str] = []
    _load(entry, registry, workspace, problems)
    assert entry.doc is not None
    return Loaded(entry.doc, problems)


def _pixel_config(registry, workspace: Workspace) -> PixelConfig:  # noqa: ANN001
    def build(entry: Entry, preset_id: str) -> PathwayConfig:
        if entry.kind is EntryKind.COMPOSITE:
            return composite_config(entry, registry, workspace, preset_id=preset_id)
        return pixel_config_for(entry, preset_id, registry, workspace)

    return build


def _load(entry: Entry, registry, workspace: Workspace, problems: list[str]) -> None:  # noqa: ANN001
    if entry.doc is not None:
        return
    assert entry.session is not None, f"{entry.name} has no session to load with"
    session = entry.session
    configure = _pixel_config(registry, workspace)
    if entry.content_kind is ContentKind.TILEMAP:
        # Noted before the restore consumes it: a width the format states only
        # seeds Cols for an entry the project had no view for.
        had_view = entry.pending_view is not None
        _load_tilemap(entry, registry, workspace, problems, configure)
        _restore(entry, registry, workspace, problems)
        doc = entry.doc
        # The app's `_apply_tilemap_columns`: the file states its own width in
        # its own unit, and the document turns it into the drawn width.
        if not had_view and doc.stated_columns:
            doc.view.columns = doc.drawn_columns or doc.stated_columns
    else:
        if entry.kind is EntryKind.COMPOSITE:
            layout = composite_layout(
                entry, registry, workspace, session.pixel_preset_id
            )
            record_composite_layout(entry, layout)
            problems.extend(f"{entry.name}: {p}" for p in layout.problems)
            cfg = composite_config(
                entry,
                registry,
                workspace,
                layout=layout,
                preset_id=session.pixel_preset_id,
            )
        else:
            cfg = configure(entry, session.pixel_preset_id)
        pending = entry.pending_view
        width = (
            pending.bitmap_width
            if pending is not None and pending.two_dimensional
            else 0
        )
        px = pipeline.load_pixel_data(cfg, registry, width)
        if backfill_slice_length(entry, px.ctx):
            cfg = configure(entry, session.pixel_preset_id)
        px, cfg = apply_pixel_preset_hint(entry, px, cfg, registry, configure)
        entry.doc = pixel_document(entry, px, cfg)
        _restore(entry, registry, workspace, problems)
        if entry.doc.view.palette_regions.is_empty():
            seed_tile_palette_rows(entry.doc, px.ctx.get(KEY_TILE_PALETTE_ROWS, b""))


def _load_tilemap(entry, registry, workspace, problems, configure) -> None:  # noqa: ANN001
    cfg = tilemap_config_for(entry, tilemap_preset_id(entry), registry, workspace)
    loaded = pipeline.load_tilemap_data(cfg, registry, size_pair=entry.sprite_size_pair)
    if backfill_slice_length(entry, loaded.ctx):
        # Bounded as a pixel slice is, so a larger repack is refused rather than
        # written over whatever follows the map in the file.
        cfg = tilemap_config_for(entry, tilemap_preset_id(entry), registry, workspace)
    through = bound_tilemap(registry, workspace, entry, problems)
    if through is not None:
        entry.doc = chained_document(registry, entry, loaded, cfg, through)
        return
    tiles = _bound_tiles(registry, workspace, entry, problems, configure)
    entry.doc = tilemap_document(registry, workspace, entry, loaded, cfg, tiles)


def bound_tilemap(
    registry, workspace: Workspace, entry: Entry, problems: list[str]
) -> Document | None:  # noqa: ANN001
    """The tilemap ``entry`` draws through, loaded — or None if it draws art.

    What stops a chain is **depth, not format**: one hop is resolved, and the map
    at the end of it must reach art itself (``docs/design/tilemap-entry.md``
    §3.1). The gate is on the binding, checked before the source is loaded, which
    is what keeps two maps bound to each other from recursing.
    """
    source = entry.tile_source
    candidate = binding_target(workspace, source) if source is not None else None
    if candidate is None or not can_supply_tiles(workspace, entry, candidate):
        return None
    if candidate.content_kind is not ContentKind.TILEMAP:
        return None
    if candidate.doc is None:
        try:
            _load(candidate, registry, workspace, problems)
        except PipelineError as exc:
            problems.append(f"{candidate.name}: {exc}")
            return None
    doc = candidate.doc
    # A sprite object holds records at signed pixel offsets, not a grid to index.
    if doc is None or not doc.is_tilemap or doc.is_sprite:
        return None
    return doc


def _bound_tiles(registry, workspace, entry, problems, configure) -> BoundTiles:  # noqa: ANN001
    source = entry.tile_source
    fallback = entry.session.pixel_preset_id if entry.session else DEFAULT_PIXEL_PRESET
    if source is None or not source.is_bound:
        return no_tiles(registry, fallback)
    try:
        cfg = tile_source_config(workspace, entry, source, configure, fallback)
    except (PipelineError, KeyError) as exc:
        problems.append(f"{entry.name}: could not read the tiles it is bound to: {exc}")
        return no_tiles(registry, fallback)
    live = live_bound_tiles(workspace, source, cfg)
    if live is not None:
        return live
    try:
        px = pipeline.load_pixel_data(cfg, registry)
    except (PipelineError, KeyError) as exc:
        problems.append(f"{entry.name}: could not read the tiles it is bound to: {exc}")
        return no_tiles(registry, fallback)
    return BoundTiles(
        px.data, px.bytes_per_tile, px.tile_width, px.tile_height, px.ctx, cfg
    )


def _restore(entry: Entry, registry, workspace: Workspace, problems: list[str]) -> None:  # noqa: ANN001
    """The project restore: the stored view, then the stored palette.

    A palette that cannot be restored degrades to the default one with a
    problem noted — as the app does, including for an **Offset palette on a
    composite**, which has no file to read it from.
    """
    doc = entry.doc
    assert doc is not None and entry.session is not None
    if entry.pending_view is not None:
        doc.view = entry.pending_view
        entry.pending_view = None
    source, entry.pending_palette = entry.pending_palette, None
    if source is None:
        return
    try:
        palette = restored_palette(entry, source, registry, workspace)
    except Exception as exc:  # noqa: BLE001 — every failure here degrades, as in the app
        problems.append(
            f"{entry.name}: palette not restored, using the default palette ({exc})"
        )
        entry.session.palette_mode = PaletteMode.DEFAULT
        return
    if palette is not None:
        doc.palette, doc.palette_ctx = palette.palette, palette.ctx
        doc.palette_base_bytes, doc.palette_edits = palette.data, set()
        doc.palette_config = palette.config


class _Palette(NamedTuple):
    palette: Palette
    ctx: PipelineContext
    data: bytes
    config: PathwayConfig


def restored_palette(
    entry: Entry, source: PaletteSource, registry, workspace: Workspace
) -> _Palette | None:  # noqa: ANN001
    """The colours ``source`` names for ``entry``, read the way the app reads them.

    Custom colours as stated; a file through its PALETTE entry's own format and
    container where the project registered one; an emulator state through the
    console it detects; an offset in the owning file's coordinates. Raises where
    the app would degrade to the default palette.
    """
    session = entry.session
    assert session is not None
    if source.colors is not None:
        return _Palette(
            Palette(source.colors),
            PipelineContext(),
            b"",
            placeholder_palette_config(session.palette_preset_id),
        )
    if source.path is not None and not Path(source.path).exists():
        raise FileNotFoundError(source.path)
    if session.palette_mode is PaletteMode.FILE and source.path is not None:
        owner = workspace.find_palette(source.path)
        preset = (
            owner.palette_preset_id if owner is not None else None
        ) or session.palette_preset_id
        from celpix.plugins.detect import (
            detect_container,  # noqa: PLC0415 — file mode only
        )

        container = (
            owner.container_id
            if owner is not None and owner.container_id
            else detect_container(registry, source.path, kind=ContentKind.PALETTE)
        )
        cfg = replace(
            file_palette_config(source.path, source.offset, preset, container),
            write_enabled=False,
        )
    elif session.palette_mode is PaletteMode.ENTRY:
        cfg = entry_palette_config(entry, source, registry, workspace)
    elif session.palette_mode is PaletteMode.EMULATOR and source.path is not None:
        _fmt, cfg = emulator_palette_config(source.path, registry)
    elif source.path is not None:
        cfg = PathwayConfig(
            source=FileRef(source.path, offset=source.offset),
            interpret_preset_id=session.palette_preset_id,
        )
    else:
        ref, writable = offset_palette_source(
            registry, workspace, entry, source.offset, session.palette_preset_id
        )
        if ref is None:
            raise PipelineError(
                Stage.CONTAINER,
                Pathway.PALETTE,
                "not enough data at the palette offset",
                plugin=session.palette_preset_id,
            )
        cfg = PathwayConfig(
            source=ref,
            interpret_preset_id=session.palette_preset_id,
            write_enabled=writable,
        )
    loaded = pipeline.load_palette(cfg, registry)
    return _Palette(loaded.palette, loaded.ctx, loaded.data, cfg)
