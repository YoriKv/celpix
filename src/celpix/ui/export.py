"""Exporting interpreted graphics to standalone image / raw files.

Export is a one-way projection *out* of celPix's model: it renders an entry's
whole document — every tile, laid out by its view arrangement — to a PNG, or
writes the decoded pixel bytes straight out as a raw binary. Unlike Write, it
never targets the source file; it produces new, self-contained files for use in
other tools.

The PNG is a genuine **indexed** (color-type-3) image: the render bridge builds
a ``Format_Indexed8`` QImage whose color table is exactly the active subpalette,
and Qt's PNG writer turns that into a palette PNG — so an exported sheet opens in
Aseprite as an indexed sprite with the palette and index identity intact. Colors
keep the codec's own alpha; index 0 is exported opaque like any other entry (its
color is preserved, not forced transparent) — see ``docs/design/export.md``.

This lives on the ``ui`` side because it produces ``QImage`` and uses Qt's image
writer; the decode+compose core it calls (``pipeline.decode_and_compose``) is the
same Qt-free arrangement path the live view uses.
"""

from __future__ import annotations

from PySide6.QtGui import QImage

from celpix.core.arrangement import BlockLayout, tile_first_pixel
from celpix.core.document import Document
from celpix.pipeline import pipeline
from celpix.plugins.registry import Registry
from celpix.ui import render_bridge


def _palette_biases(
    doc: Document, registry: Registry, columns: int
) -> list[int] | None:
    """Pinned-region index shifts for **every** tile, or None if nothing is pinned.

    The whole-file counterpart of the live view's ``_window_biases``. One thing
    differs from the canvas, and it follows from what export is: the document is
    rendered whole, from its first tile, with no rearrangement — so slot *n* is
    tile *n* and the tile map is not consulted. An export is the file's own order.

    Regions are bounded here as the view bounds them, so a row that outran a
    shorter palette exports as the unpinned view shows it rather than as the
    magenta missing-colour sentinel.
    """
    view = doc.view
    if not view.show_palette_regions or view.palette_regions.is_empty():
        return None
    index_space = min(
        256, 1 << pipeline.pixel_bpp(doc.pixel_config.interpret_preset_id, registry)
    )
    max_row = min(max(0, len(doc.palette) - 1) // index_space, 256 // index_space - 1)
    per_tile = doc.tile_width * doc.tile_height
    regions = view.palette_regions.bounded(doc.tile_count * per_tile, max_row)
    if regions.is_empty():
        return None
    offsets = [
        tile_first_pixel(
            slot,
            doc.tile_width,
            doc.tile_height,
            max(1, columns),
            view.two_dimensional,
        )
        for slot in range(doc.tile_count)
    ]
    return [row * index_space for row in regions.rows_for(offsets, view.subpalette_row)]


def document_image(doc: Document, registry: Registry) -> QImage:
    """Render every tile of ``doc`` to one QImage, laid out per its view options.

    The full-file analogue of the windowed live view: it honors the columns, the
    block/2D arrangement and the active subpalette row, so the export matches what
    the canvas shows — just the whole file rather than the visible window. An
    indexed codec yields a ``Format_Indexed8`` image whose color table is exactly
    the active subpalette window (index 0 transparent), so Qt writes a compact
    indexed PNG; a direct-color codec yields ``Format_ARGB32``.
    """
    view = doc.view
    cols = max(1, view.columns)
    engine, preset = registry.engine_for(doc.pixel_config.interpret_preset_id)
    layout = BlockLayout(cols, view.block_columns, view.block_rows, view.block_order)
    biases = _palette_biases(doc, registry, cols)
    grid, _filled = pipeline.decode_and_compose(
        doc.pixel_data,
        engine,
        # The document's own geometry, not the preset's: under a bitmap width
        # those differ, and an export cut into different tiles than the canvas
        # shows would not be the picture the user is looking at.
        pipeline.tile_params(doc, engine, preset.params),
        layout,
        view.two_dimensional,
        None,
        biases,
    )
    if grid.bytes_per_pixel == 4:
        # Direct-color: no palette; the ARGB carries its own alpha.
        return render_bridge.render(grid, doc.palette)
    index_space = min(
        256, 1 << pipeline.pixel_bpp(doc.pixel_config.interpret_preset_id, registry)
    )
    if biases is not None:
        # Pinned regions: the row is already in the indices, so the table cannot
        # offset again — and it has to span every row on screen rather than one
        # subpalette. Sized to the highest row actually used, not blindly to 256,
        # so a two-palette sheet exports a two-row table.
        top = (max(biases) // index_space + 1) * index_space
        return render_bridge.indexed_image(
            grid, [doc.palette.color(i) for i in range(top)]
        )
    base = view.subpalette_row * index_space
    # Exactly one entry per index the format can produce, in celPix order — no
    # minimizing (Aseprite would otherwise renumber unused leading colors). Every
    # entry keeps the codec's own alpha; index 0 is *not* forced transparent, so a
    # meaningful color 0 exports as the opaque color it is.
    table = [doc.palette.color(base + i) for i in range(index_space)]
    return render_bridge.indexed_image(grid, table)


def save_png(image: QImage, path: str) -> bool:
    """Write ``image`` to ``path`` as PNG; False if Qt could not write it."""
    return image.save(path, "PNG")


def save_raw(doc: Document, path: str) -> None:
    """Write ``doc``'s decoded pixel bytes to ``path`` as a raw binary.

    These are the *decompressed*, decoded bytes the document holds — for a
    compressed slice, its unpacked contents, which is what a raw dump is wanted
    for (the actual graphics data, not the packed stream). Raises ``OSError`` on a
    write failure, for the caller to report.
    """
    with open(path, "wb") as handle:
        handle.write(doc.pixel_data)
