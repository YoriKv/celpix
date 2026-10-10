"""An entry's :class:`~celpix.core.document.Document`, with or without a window.

An entry's document is assembled from several reads and a long list of
decisions: which cell format its file is read under, what that format declares
about itself, where its tiles come from, whether they come *through* another
map, how big a stamp is and how far apart its rows are, which palette row its
cells count from, whether a font groups its tiles into glyphs. They live here,
Qt-free, so a script — a verifier, a renderer, an agent checking a generated
project — gets the very document the app would have drawn rather than a second
implementation of it that drifts.

What a cell format declares, and how a map's indices read, are answered in
:mod:`celpix.project.declarations`; where a palette read from another entry, a
palette file or an emulator state comes from, in :mod:`celpix.project.palettes`.
This module binds, builds and loads, and re-exports both.

The window calls them (:mod:`celpix.ui.main_window.session`). Two things are
**injected** rather than decided, because they are the window's alone: how a
pixel pathway config is built (the window's settles a slice's parent first, so
a dirty parent's edits reach the read — ``docs/design/slices-and-parents.md``
§2) and what happens to a problem (the window alerts; a script collects).
:func:`load_document` wires both to their headless answers and is the one entry
point a script needs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from celpix.core.arrangement import BlockLayout
from celpix.core.capabilities import ContentKind
from celpix.core.cellchain import CellChain
from celpix.core.context import (
    KEY_PIXEL_PRESET,
    KEY_TILE_PALETTE_ROW_BASE,
    KEY_TILE_PALETTE_ROWS,
    KEY_TILEMAP_CELL_TILES,
    KEY_TILEMAP_PALETTE_ROW_BASE,
    PipelineContext,
)
from celpix.core.document import Document, ViewOptions
from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.core.font import FontAlphabet, font_alphabet
from celpix.core.palette import Palette
from celpix.core.paletteregions import PaletteRegion, PaletteRegions
from celpix.core.tilemap import (
    VRAM_ROW_STRIDE,
    Cell,
    Geometry,
    index_corner,
)
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import (
    DEFAULT_PALETTE_PRESET,
    DEFAULT_PIXEL_PRESET,
    FileRef,
)
from celpix.plugins.registry import Registry
from celpix.project.declarations import (
    CellStamp,
    IndexReading,
    binding_target,
    cell_stamp,
    chain_source_columns,
    chain_stamp_cells,
    chain_stamp_column_major,
    chain_width_is_stated,
    count_bases_in_units,
    declared_cell_column_stride,
    declared_cell_row_stride,
    document_index_reading,
    engine_counts_records,
    flag_break,
    format_addressing,
    index_reading,
    is_dense,
    is_fontmap,
    is_indirect,
    is_sprite,
    preset_declares,
    reads_record_keys,
    rebound_reading,
    stated_addressing,
    states_subsprite_size,
    tilemap_declares,
    tilemap_preset_id,
)
from celpix.project.palettes import (
    emulator_palette_config,
    entry_palette_config,
    entry_palette_source,
    fallback_palette,
    file_palette_config,
    offset_palette_files,
    offset_palette_source,
    offset_palette_space,
    palette_entry_target,
    palette_offset_owner,
    placeholder_palette_config,
    reordered_view,
    source_view_preset,
)
from celpix.project.workspace import (
    Entry,
    EntryKind,
    FileStages,
    PaletteMode,
    PaletteSource,
    TileSource,
    Workspace,
    backfill_slice_length,
    composite_config,
    composite_layout,
    pixel_config_for,
    record_composite_layout,
    swatch_session_for,
    tilemap_config_for,
)

if TYPE_CHECKING:
    pass

# The whole of what the window reaches here: this module's own bindings,
# builders and headless load, and the two it composes — what a cell format
# declares (:mod:`celpix.project.declarations`) and where a cross-entry palette
# is read from (:mod:`celpix.project.palettes`) — so a caller asks one module
# for everything about an entry's document.
__all__ = [
    "BoundTiles",
    "CellStamp",
    "IndexReading",
    "Loaded",
    "PixelConfig",
    "TileSourceUnavailable",
    "apply_pixel_preset_hint",
    "binding_target",
    "bound_tilemap",
    "can_supply_tiles",
    "cell_stamp",
    "chain_loops",
    "chain_source_columns",
    "chain_stamp_cells",
    "chain_stamp_column_major",
    "chain_width_is_stated",
    "chained_document",
    "count_bases_in_units",
    "declared_cell_column_stride",
    "declared_cell_row_stride",
    "document_index_reading",
    "draws_through_tilemap",
    "emulator_palette_config",
    "engine_counts_records",
    "entry_palette_config",
    "entry_palette_source",
    "fallback_palette",
    "file_palette_config",
    "fit_tile_base",
    "flag_break",
    "font_alphabet_for",
    "format_addressing",
    "glyph_layout_for",
    "index_reading",
    "is_dense",
    "is_fontmap",
    "is_indirect",
    "is_sprite",
    "live_bound_tiles",
    "load_document",
    "no_tiles",
    "offset_palette_files",
    "offset_palette_source",
    "offset_palette_space",
    "palette_entry_target",
    "palette_offset_owner",
    "pixel_document",
    "placeholder_palette_config",
    "preset_declares",
    "reads_record_keys",
    "rebound_reading",
    "reordered_view",
    "restored_palette",
    "row_base_for",
    "seed_tile_palette_rows",
    "source_view_preset",
    "stated_addressing",
    "states_subsprite_size",
    "tile_source_config",
    "tilemap_declares",
    "tilemap_document",
    "tilemap_preset_id",
]

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


# ---------------------------------------------------------------------------
# Palette rows, glyphs and the tile base


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
    return None if view is None else glyph_layout_of_view(view)


def glyph_layout_of_view(view: ViewOptions) -> BlockLayout | None:
    """How a font sheet viewed as ``view`` groups its tiles into glyphs.

    The rule :func:`glyph_layout_for` reads through a binding, and the alphabet
    editor reads off the sheet on screen: a glyph is one Pattern block at the
    sheet's Cols. None where the block is 1x1, one tile per glyph.
    """
    if view.block_columns <= 1 and view.block_rows <= 1:
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
    geometry: Geometry | None = None,
) -> None:
    """Shift a map onto a source it overflows, when its own indices say how.

    A map's cells and the entry supplying its tiles routinely number from
    different places. If the lowest index is the amount by which the highest
    overflows the source, the map is the same picture shifted and ``-min`` lands
    it. Deliberately narrow: only when the map does not fit as it stands and does
    once shifted — a condition an absolutely-indexed map never meets — and never
    over a base the user set. ``named`` is the codes a fontmap's font names past
    the end of its sheet (a terminator, a space with no glyph), which draw
    nothing by design and are not an overflow. ``geometry`` is the map's index
    addressing (:attr:`~celpix.core.document.Document.index_geometry`): the
    base counts what the index counts, so under ordinal addressing the shift is
    a number of metatiles, and whether a map fits is asked of the tiles each
    metatile starts at (:func:`~celpix.core.tilemap.index_corner`).
    """
    source = entry.tile_source
    if source is None or not source.is_bound or source.base_index or not cells:
        return
    count = len(tiles.data) // max(1, tiles.bytes_per_tile)
    if not count:
        return  # unreadable binding: nothing to fit against
    # Filtered on the tile each cell's unit starts at: an ordinal short of the
    # bank's length can still start past its end. Only the membership test
    # reads the raw index, since `named` holds codes.
    indices = [
        cell.index
        for cell in cells
        if index_corner(cell.index, geometry) < count or cell.index not in named
    ]
    if not indices:
        return
    low, high = min(indices), max(indices)
    # A cell covering several tiles reaches past its own index, so the span has
    # to allow for what the widest of them draws.
    across, down = max(1, cell_tiles[0]), max(1, cell_tiles[1])
    extra = (down - 1) * VRAM_ROW_STRIDE + (across - 1)
    reach = index_corner(high, geometry) + extra
    shifted = index_corner(high, geometry, -low) + extra
    if low and reach >= count and shifted < count:
        entry.tile_source = replace(source, base_index=-low)


# ---------------------------------------------------------------------------
# Where a map's tiles come from


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
    and a tilemap at any depth, so long as its chain does not come back round to
    ``entry`` or loop on itself (:func:`chain_loops`). Never the entry itself and
    never a bookmark."""
    if candidate is entry or candidate.kind is EntryKind.BOOKMARK:
        return False
    if candidate.content_kind is ContentKind.PIXELS:
        return True
    if candidate.content_kind is not ContentKind.TILEMAP:
        return False
    return not chain_loops(workspace, entry, candidate)


def chain_loops(workspace: Workspace, entry: Entry, candidate: Entry) -> bool:
    """Whether following the bindings from ``candidate`` ever revisits a map —
    ``entry`` itself, or one already passed.

    The one thing that stops a chain, whatever its depth: every other chain
    ends, at art or at a map that draws blank (unbound, or bound to something
    closed), and resolves as far as it goes (``docs/design/tilemap-entry.md``
    §3.1). Walked on the **bindings**, not on loaded documents, which is what
    keeps two maps bound to each other from recursing during a load — both
    fail here before either loads the other.
    """
    seen = {id(entry)}
    at: Entry | None = candidate
    while at is not None and at.content_kind is ContentKind.TILEMAP:
        if id(at) in seen:
            return True
        seen.add(id(at))
        source = at.tile_source
        at = binding_target(workspace, source) if source is not None else None
    return False


def font_alphabet_for(
    registry: Registry, workspace: Workspace, entry: Entry, cell_bytes: int
) -> FontAlphabet | None:
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
    return font_alphabet(
        font.font_chars if font is not None else "",
        font.font_codes if font is not None else (),
        code_digits=max(1, cell_bytes) * 2,
        base=font.font_base if font is not None else 0,
        flag_break=flag_break(registry, entry),
    )


def no_tiles(registry: Registry, preset_id: str) -> BoundTiles:
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


class TileSourceUnavailable(Exception):
    """The entry a map's tiles are bound to cannot be read as art
    (:func:`tile_source_config`): it is not open, it is a map the chain refused,
    or it cannot supply tiles.

    Its own exception rather than a ``KeyError``, which is what a registry miss
    raises when a format id is not installed: the two call for different fixes
    — re-point the binding, or install the format — and a reader told only the
    type would have to guess which one it is holding. The message is a whole
    sentence naming the entry.
    """


def tile_source_config(
    workspace: Workspace,
    entry: Entry,
    source: TileSource,
    pixel_config: PixelConfig,
    fallback_preset: str,
) -> PathwayConfig:
    """The pathway that reads the tiles ``source`` points at.

    Resolved through the bound entry's own config, so the tiles are read exactly
    as that entry reads them — its container, reshape and codec. Only **art** is
    read this way. A bound tilemap is drawn through by the chain resolution
    (:func:`bound_tilemap`), and reaching here means that resolution refused it
    — a chain that loops, a source that did not open, a sprite object: its file
    holds *coordinates*, and a pixel codec would draw them as art and give a
    pen stroke on the map a bank to land in. Read, but **never written**: the
    tiles belong to the bound entry (``docs/design/tilemap-entry.md`` §3).
    """
    bound = binding_target(workspace, source)
    if bound is None:
        name = source.entry.name if source.entry is not None else "nothing"
        raise TileSourceUnavailable(f"the tiles are bound to {name}, which is not open")
    if bound.content_kind is ContentKind.TILEMAP:
        if chain_loops(workspace, entry, bound):
            raise TileSourceUnavailable(f"{bound.name}'s chain loops back on itself")
        raise TileSourceUnavailable(
            f"{bound.name} is a tilemap that could not be drawn through"
        )
    if not can_supply_tiles(workspace, entry, bound):
        raise TileSourceUnavailable(f"{bound.name} cannot supply tiles")
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
    entry: Entry,
    px: pipeline.PixelData,
    cfg: PathwayConfig,
    registry: Registry,
    pixel_config: PixelConfig,
) -> tuple[pipeline.PixelData, PathwayConfig]:
    """Adopt the format the container says its payload is in, if it says.

    A tile bank that records its own bit depth should not need one guessed: 2bpp,
    4bpp and 8bpp all decode into something that *looks* like graphics. Only the
    geometry is re-derived, and only on a **fresh** entry — once a project has
    stored a format, or the user picked one, that is the answer. A file opened
    with the settings of the entry on screen is still fresh: those were handed
    on, not stated for it (:attr:`~celpix.project.entry.EntrySession.inherited`).
    """
    wanted = str(px.ctx.get(KEY_PIXEL_PRESET, "") or "")
    session = entry.session
    if not wanted or session is None:
        return px, cfg
    if entry.pending_view is not None and not session.inherited:
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


def pixel_document(
    entry: Entry, px: pipeline.PixelData, cfg: PathwayConfig
) -> Document:
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
        pixel_base_bytes=px.data,
        # A bank states where its own rows count from, and its per-tile table
        # counts from exactly there. Nothing in a preset can say it — it is a fact
        # about this file — so the declared answer is 0 and the header the only
        # other voice.
        palette_row_base=row_base_for(
            entry, 0, stated=False, bank=px.ctx.get(KEY_TILE_PALETTE_ROW_BASE)
        ),
    )


def chained_document(
    registry: Registry,
    entry: Entry,
    loaded: pipeline.TilemapData,
    cfg: PathwayConfig,
    through: Document,
) -> Document:
    """A map whose cells are coordinates into ``through``'s cells.

    ``through`` may be chained itself, to any depth: its own chain rides along
    (:attr:`~celpix.core.cellchain.CellChain.through`), and everything else taken
    from it — the art, the cell size, the tile base — is already the end of the
    chain's, since ``through`` took it from its own source the same way. The
    drawing geometry is the source map's, because what is drawn is its cells;
    this entry's own record size is what the hex dump shows. The pixel config
    stays read-only: the art belongs to the entry at the end of the chain, and
    a restamp must never reach it.

    This entry's **own** cell size is not a geometry for the art — its cells are
    coordinates, and what they draw is the source's cells — so a size over one
    unit it states becomes the stamp (:func:`cell_stamp`), and the source's cell
    size is the one the tiles are drawn in.
    """
    assert entry.session is not None
    stamp = cell_stamp(registry, entry, loaded.ctx)
    stamp_cells = chain_stamp_cells(registry, entry, through, stamp)
    reading = index_reading(registry, entry, through=through, stamp=stamp_cells)
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
            stamp=stamp_cells,
            source_columns=chain_source_columns(through),
            # A stamp stated by the cell size is one entry drawing the whole
            # stamp, which is what dense means.
            dense=is_dense(registry, entry) or stamp is not None,
            stamp_column_major=chain_stamp_column_major(through),
            base=entry.tile_source.base_index if entry.tile_source else 0,
            through=through.chain,
            geometry=reading.geometry,
        ),
        addressing_refusal=reading.refusal,
        tilemap_config=cfg,
        tilemap_ctx=loaded.ctx,
        tilemap_data=loaded.data,
        tilemap_base_bytes=loaded.disk,
        cell_bytes=loaded.cell_bytes,
        cell_tiles=through.cell_tiles,
        cell_row_stride=through.cell_row_stride,
        cell_column_stride=through.cell_column_stride,
        tile_base_index=through.tile_base_index,
        index_mask=through.index_mask,
        # The resolved cells are the last source's, numbered as it numbers them.
        index_geometry=through.index_geometry,
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
        # This entry's own too, and for the same reason: what it settles is the
        # referrer's cells, which are what a write of this entry encodes. A
        # metatile-id map is exactly this shape — a chain whose byte derives its
        # palette row from the record it names.
        cell_settler=loaded.settler,
        # The byte widths are this entry's own as well: they measure the cells
        # this entry's codec writes, not the ones they resolve to.
        cell_widths=loaded.widths,
        line_bytes=loaded.line_bytes,
        column_major=loaded.column_major,
    )


def tilemap_document(
    registry: Registry,
    workspace: Workspace,
    entry: Entry,
    loaded: pipeline.TilemapData,
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
    # The stride is the fixed-offset way of finding a cell's other tiles and a
    # glyph layout the general one, so they are never both set.
    strided = glyph_layout is None and cell_tiles != (1, 1)
    row_stride = declared_cell_row_stride(registry, entry) if strided else 0
    column_stride = declared_cell_column_stride(registry, entry) if strided else 0
    # A glyph code already counts whole glyphs and a subsprite draws its own run
    # of tiles, so neither has a unit for an ordinal to count.
    geometry = refusal = None
    if glyph_layout is None and loaded.frames is None:
        reading = index_reading(
            registry,
            entry,
            cell_tiles=cell_tiles,
            cell_row_stride=row_stride,
            cell_column_stride=column_stride,
        )
        geometry, refusal = reading.geometry, reading.refusal
    if glyph_layout is None:
        font = entry.tile_source.entry if fontmap and entry.tile_source else None
        named = frozenset(g.code for g in font.font_codes) if font else frozenset()
        fit_tile_base(entry, loaded.cells, tiles, cell_tiles, named, geometry)
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
        tilemap_base_bytes=loaded.disk,
        cell_bytes=loaded.cell_bytes,
        cell_tiles=cell_tiles,
        cell_row_stride=row_stride,
        cell_column_stride=column_stride,
        glyph_layout=glyph_layout,
        index_mask=loaded.index_mask,
        index_geometry=geometry,
        addressing_refusal=refusal,
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
        cell_settler=loaded.settler,
        cell_widths=loaded.widths,
        line_bytes=loaded.line_bytes,
        column_major=loaded.column_major,
        text_layout=fontmap,
        font_alphabet=font_alphabet_for(registry, workspace, entry, loaded.cell_bytes),
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


def load_document(entry: Entry, registry: Registry, workspace: Workspace) -> Loaded:
    """``entry``'s document as the app builds it, with no window.

    Every decision is the one the app makes (the functions above are what its
    session code calls), and so are the reads — through ``pixel_config_for`` and
    ``tilemap_config_for`` with the workspace, a composite through
    ``composite_layout``, a chained map through its source's own load. The
    restored view and palette are applied the way a project restore applies
    them, so the document carries the entry's columns, palette row and colours.
    What differs is only what has no headless meaning: nothing is settled (there
    are no unsaved edits), and a problem is collected instead of alerted.

    A project from before format version 7 is opened with one more step first,
    which needs the whole project rather than an entry:
    :func:`count_bases_in_units`, as the window does, or a map whose index
    counts records draws its base as that many records rather than cells.

    Sets ``entry.doc``, since that is where a bound map finds its source's live
    buffer, and returns it with the problems met on the way. Raises
    :class:`~celpix.core.errors.PipelineError` where the app would have refused
    to open the entry at all.
    """
    problems: list[str] = []
    _load(entry, registry, workspace, problems)
    assert entry.doc is not None
    return Loaded(entry.doc, problems)


def _pixel_config(registry: Registry, workspace: Workspace) -> PixelConfig:
    # ``pixel_config_for`` already dispatches a composite to its own builder.
    return lambda entry, preset_id: pixel_config_for(
        entry, preset_id, registry, workspace
    )


def _load(
    entry: Entry, registry: Registry, workspace: Workspace, problems: list[str]
) -> None:
    if entry.doc is not None:
        return
    colors = None
    if entry.kind is EntryKind.PALETTE:
        colors = _palette_entry_colors(entry, registry, problems)
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
        if colors is not None:
            # The palette half of a palette file's document: the same bytes
            # decoded as colours, which is what a File-mode graphic mirrors and
            # what an edit writes back through — so the config stays writable.
            loaded, palette_cfg = colors
            entry.doc.palette = loaded.palette
            entry.doc.palette_ctx = loaded.ctx
            entry.doc.palette_config = palette_cfg
            entry.doc.palette_base_bytes = loaded.data
        if entry.doc.view.palette_regions.is_empty():
            seed_tile_palette_rows(entry.doc, px.ctx.get(KEY_TILE_PALETTE_ROWS, b""))


def _palette_entry_colors(
    entry: Entry, registry: Registry, problems: list[str]
) -> tuple[pipeline.PaletteData, PathwayConfig] | None:
    """A PALETTE entry's swatch session, and its bytes decoded as colours.

    A palette file opens as swatches in its own colour format, on a session
    built for that where the project stored none, and carries both halves of
    its bytes: the swatches :func:`_load` reads as pixels, and the colours
    returned here as the palette half — the app's loader does the same
    (``docs/design/palette-editing.md`` §2). None, with a problem noted, where
    the colours will not decode; the document then keeps the default palette,
    where the app would open on its error palette for the user to correct.
    """
    session = swatch_session_for(entry, registry, DEFAULT_PALETTE_PRESET)
    cfg = file_palette_config(
        entry.path,
        0,
        entry.palette_preset_id or DEFAULT_PALETTE_PRESET,
        entry.file_stages,
        registry,
    )
    try:
        loaded = pipeline.load_palette(cfg, registry)
    except PipelineError as exc:
        problems.append(f"{entry.name}: colors not decoded ({exc})")
        return None
    # The dock's format on a palette file is the file's own, the one the colours
    # just decoded with — and so is the swatch view's, which reads the same
    # colour words as pixels.
    session.palette_preset_id = cfg.interpret_preset_id
    session.palette_view_preset_id = cfg.interpret_preset_id
    return loaded, cfg


def _load_tilemap(entry, registry: Registry, workspace, problems, configure) -> None:  # noqa: ANN001
    cfg = tilemap_config_for(entry, tilemap_preset_id(entry), registry, workspace)
    loaded = pipeline.load_tilemap_data(cfg, registry, size_pair=entry.sprite_size_pair)
    if backfill_slice_length(entry, loaded.ctx):
        # Bounded as a pixel slice is, so a larger repack is refused rather than
        # written over whatever follows the map in the file.
        cfg = tilemap_config_for(entry, tilemap_preset_id(entry), registry, workspace)
    through = bound_tilemap(registry, workspace, entry, problems)
    if through is not None:
        entry.doc = chained_document(registry, entry, loaded, cfg, through)
    else:
        tiles = _bound_tiles(registry, workspace, entry, problems, configure)
        entry.doc = tilemap_document(registry, workspace, entry, loaded, cfg, tiles)
    # The app's status line, collected: the picture is not the one the formats
    # asked for, and a script should hear why as a user does. Lower-cased after
    # the name, like every other problem.
    for note in entry.doc.refusal_notes:
        problems.append(f"{entry.name}: {note[:1].lower()}{note[1:]}")


def bound_tilemap(
    registry: Registry, workspace: Workspace, entry: Entry, problems: list[str]
) -> Document | None:
    """The tilemap ``entry`` draws through, loaded — or None if it draws art.

    A chain resolves at any depth; what stops one is a **loop**
    (:func:`chain_loops`, ``docs/design/tilemap-entry.md`` §3.1). The gate is on
    the binding, checked before the source is loaded, which is what keeps two
    maps bound to each other from recursing. Loading the source is the ordinary
    load, so a chained source settles its own chain first and comes back with
    it for :func:`chained_document` to carry.
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


def _bound_tiles(
    registry: Registry, workspace, entry, problems, configure
) -> BoundTiles:  # noqa: ANN001
    source = entry.tile_source
    fallback = entry.session.pixel_preset_id if entry.session else DEFAULT_PIXEL_PRESET
    if source is None or not source.is_bound:
        return no_tiles(registry, fallback)
    try:
        cfg = tile_source_config(workspace, entry, source, configure, fallback)
    except (PipelineError, KeyError, TileSourceUnavailable) as exc:
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


def _restore(
    entry: Entry, registry: Registry, workspace: Workspace, problems: list[str]
) -> None:
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
    entry: Entry, source: PaletteSource, registry: Registry, workspace: Workspace
) -> _Palette | None:
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

        # The registered palette's own stages, where there is one: a reshape
        # on the palette file reorders the colours every graphic reads from it.
        stages = (
            owner.file_stages
            if owner is not None
            else FileStages(
                detect_container(registry, source.path, kind=ContentKind.PALETTE)
            )
        )
        # Read whole, as the app reads it (the graphic mirrors the palette
        # entry's own colours): a stored offset plays no part, since a run of a
        # palette file is a slice of it applied in Entry mode. Reading from the
        # offset instead would also run the file's reshape over a different
        # region than the palette entry's, and a position- or length-dependent
        # reshape would hand back colours the entry never shows.
        cfg = replace(
            file_palette_config(source.path, 0, preset, stages, registry),
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
