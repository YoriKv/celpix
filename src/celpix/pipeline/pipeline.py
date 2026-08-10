"""The strictly linear pipeline: run both pathways for load and for save.

Load runs each pathway forward — container.read -> reshape -> decompress ->
interpret — and converges the results into a :class:`Document`. Save mirrors
it — interpret.encode -> compress -> unshape -> container.write — per pathway,
with the palette's save optional. Any stage that cannot proceed raises
:class:`PipelineError`, which halts the pipeline and names the stage + direction
+ pathway + reason; nothing partial is written (``docs/design/overview.md`` §2).

Running the stages is all this module does. The three questions that are asked
*around* a pipeline run rather than by one are its neighbours, and are
re-exported here so that a caller keeps one import for the whole of it:

- :mod:`celpix.pipeline.render` — decoded data laid out as a picture: tile
  windows, tilemaps, tile source sheets and sprite objects.
- :mod:`celpix.pipeline.inspection` — what one container made of one file, read on
  its own and reported rather than raised.
- :mod:`celpix.pipeline.metrics` — scalar queries put to a resolved codec: entry
  sizes, capacities, bit depth, what a format can store.

What all four share — executing one stage, acquiring a source, resolving tile
geometry — is :mod:`celpix.pipeline._stage`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple

from celpix.core.context import (
    KEY_SOURCE_FILES,
    KEY_SOURCE_PATH,
    KEY_TILEMAP_PALETTE_ROW_BASE,
    PipelineContext,
)
from celpix.core.document import Document
from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.core.font import Alphabet, Glyph, GlyphRole, glyphs_from_spec
from celpix.core.notices import warn
from celpix.core.palette import Palette
from celpix.core.sprite import DEFAULT_SUBSPRITE_TILES, Frame, frame_bounds
from celpix.core.tilemap import Cell
from celpix.pipeline._stage import (
    _acquire,
    _pixel_geometry,
    _run,
    bitmap_params,
    tile_params,
)
from celpix.pipeline.inspection import ContainerReport, inspect_container
from celpix.pipeline.metrics import (
    palette_entries_per_unit,
    palette_entry_capacity,
    palette_entry_size,
    palette_has_alpha,
    palette_read_bytes,
    pixel_bpp,
    pixel_is_direct_color,
    quantize_color,
    quantize_palette,
)
from celpix.pipeline.pathway import PathwayConfig
from celpix.pipeline.render import (
    SPRITE_SHEET_PIXELS,
    SpriteHit,
    SpriteSheet,
    TilemapImage,
    TileRegion,
    TileSheet,
    _check_sprite_extent,
    compose_tiles,
    decode_and_compose,
    decode_tiles,
    decode_window,
    drawn_palette_row,
    encode_tiles,
    expand_cells,
    hidden_rects,
    patch_tile_bank,
    sprite_hit,
    sprite_hits,
    sprite_hoist,
    sprite_image,
    sprite_sheet,
    subsprite_at,
    subsprites_at,
    tile_bank,
    tile_region,
    tile_source_ids,
    tile_source_image,
    tile_source_span,
    tilemap_image,
    tilemap_tiles,
    tiles_per_stripe,
)
from celpix.plugins.base import (
    NO_COMPRESSION,
    NO_RESHAPE,
    RAW_CONTAINER,
    AlphabetPlugin,
    ColorCodecPlugin,
    CompressionPlugin,
    ContainerPlugin,
    FileRef,
    ReshapePlugin,
    TilemapCodecPlugin,
    WriteTarget,
)
from celpix.plugins.registry import Registry

# The pipeline's whole public surface, in one list because most of it is a
# re-export: a caller imports this module and reaches the render, inspect and
# metrics entry points through it without having to know which one owns what.
__all__ = [
    "SPRITE_SHEET_PIXELS",
    "ContainerReport",
    "PaletteData",
    "PixelData",
    "ScanResult",
    "SpriteHit",
    "SpriteSheet",
    "TileRegion",
    "TileSheet",
    "TilemapData",
    "TilemapImage",
    "bitmap_params",
    "compose_tiles",
    "decode_and_compose",
    "decode_tiles",
    "decode_window",
    "drawn_palette_row",
    "encode_cells",
    "encode_tiles",
    "encoded_pixel_bytes",
    "expand_cells",
    "export_palette",
    "find_next_structure",
    "hidden_rects",
    "inspect_container",
    "load",
    "load_alphabet",
    "load_palette",
    "load_pixel_data",
    "load_tilemap_data",
    "palette_entries_per_unit",
    "palette_entry_capacity",
    "palette_entry_size",
    "palette_has_alpha",
    "palette_read_bytes",
    "patch_tile_bank",
    "pixel_bpp",
    "pixel_is_direct_color",
    "quantize_color",
    "quantize_palette",
    "read_region",
    "reinterpret_pixel_data",
    "save",
    "spliced_palette_bytes",
    "sprite_hit",
    "sprite_hits",
    "sprite_hoist",
    "sprite_image",
    "sprite_sheet",
    "subsprite_at",
    "subsprites_at",
    "tile_bank",
    "tile_params",
    "tile_region",
    "tile_source_ids",
    "tile_source_image",
    "tile_source_span",
    "tilemap_image",
    "tilemap_tiles",
    "tiles_per_stripe",
]


@dataclass(frozen=True)
class ScanResult:
    """Where a forward structure scan ended (:func:`find_next_structure`).

    ``found`` is the hit offset or ``None``; ``end`` is where the scan stopped
    (where the caller lands when there was no hit — ``len(data)`` once the whole
    buffer is exhausted); ``stopped`` is True when the caller aborted the scan
    via its tick callback rather than reaching the end.
    """

    found: int | None
    end: int
    stopped: bool


def find_next_structure(
    data: bytes,
    plugin: CompressionPlugin,
    probe_bytes: int,
    start: int,
    *,
    progress_every: int = 64,
    on_tick: Callable[[int], bool] | None = None,
) -> ScanResult:
    """The first offset ≥ ``start`` where ``plugin`` decodes a complete structure.

    Walks ``data`` one byte at a time, trying a strict decompress of the
    ``probe_bytes`` compressed bytes at each offset; a non-empty result is a hit. This
    is the Qt-free core of the toolbar's *Scan* — a hit is a *complete*, non-empty
    structure, since a best-effort partial decode "succeeds" on almost any bytes
    (so non-self-delimiting schemes are effectively unscannable). Every
    ``progress_every`` bytes ``on_tick(pos)`` is called if given; returning True
    aborts the scan (the UI pumps its event loop and reports a Stop there).
    """
    pos = start
    n = len(data)
    while pos < n:
        try:
            if plugin.decompress(data[pos : pos + probe_bytes], PipelineContext()):
                return ScanResult(pos, pos, False)
        except Exception:  # noqa: BLE001 — not a structure here; keep walking
            pass
        pos += 1
        if on_tick is not None and pos % progress_every == 0 and on_tick(pos):
            return ScanResult(None, pos, True)
    return ScanResult(None, pos, False)


class PixelData(NamedTuple):
    """The pixel pathway loaded up to (but not through) decode.

    The raw decompressed bytes plus the codec geometry needed to decode them a
    window at a time — see :func:`load_pixel_data`.
    """

    data: bytes
    bytes_per_tile: int
    tile_width: int
    tile_height: int
    ctx: PipelineContext


def load_pixel_data(
    cfg: PathwayConfig, reg: Registry, bitmap_width: int = 0
) -> PixelData:
    """Run the pixel pathway forward through Decompress, *without* decoding.

    Returns the raw decompressed bytes plus the codec's atomic geometry, so the view
    can decode only the visible window on demand (:func:`decode_window`) rather than
    the whole file. Data whose length isn't a whole number of tiles is fine — the
    trailing partial tile is zero-padded at decode time (``Document.window_bytes``).

    ``bitmap_width`` is the view's wide-bitmap width, which re-cuts the geometry
    (see :func:`bitmap_params`); 0 leaves the codec's own tiles alone.
    """
    ctx = PipelineContext()
    data = _read_reshape_decompress(cfg, ctx, reg, Pathway.PIXEL)
    return PixelData(data, *_pixel_geometry(cfg, reg, bitmap_width), ctx)


def reinterpret_pixel_data(
    data: bytes,
    ctx: PipelineContext,
    cfg: PathwayConfig,
    reg: Registry,
    bitmap_width: int = 0,
) -> PixelData:
    """Already-loaded bytes under ``cfg``'s Interpret preset — nothing re-read.

    Only the codec's geometry depends on the interpret preset; which bytes there
    are comes out of Read + Decompress, which this leaves alone. That is what
    makes switching formats non-destructive: unsaved edits live *in* these bytes,
    and re-running the pathway would pull the file's own bytes back over them.
    Raises the same :class:`PipelineError` a load would for an unusable preset.
    """
    return PixelData(data, *_pixel_geometry(cfg, reg, bitmap_width), ctx)


class PaletteData(NamedTuple):
    """A loaded palette plus the bytes it came from.

    ``data`` is kept so a later save can splice edited entries into it instead
    of re-encoding the whole palette, which would not round-trip
    (see :func:`_save_palette`).
    """

    palette: Palette
    ctx: PipelineContext
    data: bytes


def load_palette(cfg: PathwayConfig, reg: Registry) -> PaletteData:
    """Run the palette pathway forward: Read -> Decompress -> decode to a Palette."""
    ctx = PipelineContext()
    data = _read_reshape_decompress(cfg, ctx, reg, Pathway.PALETTE)
    engine, preset = reg.engine_for(cfg.interpret_preset_id, ColorCodecPlugin)
    colors = _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.decode(data, preset.params, ctx),
        plugin=preset.id,
    )
    return PaletteData(colors, ctx, data)


class TilemapData(NamedTuple):
    """The tilemap pathway loaded up to (but not through) grid layout.

    ``cells`` are in the file's own order; ``data`` is kept for the same reason
    the palette keeps its bytes — an edit is spliced into the buffer that was
    read rather than re-encoded from a partial grid. ``cell_tiles`` is how many
    tiles one cell covers, which only the codec knows.

    ``frames`` is set only by a codec whose cells are **subsprites**, whose
    pixel offsets no grid can express (:mod:`celpix.core.sprite`). None for every
    ordinary tilemap, which is drawn as the grid its cells already are.

    ``index_mask`` is the ``index`` field's own width, so a multi-tile cell's
    neighbours wrap inside the field rather than running past the end
    (:attr:`~celpix.core.document.Document.index_mask`) — the codec's
    :meth:`~celpix.plugins.base.TilemapCodecPlugin.index_limit`, since the mask
    *is* the highest value the field holds. 0 for a format that does not say.

    ``palette_row_base`` is the palette row a cell's row 0 means — 8 for a sprite,
    whose 3-bit field counts from the upper half of CGRAM
    (:attr:`~celpix.core.document.Document.palette_row_base`).

    ``palette_rows`` is whether the *format* gives a cell a palette row to name —
    the format's word, not this file's, so a screen whose cells all happen to sit
    on row 0 still reports True. What it gates is whether the view's subpalette
    applies at all (``docs/design/tilemap-entry.md`` §8).
    """

    cells: list[Cell]
    cell_bytes: int
    cell_tiles: tuple[int, int]
    ctx: PipelineContext
    data: bytes
    frames: list[Frame] | None = None
    size_pair: tuple[int, int] = DEFAULT_SUBSPRITE_TILES
    palette_rows: bool = True
    index_mask: int = 0
    palette_row_base: int = 0


def load_tilemap_data(
    cfg: PathwayConfig, reg: Registry, live: bytes | None = None
) -> TilemapData:
    """Run the tilemap pathway forward: Read -> Decompress -> decode to cells.

    Stops at a flat list rather than a grid. A tilemap file rarely states its own
    width — a screen is four fixed 32x32 pages, a stamp layout's 128 columns had
    to be recovered from the data — so the layout is the view's to choose and
    change without re-reading anything
    (``docs/graphics-formats-reference/scgcad-formats.md`` §4).

    ``live`` decodes an **unsaved** payload in place of the one on disk, for a
    caller re-reading an entry it has edits for: the cells are the document, and
    a read that went to the file would take them back out again. The file is
    still read, because only the container can answer what its header states —
    the width, the cell and stamp sizes, the byte order — and an edit to the
    cells has not moved any of that. Spliced on the rule the edit itself follows
    (:meth:`~celpix.ui.main_window.tilemap_edit.TilemapEditMixin._reencode_cells`),
    so whatever sits past the last cell stays as the file has it.
    """
    ctx = PipelineContext()
    data = _read_reshape_decompress(cfg, ctx, reg, Pathway.TILEMAP)
    if live is not None:
        data = live + data[len(live) :]
    engine, preset = reg.engine_for(cfg.interpret_preset_id, TilemapCodecPlugin)
    cells = _run(
        Stage.INTERPRET_TILEMAP,
        Pathway.TILEMAP,
        lambda: engine.decode(data, preset.params, ctx),
        plugin=preset.id,
    )
    cell_bytes = _run(
        Stage.INTERPRET_TILEMAP,
        Pathway.TILEMAP,
        lambda: engine.bytes_per_cell(preset.params),
        plugin=preset.id,
    )
    if cell_bytes <= 0:
        raise PipelineError(
            Stage.INTERPRET_TILEMAP,
            Pathway.TILEMAP,
            f"bytes per cell ({cell_bytes}) is not positive",
        )
    tiles = _run(
        Stage.INTERPRET_TILEMAP,
        Pathway.TILEMAP,
        lambda: engine.cell_tiles(preset.params),
        plugin=preset.id,
    )
    # The optional half of the protocol, and the only one an engine may lack: a
    # format whose cells are subsprites groups them into frames itself, because
    # what they *mean* — which frame, at what offset, how big — is the same
    # knowledge that decoded them (:mod:`celpix.plugins.builtins.object_codec`).
    # It is handed ``ctx`` as well as the cells, because where the frames are cut
    # is not always in the preset: a format that counts each frame keeps those
    # counts between its records, so the container is the only thing that can have
    # read them (:data:`~celpix.core.context.KEY_TILEMAP_FRAME_SIZES`).
    frames = None
    size_pair = DEFAULT_SUBSPRITE_TILES
    if hasattr(engine, "frames"):
        frames = _run(
            Stage.INTERPRET_TILEMAP,
            Pathway.TILEMAP,
            lambda: engine.frames(cells, preset.params, ctx),
            plugin=preset.id,
        )
        # Optional even among the sprite formats: it is the setting a record
        # holding a size *bit* is resolved against, and a format whose record
        # states the rectangle outright has nothing to resolve. Its subsprites
        # arrive already sized, so the default here is only what the bar shows.
        if hasattr(engine, "size_pair"):
            size_pair = _run(
                Stage.INTERPRET_TILEMAP,
                Pathway.TILEMAP,
                lambda: engine.size_pair(preset.params),
                plugin=preset.id,
            )
        # Bounded here as well as at the allocation itself, because *this* is the
        # call the UI can take back: a read that fails leaves the binding on what
        # it was and says why, where a render that fails has already replaced the
        # document it was drawing (``docs/design/tilemap-entry.md`` §6). The box
        # is measured over nominal 8px tiles — the real ones come from whatever
        # entry the map is bound to, and are not known until the document exists —
        # which is close enough for a limit the offsets, not the tiles, blow past.
        box = frame_bounds(frames)
        _check_sprite_extent(
            len(frames) * box[2] * box[3],
            f"{len(frames)} frames of subsprites, {box[2]}x{box[3]} pixels each",
        )
    # The other optional half: whether the format has a palette row for a cell to
    # name. **True when the engine does not answer**, which is the safe
    # direction — a format that does carry rows and stayed quiet must not have a
    # view-wide row added on top of the ones its cells already state.
    rows = True
    if hasattr(engine, "has_palette_rows"):
        rows = bool(
            _run(
                Stage.INTERPRET_TILEMAP,
                Pathway.TILEMAP,
                lambda: engine.has_palette_rows(preset.params),
                plugin=preset.id,
            )
        )
    # The index field's own width, straight off the codec
    # (:meth:`~celpix.plugins.base.TilemapCodecPlugin.index_limit`) rather than a
    # second parameter that could disagree with the field table -- the mask *is*
    # the highest value the field holds. Probed like the other optional methods:
    # a format that cannot say leaves its references unbounded.
    mask = 0
    ask = getattr(engine, "index_limit", None)
    if ask is not None:
        try:
            top = ask(preset.params)
        except Exception:  # noqa: BLE001 — a probe must not fail the load
            top = None
        mask = top if top and top > 0 else 0
    # Where the format's rows count from. Usually a plain preset parameter — a
    # sprite's 3-bit field counts from CGRAM row 8 whatever file it came from,
    # which is a fact about the console rather than about this map.
    #
    # But a *file* can say otherwise, and two of this family do: a screen and a
    # panel each carry a colour-half and a colour-cell byte that move the base
    # per file (`scgcad-formats.md` §3.3). The container reads those and publishes
    # the answer, so it wins over the preset where it is present — 94% of panels
    # state base 1, and taking the preset's 0 draws every one of them through the
    # wrong sixteen colours.
    stated = ctx.get(KEY_TILEMAP_PALETTE_ROW_BASE)
    row_base = (
        int(stated)
        if stated is not None
        else int(preset.params.get("palette_row_base", 0) or 0)
    )
    return TilemapData(
        cells, cell_bytes, tiles, ctx, data, frames, size_pair, rows, mask, row_base
    )


def load_alphabet(
    preset_id: str | None,
    reg: Registry,
    ctx: PipelineContext,
    *,
    controls: Iterable[dict] = (),
    code_digits: int = 2,
    base: int = 0,
    flag_break: bool = False,
) -> Alphabet | None:
    """The lookup a fontmap's codes are read through, from its two halves.

    ``preset_id`` is the **font's** alphabet, picked on the entry that supplies
    the tiles, and ``ctx`` is that entry's own context — so a container that
    computed a mapping no table could hold reaches the engine that way
    (:data:`~celpix.core.context.KEY_ALPHABET`). ``controls`` is the **fontmap's**
    own, off its cell format's params, and it is laid over the font's: where a
    stream reserves a code the font also spells, the stream wins, because the
    font's table was authored against tiles and knows nothing about which codes a
    given stream has taken (``docs/design/fontmap-entry.md`` §3).

    ``base`` shifts the **font's** half and not the stream's
    (:meth:`~celpix.core.font.Alphabet.shifted`). The two are stated against
    different things and only one of them can be off by an origin: a table
    numbers the glyphs from wherever its author started reading the sheet, while
    a format's controls name the codes the stream actually holds. Shifting those
    too would move a terminator the user read straight out of the file.

    ``flag_break`` is the third thing the **fontmap's** cell format states, beside
    its controls: that lines end on a bit the cell carries rather than on a code
    (:attr:`~celpix.core.font.Alphabet.flag_break`). It travels with the controls
    for the same reason they do — it is punctuation, and punctuation is the
    stream's.

    Returns **None** rather than an empty alphabet where neither half says
    anything: "no lookup picked" and "a lookup that maps nothing" are different
    states, and only the first is worth telling the user about.

    Never raises. An alphabet is a *reading* of cells that are already decoded,
    so a preset naming an engine this build has not got must not take the
    document with it — the map still draws, and its text reads as hex until the
    picker is put right. That is the opposite of the hard-stop the byte stages
    take, and deliberately: those stages have no output at all without their
    plugin, and this one does.
    """
    glyphs: list[Glyph] = []
    if preset_id:
        try:
            engine, preset = reg.engine_for(preset_id, AlphabetPlugin)
            params = {"code_digits": code_digits, **preset.params}
            glyphs = list(engine.glyphs(params, ctx))
        except Exception:  # noqa: BLE001 — see the docstring: text degrades to hex
            glyphs = []
    alphabet = Alphabet(glyphs, code_digits=code_digits, flag_break=flag_break).shifted(
        base
    )
    stream = list(controls or ())
    # Asked of what the preset *said*, not of what survived the shift: an
    # alphabet dialled clean off the end of the code space is still an alphabet
    # the user picked, and reporting "none picked" there would point them at the
    # combo they already set instead of at the spin they just moved.
    if not glyphs and not stream:
        return None
    if not stream:
        return alphabet
    return alphabet.merged(
        Alphabet(
            glyphs_from_spec(stream, GlyphRole.CONTROL),
            code_digits=code_digits,
            flag_break=flag_break,
        )
    )


def encode_cells(
    cells: list[Cell],
    preset_id: str,
    reg: Registry,
    ctx: PipelineContext | None = None,
) -> bytes:
    """``cells`` back to bytes under ``preset_id`` — the save-side half.

    Separate from a whole-document save because a tilemap edit is local: the
    caller encodes the cells that changed and splices them into the buffer
    :func:`load_tilemap_data` returned, rather than re-encoding a grid whose
    trailing partial row was never part of the file.

    ``ctx`` should be the **document's own**, as :func:`_save_tilemap` passes it:
    a container may have stated something about this file that the preset cannot
    know, and the live case is byte order — 26 corpus sprite objects come from a
    build that stores the attribute word the other way round and say so in their
    header (:data:`~celpix.core.context.KEY_TILEMAP_ENDIAN`). Encoding those
    against a fresh context would write every word back swapped. A default one is
    kept for callers with no document behind them, where the preset is the whole
    of the answer.
    """
    engine, preset = reg.engine_for(preset_id, TilemapCodecPlugin)
    context = PipelineContext() if ctx is None else ctx
    return _run(
        Stage.INTERPRET_TILEMAP,
        Pathway.TILEMAP,
        lambda: engine.encode(cells, preset.params, context),
        "encode",
        plugin=preset.id,
    )


def read_region(cfg: PathwayConfig, reg: Registry) -> tuple[bytes, PipelineContext]:
    """A pathway's Read alone: container -> reshape -> decompress, no decoding.

    The front half of a load without a codec, for a caller that
    needs an entry's **view buffer** rather than its picture — an Offset palette
    resolving its coordinates against the file entry that owns them, when that
    entry is closed and has no live document to borrow the bytes from
    (``docs/design/palette-editing.md`` §2). The context comes back because the
    buffer alone doesn't say where it starts: only the container knows that, and
    it reports it as ``KEY_SOURCE_OFFSET``.
    """
    ctx = PipelineContext()
    return _read_reshape_decompress(cfg, ctx, reg, Pathway.PIXEL), ctx


def load(pixel: PathwayConfig, palette: PathwayConfig, reg: Registry) -> Document:
    """Read + decompress both pathways into a Document (pixels decode on demand)."""
    px = load_pixel_data(pixel, reg)
    pal = load_palette(palette, reg)
    return Document(
        pixel_data=px.data,
        bytes_per_tile=px.bytes_per_tile,
        tile_width=px.tile_width,
        tile_height=px.tile_height,
        palette=pal.palette,
        pixel_config=pixel,
        palette_config=palette,
        pixel_ctx=px.ctx,
        palette_ctx=pal.ctx,
        palette_base_bytes=pal.data,
    )


def save(
    doc: Document, reg: Registry, *, pixel: bool = True, palette: bool = True
) -> None:
    """Encode + compress + write the requested pathways.

    ``write_enabled=False`` on the pixel pathway marks a view-only document — e.g.
    a decompressed slice whose scheme has no compressor — and skips its write.

    ``pixel``/``palette`` let a caller write one pathway alone. The two go to
    different files, so a palette-only edit has no business rewriting the graphic
    (which for a compressed slice could even re-encode to equivalent-but-different
    bytes — see :func:`_save_pixel`).

    A pathway marked ``writes_through_parent`` is refused rather than deposited:
    its target names a file position its bytes do not occupy, so writing it here
    would scatter them. Routing it is the host's job (it is the one that knows
    the parent) — see :func:`encoded_pixel_bytes`.
    """
    if pixel and doc.is_tilemap:
        # ``pixel`` means "the entry's own data", and a tilemap entry's own data
        # is its cells. Its pixel pathway points at whatever tile source it is
        # bound to — a *different* entry's file — which must never be written as
        # a side effect of saving the map (``docs/design/tilemap-entry.md`` §3).
        _save_tilemap(doc, reg)
    elif pixel and doc.pixel_config.write_enabled:
        if doc.pixel_config.writes_through_parent:
            raise PipelineError(
                Stage.CONTAINER,
                Pathway.PIXEL,
                "this region is inside one its parent reorders, so its bytes "
                "have no file position of their own; it must be written through "
                "the parent",
                "write",
            )
        _save_pixel(doc, reg)
    if palette and doc.palette_config.write_enabled:
        _save_palette(doc, reg)


def export_palette(
    doc: Document, path: str, reg: Registry, preset_id: str | None = None
) -> None:
    """Write ``doc``'s palette to ``path`` as a standalone palette file.

    A **whole-palette** encode, unlike the splicing :func:`_save_palette` does:
    the destination is a new file with nothing in it to preserve, so every entry
    is written and the file holds exactly the colors the panel is showing.
    Uncompressed and at offset 0 — a ``.pal`` is the bytes themselves, whatever
    container the palette was read out of.

    ``preset_id`` names the color format to write in, defaulting to the one the
    palette was *read* with. Callers exporting for interchange pass an explicit
    format instead: a ``.pal`` records nothing about its own encoding, so the
    format a reader has to guess should be a deliberate choice, not a side
    effect of how these colors happened to arrive.
    """
    engine, preset = reg.engine_for(
        preset_id or doc.palette_config.interpret_preset_id, ColorCodecPlugin
    )
    data = _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.encode(doc.palette, preset.params, doc.palette_ctx),
        plugin=preset.id,
    )

    def write() -> None:
        container = reg.plugin(Stage.CONTAINER, RAW_CONTAINER, ContainerPlugin)
        _deposit(FileRef(path), lambda t: container.write(data, t, doc.palette_ctx))

    _run(Stage.CONTAINER, Pathway.PALETTE, write, "write", plugin=RAW_CONTAINER)


def _existing(path: str) -> bytes:
    """A *destination's* current bytes, or ``b""`` when it isn't there yet.

    Write-side only. A missing source is a hard failure — reading one lets the
    OSError out so the load stops and says so, rather than opening an empty
    document that looks like a file full of zeroes.
    """
    target = Path(path)
    return target.read_bytes() if target.exists() else b""


def _deposit(ref: FileRef, produce: Callable[[WriteTarget], bytes]) -> None:
    """Hand ``ref``'s current bytes to ``produce`` and store what it returns.

    The mirror of :func:`_acquire`, and the only place a pathway writes a file.
    The destination is read first because a container returns it *whole* — that is
    what lets it keep the framing and the surrounding bytes it never decoded — so
    it has to be shown what is there to keep.

    With several files the returned buffer is cut back apart at the boundaries the
    files on disk have *now*, and each piece written to its own file. So the
    result has to be the length it was handed: the boundaries are the only thing
    that says which bytes belong to which file, and a buffer that changed size has
    moved every boundary after the change by an unknown amount. Refusing is the
    only safe answer — the alternative is writing most of a sprite sheet into the
    wrong chip. A single file has no boundaries to keep and so is free to grow or
    shrink, which is what a fresh palette export relies on.

    Files whose bytes did not change are left alone rather than rewritten
    identically: editing one tile of a region spread over four ROM chips should
    not touch the timestamps of the three that did not change.
    """
    if len(ref.paths) == 1:
        existing = _existing(ref.path)
        result = produce(WriteTarget(existing, ref.path, ref.offset, ref.length))
        Path(ref.path).write_bytes(result)
        return
    blobs = [_existing(path) for path in ref.paths]
    existing = b"".join(blobs)
    result = produce(WriteTarget(existing, ref.path, ref.offset, ref.length))
    if len(result) != len(existing):
        raise ValueError(
            f"result is {len(result)} bytes but the {len(ref.paths)} files it has "
            f"to be split back into hold {len(existing)}; the file boundaries "
            "would move, so nothing was written"
        )
    at = 0
    for path, blob in zip(ref.paths, blobs):
        chunk = result[at : at + len(blob)]
        at += len(blob)
        if chunk != blob:
            Path(path).write_bytes(chunk)


def _read_reshape_decompress(
    cfg: PathwayConfig, ctx: PipelineContext, reg: Registry, pathway: Pathway
) -> bytes:
    # Said once per load, here rather than where the config was built, because a
    # notice needs the context a load creates. Without it the user meets a file
    # that opens looking wrong with Write greyed out and nothing saying why.
    for stage, wanted in cfg.missing_plugins:
        warn(
            ctx,
            f"Missing plugin: {wanted}",
            f"This entry reads through a {stage.value} plugin this build\n"
            "does not have, so its bytes are shown untransformed and\n"
            "cannot be saved. Install the plugin, or choose a different\n"
            f"{stage.value} to make the entry editable again.",
            stage.value,
        )

    def read() -> bytes:
        source, files = _acquire(cfg.source)
        # Provenance the host owns, because it is the host that knows where the
        # bytes came from; the container publishes only KEY_SOURCE_OFFSET, which
        # is a fact about the format rather than about the source. Both keys are
        # set before the container runs, so one that wants to know how its buffer
        # was assembled can read KEY_SOURCE_FILES while assembling it.
        ctx.set(KEY_SOURCE_PATH, source.path)
        ctx.set(KEY_SOURCE_FILES, files)
        return reg.plugin(Stage.CONTAINER, cfg.container_id, ContainerPlugin).read(
            source, ctx
        )

    raw = _run(Stage.CONTAINER, pathway, read, "read", plugin=cfg.container_id)
    # The reshape sits between the container and the decompressor so that a
    # compressed structure inside an interleaved region is contiguous by the
    # time compression sees it. It is handed the container's whole payload —
    # a reshape is region-scoped, and the region is what Read produced.
    shaped = _run(
        Stage.RESHAPE,
        pathway,
        lambda: reg.plugin(Stage.RESHAPE, cfg.reshape_id, ReshapePlugin).reshape(
            raw, ctx
        ),
        "reshape",
        plugin=cfg.reshape_id,
    )
    return _run(
        Stage.COMPRESSION,
        pathway,
        lambda: reg.plugin(
            Stage.COMPRESSION, cfg.compression_id, CompressionPlugin
        ).decompress(shaped, ctx),
        "decompress",
        plugin=cfg.compression_id,
    )


def _save_pixel(doc: Document, reg: Registry) -> None:
    # The decompressed pixel bytes are the source of truth: edits are already
    # spliced into them (encode_tiles -> Document.replace_bytes), so saving is just
    # compress + write of the buffer as it stands. Writing the bytes is exactly
    # equivalent to encode(decode(bytes)) for these codecs, and avoids decoding the
    # whole file just to save it. Note that a real compressor may make different
    # encoding choices than the original stream, so writing a *compressed* pathway
    # can rewrite equivalent-but-different bytes inside the slot even where nothing
    # was edited — harmless, and rare: dirty tracking gates Write All.
    _compress_unshape_write(
        doc.pixel_config, doc.pixel_data, doc.pixel_ctx, reg, Pathway.PIXEL
    )


def _save_tilemap(doc: Document, reg: Registry) -> None:
    """Encode a tilemap's cells and write them back through its own container.

    Re-encoded from the cells rather than written from the buffer they were read
    into, unlike :func:`_save_pixel`. A tilemap edit changes a *cell*, which is a
    model object and not a byte range, so the cells are the source of truth here
    and the buffer is only what they came from. The round trip is exact — every
    field a format has survives it (:mod:`celpix.plugins.builtins.tilemap_codec`)
    — so an unedited map still writes back the bytes it was read from.

    The container's write half is what preserves everything around the payload:
    a screen's trailing metadata block, a panel's flag table. Nothing here has to
    know those exist.
    """
    cfg = doc.tilemap_config
    if cfg is None or not cfg.write_enabled:
        return
    engine, preset = reg.engine_for(cfg.interpret_preset_id, TilemapCodecPlugin)
    data = _run(
        Stage.INTERPRET_TILEMAP,
        Pathway.TILEMAP,
        lambda: engine.encode(doc.cells or [], preset.params, doc.tilemap_ctx),
        "encode",
        plugin=preset.id,
    )
    _compress_unshape_write(cfg, data, doc.tilemap_ctx, reg, Pathway.TILEMAP)


def encoded_pixel_bytes(doc: Document, reg: Registry) -> bytes:
    """The pixel buffer as a save would lay it down: compressed and un-reshaped.

    :func:`_save_pixel` without the deposit, for the entry whose bytes have no
    file position to be deposited *at*: a slice inside a region its parent
    reorders. Those bytes belong at an offset in the parent's buffer, and only
    the parent's own write — which carries the whole region through ``unshape``
    and the container — knows where that lands in the files. The host splices
    this in and writes the parent (``docs/design/reshape-stage.md`` §3), exactly
    as an Offset palette in the same position rides its owner
    (:func:`spliced_palette_bytes`).

    The slot checks still apply and are still made here, against the slice's own
    bounds: what is produced has to fit the window it came from wherever it is
    ultimately delivered.
    """
    return _compress_unshape(
        doc.pixel_config, doc.pixel_data, doc.pixel_ctx, reg, Pathway.PIXEL
    )


def _save_palette(doc: Document, reg: Registry) -> None:
    """Encode + write the palette, **splicing** only the entries that changed.

    A color codec round-trips ARGB faithfully but not *bytes*: anything outside
    its masks is dropped, and an indexed codec has no inverse at all. So writing
    a full re-encode would rewrite — and corrupt — entries the user never
    touched. Instead the freshly encoded bytes of edited entries are spliced
    into the buffer the palette was read from, leaving every other byte exactly
    as it was found (see :attr:`Document.palette_base_bytes`).

    Falls back to a whole-palette encode when there is nothing to splice into —
    no original bytes, or a palette whose length no longer matches them (a
    format switch changes the entry size, so the old buffer doesn't apply).
    """
    data = spliced_palette_bytes(doc, reg)
    _compress_unshape_write(
        doc.palette_config, data, doc.palette_ctx, reg, Pathway.PALETTE
    )
    # The file now holds these bytes, so they become the baseline for the next
    # splice and no entry is outstanding. Skipping this would make a second save
    # splice against pre-save bytes and undo the first one's edits.
    doc.palette_base_bytes = data
    doc.palette_edits = set()


def spliced_palette_bytes(doc: Document, reg: Registry) -> bytes:
    """The palette's byte image as a save would write it: the freshly encoded
    bytes of the *edited* entries spliced into the buffer it was read from.

    The encode half of :func:`_save_palette`, usable without a write target —
    an Offset palette living inside a reordered region persists an edit by
    splicing this into the owning entry's pixel buffer rather than through its
    own (write-disabled) pathway (``docs/design/palette-editing.md`` §2).
    """
    cfg = doc.palette_config
    engine, preset = reg.engine_for(cfg.interpret_preset_id, ColorCodecPlugin)
    encoded = _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.encode(doc.palette, preset.params, doc.palette_ctx),
        plugin=preset.id,
    )
    return _splice_palette(doc, encoded, engine, preset)


def _splice_palette(doc: Document, encoded: bytes, engine, preset) -> bytes:  # noqa: ANN001
    original = doc.palette_base_bytes
    if not original or len(original) != len(encoded):
        return encoded
    size = _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.bytes_per_entry(preset.params),
        plugin=preset.id,
    )
    if size <= 0:
        return encoded
    per_unit = getattr(engine, "entries_per_unit", None)
    step = max(1, per_unit(preset.params)) if per_unit is not None else 1
    out = bytearray(original)
    for index in doc.palette_edits:
        # The unit the entry lives in, not the entry: a Game Boy shade shares its
        # byte with three others, so the byte is the smallest thing that can be
        # put back. Its neighbours re-encode to what they already were.
        start = (index // step) * size
        if 0 <= start and start + size <= len(out):
            out[start : start + size] = encoded[start : start + size]
    return bytes(out)


def _compress_unshape_write(
    cfg: PathwayConfig,
    data: bytes,
    ctx: PipelineContext,
    reg: Registry,
    pathway: Pathway,
) -> None:
    """Compress ``data``, undo any reshape, and write it to the config's target."""
    shaped = _compress_unshape(cfg, data, ctx, reg, pathway)
    target = cfg.write_target()

    def write() -> None:
        container = reg.plugin(Stage.CONTAINER, cfg.container_id, ContainerPlugin)
        _deposit(target, lambda dest: container.write(shaped, dest, ctx))

    _run(Stage.CONTAINER, pathway, write, "write", plugin=cfg.container_id)


def _compress_unshape(
    cfg: PathwayConfig,
    data: bytes,
    ctx: PipelineContext,
    reg: Registry,
    pathway: Pathway,
) -> bytes:
    """``data`` compressed and un-reshaped: the bytes that belong in the slot.

    The write minus the deposit, so the checks that make a slot safe are stated
    once and hold however the bytes are then delivered — through the container to
    a file, or spliced into a parent's buffer
    (:func:`encoded_pixel_bytes`).

    A bounded target (``length`` set — a slice of a larger file) is a hard slot:
    a result that would overflow it raises before anything touches the file. A
    result *smaller* than the slot leaves room at the end, and what goes there is
    the pathway's :class:`~celpix.pipeline.pathway.SlotFill`: padded out to the
    slot with ``$FF`` or ``$00``, or written short so the previous stream's tail
    stands. Every supported scheme is self-delimiting, so no reader reaches those
    bytes either way — the choice is about what someone reading the file finds.

    Only a **compressed** pathway is padded. Everywhere else the result is the
    length of the buffer it was read from, so a short one means something the
    pathway didn't expect, and inventing bytes to cover it would bury that.

    **Under an active reshape the slot must be filled exactly.** A reshape's
    part boundaries are fractions of the region's length, so a short result is
    a *different region* whose unshape scatters every byte to the wrong chip —
    there is no such thing as writing the front of one.
    """
    packed = _run(
        Stage.COMPRESSION,
        pathway,
        lambda: reg.plugin(
            Stage.COMPRESSION, cfg.compression_id, CompressionPlugin
        ).compress(data, ctx),
        "compress",
        plugin=cfg.compression_id,
    )
    shaped = _run(
        Stage.RESHAPE,
        pathway,
        lambda: reg.plugin(Stage.RESHAPE, cfg.reshape_id, ReshapePlugin).unshape(
            packed, ctx
        ),
        "unshape",
        plugin=cfg.reshape_id,
    )
    target = cfg.write_target()
    if target.length is not None and len(shaped) > target.length:
        raise PipelineError(
            Stage.CONTAINER,
            pathway,
            f"result ({len(shaped)} bytes) exceeds the {target.length}-byte slot "
            f"at {target.offset:#x} in {target.path}",
            "write",
        )
    if (
        cfg.reshape_id != NO_RESHAPE
        and target.length is not None
        and len(shaped) != target.length
    ):
        raise PipelineError(
            Stage.RESHAPE,
            pathway,
            f"result ({len(shaped)} bytes) must fill the {target.length}-byte "
            f"slot at {target.offset:#x} in {target.path} exactly: a reshape's "
            "boundaries are fractions of the region, so a shorter region is a "
            "different reshape",
            "unshape",
        )
    if (
        cfg.compression_id != NO_COMPRESSION
        and target.length is not None
        and len(shaped) < target.length
    ):
        # A reshape can't reach here — the check above already demanded an exact
        # fill — so padding never lands inside a permutation it would scatter.
        filler = cfg.slot_fill.filler
        if filler:
            shaped += filler * (target.length - len(shaped))
    return shaped
