"""Scalar questions asked of a resolved codec — sizes, capacities, bit depth.

One place to ask a plugin a number: how many bytes one palette read unit takes,
how many entries fit a window, how many bits a pixel has, what a colour actually
survives being stored as. Everything that sizes a palette window, a colour
table or an index space comes through here, so the several callers that need the
same number resolve the same preset the same way and cannot answer differently.

Several answers are **behavioural** rather than read out of preset params:
whether a colour format keeps alpha, and whether a pixel codec yields colours
instead of palette indices, are settled by a round trip through the engine. That
holds for a mask-based codec, an indexed one and a plugin's own alike, without
any of them growing a capability flag to declare it — and it reports what the
codec really does rather than what its preset claims. Bit depth is derived from
the geometry for the same reason: not every codec spells ``bpp`` as a param.

Every function here is a query. Nothing is read, written or cached, and no
document is involved: running the stages is :mod:`celpix.pipeline.pipeline`'s,
and drawing what they produce is :mod:`celpix.pipeline.render`'s.
"""

from __future__ import annotations

from celpix.core import ceil_div
from celpix.core.context import PipelineContext
from celpix.core.errors import Pathway, Stage
from celpix.core.palette import Palette
from celpix.pipeline._stage import _run
from celpix.plugins.base import ColorCodecPlugin, PixelCodecPlugin
from celpix.plugins.registry import Registry


def palette_entry_size(preset_id: str, reg: Registry) -> int:
    """Byte size of one palette **read unit** — the stride palette windows step by.

    One entry for nearly every format. The handheld grayscale registers pack
    several into a unit, so pair this with :func:`palette_entries_per_unit`
    whenever converting between a colour count and a byte length —
    :func:`palette_read_bytes` and :func:`palette_entry_capacity` are that
    conversion.
    """
    engine, preset = reg.engine_for(preset_id, ColorCodecPlugin)
    return _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.bytes_per_entry(preset.params),
        plugin=preset.id,
    )


def palette_entries_per_unit(preset_id: str, reg: Registry) -> int:
    """How many palette entries one read unit holds — 1 unless they are packed.

    Optional on the codec surface, so a colour codec that predates packed
    entries (or a third-party one that never needed them) answers 1 by omission.
    """
    engine, preset = reg.engine_for(preset_id, ColorCodecPlugin)
    per_unit = getattr(engine, "entries_per_unit", None)
    if per_unit is None:
        return 1
    return max(
        1,
        _run(
            Stage.INTERPRET_PALETTE,
            Pathway.PALETTE,
            lambda: per_unit(preset.params),
            plugin=preset.id,
        ),
    )


def palette_read_bytes(count: int, preset_id: str, reg: Registry) -> int:
    """Bytes a read window needs to hold ``count`` palette entries.

    Rounded up to a whole unit: a packed format has no way to read three of the
    four shades in a Game Boy's palette byte.
    """
    per_unit = palette_entries_per_unit(preset_id, reg)
    units = (max(0, count) + per_unit - 1) // per_unit
    return units * palette_entry_size(preset_id, reg)


def palette_entry_capacity(nbytes: int, preset_id: str, reg: Registry) -> int:
    """How many whole palette entries fit in ``nbytes`` — floored to whole units.

    The inverse of :func:`palette_read_bytes`. Flooring is what keeps a window
    off a partial trailing unit, which the colour codecs reject.
    """
    size = palette_entry_size(preset_id, reg)
    if size <= 0:
        return 0
    return (max(0, nbytes) // size) * palette_entries_per_unit(preset_id, reg)


def quantize_color(argb: int, preset_id: str, reg: Registry) -> int:
    """``argb`` as it would come back after a round trip through ``preset_id``.

    Encode-then-decode of a one-entry palette: the color editor edits in full
    8-bit RGB, and this is what the chosen palette format can actually store —
    BGR555 drops the low three bits of each channel, an indexed format snaps to
    its nearest hardware color. Shown live beside the edited color so the loss
    is visible *before* it is written (docs/design/palette-editing.md).
    """
    return quantize_palette(Palette([argb]), preset_id, reg).color(0)


def quantize_palette(palette: Palette, preset_id: str, reg: Registry) -> Palette:
    """``palette`` as it comes back after a round trip through ``preset_id``.

    The whole-palette form of :func:`quantize_color`: encode the colors to the
    format's bytes and decode them straight back, so every entry lands on a
    value that format can actually hold. Used to *rebase* a Custom palette when
    its color format is changed — a Custom palette has no source bytes to
    reinterpret, so its stored ARGB colors are re-expressed in the new format
    instead of anything being re-read.
    """
    engine, preset = reg.engine_for(preset_id, ColorCodecPlugin)

    def _round_trip() -> Palette:
        ctx = PipelineContext()
        data = engine.encode(palette, preset.params, ctx)
        return engine.decode(data, preset.params, ctx)

    return _run(Stage.INTERPRET_PALETTE, Pathway.PALETTE, _round_trip, plugin=preset.id)


def palette_has_alpha(preset_id: str, reg: Registry) -> bool:
    """Whether ``preset_id`` actually stores an alpha channel.

    Probed behaviourally rather than by reading codec params, so it holds for
    every color engine — mask-based, indexed, or a plugin's own — without any
    of them growing a new method: a format with no alpha field decodes one back
    as opaque (``_mask.value_to_argb`` substitutes ``0xFF``), so a transparent
    color that survives the round trip proves the field exists.

    Drives whether the color editor offers an alpha input at all.
    """
    return quantize_color(0x00FFFFFF, preset_id, reg) >> 24 != 0xFF


def pixel_is_direct_color(preset_id: str, reg: Registry) -> bool:
    """Whether ``preset_id``'s codec produces colors rather than palette indices.

    Probed behaviourally — a blank tile is decoded and its grid type inspected —
    for the same reason :func:`palette_has_alpha` is: it then holds for every
    pixel engine, including a plugin's own, without any of them declaring a new
    capability flag. Tells the editing paths whether incoming pixels must be
    fitted to the palette or carried through as color.
    """
    engine, preset = reg.engine_for(preset_id, PixelCodecPlugin)

    def _probe() -> bool:
        blank = bytes(engine.bytes_per_tile(preset.params))
        tiles = engine.decode(blank, preset.params, PipelineContext())
        return bool(tiles) and tiles[0].bytes_per_pixel == 4

    return _run(Stage.INTERPRET_PIXEL, Pathway.PIXEL, _probe, plugin=preset.id)


def pixel_tile_bytes(preset_id: str, reg: Registry) -> int:
    """How many bytes one tile of ``preset_id`` occupies.

    The same number :func:`~celpix.pipeline._stage._pixel_geometry` puts on a
    document, asked of a preset alone — for a caller that has to size a run of
    tiles *before* there is a config to build a document from. A **composite**
    entry's blank pads are that caller: a gap in an assembled tile window is
    stated in tiles and has to become bytes for the buffer to be laid out at all
    (``docs/design/composite-entry.md``).

    On the codec's own tile, deliberately: a wide-bitmap width re-cuts the
    geometry per *view*, and a pad measured against one view's cut would move
    every tile after it when the view changed.
    """
    engine, preset = reg.engine_for(preset_id, PixelCodecPlugin)
    return _run(
        Stage.INTERPRET_PIXEL,
        Pathway.PIXEL,
        lambda: engine.bytes_per_tile(preset.params),
        plugin=preset.id,
    )


def pixel_bpp(preset_id: str, reg: Registry) -> int:
    """Bits per pixel of a pixel preset, from its resolved engine's geometry.

    Derived (tile bits ÷ tile pixels) rather than read from ``params["bpp"]``: bpp
    is a property of the codec's tile layout, and not every codec spells it as a
    preset param — the wide/odd-tile codecs and code formats fix their geometry
    intrinsically and carry no ``bpp``. Every pixel engine exposes
    ``bytes_per_tile``/``tile_size``, so deriving it here is uniform and matches
    whatever the decoder actually produced. Rounded up so a non-whole bit depth
    still yields an index space wide enough for its largest index.
    """
    engine, preset = reg.engine_for(preset_id, PixelCodecPlugin)

    def _bpp() -> int:
        w, h = engine.tile_size(preset.params)
        pixels = w * h
        if pixels <= 0:
            raise ValueError(f"tile {w}x{h} has no pixels")
        return ceil_div(engine.bytes_per_tile(preset.params) * 8, pixels)

    return _run(Stage.INTERPRET_PIXEL, Pathway.PIXEL, _bpp, plugin=preset.id)
