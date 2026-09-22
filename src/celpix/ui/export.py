"""Exporting interpreted graphics to standalone image / raw files.

Export is a one-way projection *out* of celPix's model: it renders an entry's
whole document — every tile, laid out by its view arrangement — to a PNG, or
writes the decoded pixel bytes straight out as a raw binary. Unlike Write, it
never targets the source file; it produces new, self-contained files for use in
other tools.

The PNG is a genuine **indexed** (color-type-3) image: the render bridge builds
a ``Format_Indexed8`` QImage whose color table is exactly the active palette row,
and Qt's PNG writer turns that into a palette PNG — so an exported sheet opens in
a sprite editor as an indexed image, with the palette and index identity intact.
Colors
keep the codec's own alpha; index 0 is exported opaque like any other entry (its
color is preserved, not forced transparent) unless a tilemap's **Transparent 0**
box is ticked, which the export follows as the canvas does — see
``docs/design/export.md``.

This lives on the ``ui`` side because it produces ``QImage`` and uses Qt's image
writer; the decode+compose core it calls (``pipeline.decode_and_compose``) is the
same Qt-free arrangement path the live view uses.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect
from PySide6.QtGui import QImage

from celpix.core import gif
from celpix.core.animation import Sequence
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
    tile *n* and the rearrangement is not consulted. An export is the file's own
    order.

    Regions are bounded here as the view bounds them, so a row that outran a
    shorter palette exports as the unpinned view shows it rather than as the
    magenta missing-colour sentinel. And a pinned row is a **named** row, so it
    goes through the document's base exactly as the canvas takes it
    (:func:`~celpix.pipeline.pipeline.drawn_palette_row`); the view's own row,
    which fills the unpinned tiles, is already absolute and does not.
    """
    view = doc.view
    if not view.show_palette_regions or view.palette_regions.is_empty():
        return None
    index_space = pipeline.palette_row_size(
        doc.pixel_config.interpret_preset_id, registry
    )
    wrap = doc.palette_row_wrap(index_space)
    per_tile = doc.tile_width * doc.tile_height
    regions = view.palette_regions.bounded(
        doc.tile_count * per_tile, doc.palette_rows(index_space) - 1
    )
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
    return [
        (
            view.palette_row
            if row is None
            else pipeline.drawn_palette_row(row, doc.palette_row_base, wrap)
        )
        * index_space
        for row in regions.rows_for(offsets, None)
    ]


def _tilemap_image(doc: Document, registry: Registry, columns: int) -> QImage:
    """Export a tilemap entry as the **map**, not as the tiles behind it.

    What such an entry *is* on screen is its cells drawn through the tile source
    it is bound to, and that is what the file it exports has to hold. Rendering
    its ``pixel_data`` instead — which is the bound entry's bytes, sitting there
    so every tile path keeps working (``docs/design/tilemap-entry.md`` §8.1) —
    would quietly export the tile bank under a screen's name, or nothing at all
    for a map with no binding yet.

    Straight through :func:`~celpix.pipeline.pipeline.tilemap_image`, the same
    call the canvas makes, so the two cannot disagree. The colour table has to
    span every palette row the map uses rather than one palette row: a cell names
    its own row, the row is folded into the indices upstream, and one image
    carries one table.

    Where the **format** has no palette row to give, the map indexes a single
    range of the palette like a pixel document and the view's palette row says
    which — the same fallback the canvas takes, so the file still matches the
    screen (``rendering.RenderingMixin._render_tilemap``).
    """
    drawn = pipeline.tilemap_image(doc, registry, columns)
    index_space = pipeline.palette_row_size(
        doc.pixel_config.interpret_preset_id, registry
    )
    top = min(256, drawn.palette_rows * index_space)
    base = 0 if doc.cells_carry_palette_rows else doc.view.palette_row * index_space
    table = [doc.palette.color(base + i) for i in range(top)]
    if doc.view.transparent_zero:
        # The map's own Transparent 0 box, honoured here as on the canvas: a
        # backdrop the user has cleared on screen leaves as a hole too, where the
        # pixel path below never clears — its box does not exist to be ticked.
        # The table starts on a row boundary either way, so every row's index 0
        # sits at a multiple of ``index_space``.
        table = render_bridge.clear_zeros(table, index_space)
    return render_bridge.paint_hidden(
        render_bridge.indexed_image(drawn.grid, table),
        drawn.hidden,
    )


def document_image(doc: Document, registry: Registry) -> QImage:
    """Render every tile of ``doc`` to one QImage, laid out per its view options.

    The full-file analogue of the windowed live view: it honors the columns, the
    block/2D arrangement and the active palette row, so the export matches what
    the canvas shows — just the whole file rather than the visible window. An
    indexed codec yields a ``Format_Indexed8`` image whose color table is exactly
    the active palette row, so Qt writes a compact indexed PNG; a direct-color
    codec yields ``Format_ARGB32``.

    A tilemap entry takes a route of its own (:func:`_tilemap_image`): what it
    shows is the map, and its pixel bytes are a different entry's tiles.
    """
    view = doc.view
    cols = max(1, view.columns)
    if doc.is_tilemap:
        return _tilemap_image(doc, registry, cols)
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
    index_space = pipeline.palette_row_size(
        doc.pixel_config.interpret_preset_id, registry
    )
    if biases is not None:
        # Pinned regions: the row is already in the indices, so the table cannot
        # offset again — and it has to span every row on screen rather than one
        # palette row. Sized to the highest row actually used, not blindly to 256,
        # so a two-palette sheet exports a two-row table.
        top = (max(biases) // index_space + 1) * index_space
        return render_bridge.indexed_image(
            grid, [doc.palette.color(i) for i in range(top)]
        )
    base = view.palette_row * index_space
    # Exactly one entry per index the format can produce, in celPix order — no
    # minimizing, which editors do on load and which would renumber unused
    # leading colors. Every
    # entry keeps the codec's own alpha; index 0 is *not* forced transparent, so a
    # meaningful color 0 exports as the opaque color it is.
    table = [doc.palette.color(base + i) for i in range(index_space)]
    return render_bridge.indexed_image(grid, table)


def save_png(image: QImage, path: str) -> bool:
    """Write ``image`` to ``path`` as PNG; False if Qt could not write it."""
    return image.save(path, "PNG")


def raw_bytes(doc: Document) -> bytes:
    """The bytes ``doc`` is, for a raw dump — its **own** data, not what it draws.

    The *decompressed*, decoded bytes the document holds: for a compressed slice,
    its unpacked contents, which is what a raw dump is wanted for (the actual
    graphics data, not the packed stream). For a tilemap entry that is its
    **cells**, since its pixel bytes belong to the entry it borrows tiles from —
    dumping those would write another file's contents under this one's name.
    """
    return doc.tilemap_data if doc.is_tilemap else doc.pixel_data


def save_raw(doc: Document, path: str) -> None:
    """Write :func:`raw_bytes` to ``path``. Raises ``OSError`` on a write
    failure, for the caller to report."""
    with open(path, "wb") as handle:
        handle.write(raw_bytes(doc))


def sequence_frames(
    strip: QImage, rects: list[QRect], sequence: Sequence
) -> list[QImage | None]:
    """The picture each step of ``sequence`` shows, in play order.

    Cut from the animation player's strip, so a frame exported is the frame the
    player draws — colour rule, Transparent 0 and all. None where a step names a
    frame the file does not hold: the player shows nothing there, and so does
    the export, rather than a plausible stand-in.

    The steps are taken once through, lead-in included. A sequence that loops
    back past its first steps cannot say so in either output format.
    """
    return [
        strip.copy(rects[step.frame]) if 0 <= step.frame < len(rects) else None
        for step in sequence.steps
    ]


def _argb(image: QImage) -> list[int]:
    """``image``'s pixels as ``0xAARRGGBB`` ints, row-major.

    Read off the buffer rather than one ``QImage.pixel`` call per pixel, which is
    what the whole of a GIF export used to cost: ``Format_ARGB32`` stores each
    pixel as a single host-order word, which is already the int wanted. Rows are
    padded to a multiple of four bytes, so the stride is read from the image
    rather than taken to be its width.
    """
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    width = image.width()
    stride = image.bytesPerLine() // 4
    words = memoryview(image.constBits()).cast("I")
    return [words[y * stride + x] for y in range(image.height()) for x in range(width)]


def _step_pixels(
    strip: QImage, rects: list[QRect], sequence: Sequence
) -> list[list[int] | None]:
    """Each step's pixels as :func:`_argb` ints, converted **once per frame**.

    A sequence names the same frame over and over — a two-frame flap held for a
    hundred steps — and the cut, the conversion and the compression are all per
    *pixel*. The repeats come back as the same list object, which is how the
    encoder keys and compresses each distinct frame once too
    (:func:`~celpix.core.gif.encode`). None where a step names a frame the file
    does not hold, as :func:`sequence_frames` answers for the PNG side.
    """
    converted: dict[int, list[int] | None] = {}
    for step in sequence.steps:
        if step.frame not in converted:
            converted[step.frame] = (
                _argb(strip.copy(rects[step.frame]))
                if 0 <= step.frame < len(rects)
                else None
            )
    return [converted[step.frame] for step in sequence.steps]


def save_sequence_gif(
    strip: QImage, rects: list[QRect], sequence: Sequence, rate: int, path: str
) -> None:
    """Write ``sequence`` as a looping GIF, timed at ``rate`` ticks per second.

    Raises ``ValueError`` (nothing to write, or more colours than a GIF holds)
    or ``OSError`` for the caller to report.
    """
    if not rects or not sequence.steps:
        raise ValueError("the sequence has no steps")
    size = rects[0].size()
    frames = _step_pixels(strip, rects, sequence)
    delays = gif.delays_cs([step.duration for step in sequence.steps], rate)
    data = gif.encode(
        size.width(),
        size.height(),
        list(zip(frames, delays, strict=True)),
    )
    Path(path).write_bytes(data)


def _blank_like(strip: QImage, rect: QRect) -> QImage:
    """An empty frame for a step naming a frame the file does not hold.

    Kept indexed, through the strip's own table, wherever that table has a clear
    entry to fill with — so a PNG sequence stays one kind of file throughout.
    Only a strip with no transparency to borrow falls back to ARGB.
    """
    if strip.format() == QImage.Format.Format_Indexed8:
        table = strip.colorTable()
        clear = next((i for i, c in enumerate(table) if c >> 24 == 0), None)
        if clear is not None:
            blank = strip.copy(rect)
            blank.fill(clear)
            return blank
    blank = QImage(rect.size(), QImage.Format.Format_ARGB32)
    blank.fill(0)
    return blank


def save_sequence_pngs(
    strip: QImage, rects: list[QRect], sequence: Sequence, stem: str
) -> list[str]:
    """Write ``sequence`` as one PNG per step, ``<stem>-01.png`` onward.

    Numbered from 1 like the player's status line, and padded so the files sort
    in play order. Returns the paths written; raises ``ValueError`` when there
    is nothing to write and ``OSError`` on the first file that could not be.
    """
    if not rects or not sequence.steps:
        raise ValueError("the sequence has no steps")
    frames = sequence_frames(strip, rects, sequence)
    digits = max(2, len(str(len(frames))))
    written = []
    for at, frame in enumerate(frames, 1):
        if frame is None:
            frame = _blank_like(strip, rects[0])
        path = f"{stem}-{at:0{digits}d}.png"
        if not save_png(frame, path):
            raise OSError(f"Could not write {path}.")
        written.append(path)
    return written
