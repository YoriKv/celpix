"""Exporting interpreted graphics to standalone image / raw files.

Export is a one-way projection *out* of Celpix's model: it renders an entry's
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

from celpix.core.arrangement import BlockLayout
from celpix.core.document import Document
from celpix.pipeline import pipeline
from celpix.plugins.registry import Registry
from celpix.ui import render_bridge


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
    grid, _filled = pipeline.decode_and_compose(
        doc.pixel_data, engine, preset.params, layout, view.two_dimensional, None
    )
    if getattr(grid, "bytes_per_pixel", 1) == 4:
        # Direct-color: no palette; the ARGB carries its own alpha.
        return render_bridge.render(grid, doc.palette)
    index_space = min(
        256, 1 << pipeline.pixel_bpp(doc.pixel_config.interpret_preset_id, registry)
    )
    base = view.subpalette_row * index_space
    # Exactly one entry per index the format can produce, in Celpix order — no
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
