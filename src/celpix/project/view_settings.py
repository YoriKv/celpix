"""Copy / Paste View Settings: how one entry is read and coloured, as a value.

What travels is the answer the toolbars and the palette dock give for the entry
on screen — never its bytes, its offset, or its window size — so that a format
found by trial on one slice can be put on the next twenty with a keystroke each.
Two halves, because a pixel entry and a tilemap are read by different controls:

- **how the entry is read** — a pixel entry's format, swatch color format and
  arrangement (Pattern, Block, Order, 2D, bitmap width), or a tilemap's cell
  format. This half lands only on an entry of the same content kind: a cell
  format says nothing about tiles, and a pixel format nothing about cells.
- **what it previews and is coloured through** — the compression preview, the
  palette (format, mode and source) and the Palette Row. Every kind has these,
  so this half lands on any entry. A palette *file* has none to give or take:
  its colours are its own bytes (``docs/design/palette-editing.md`` §2).

The palette source is written the way the project file writes one
(:func:`~celpix.project.projectfile._palette_dict`, absolute-pathed, as the
entry clipboard is), and an ENTRY palette's source entry is position ``0`` of
the payload — the window keeps the object beside the copy, since an entry is
not a value (:class:`~celpix.ui.clipboard._JsonFlavour`). Qt-free; the window
puts the payload on the system clipboard.
"""

from __future__ import annotations

from dataclasses import dataclass

from celpix.core.arrangement import BLOCK_ORDERS
from celpix.project.workspace import PaletteMode, PaletteSource

#: Bumped only on an incompatible change to the payload below; a copy taken by
#: another build then reads as nothing to paste.
VIEW_SETTINGS_VERSION = 1


@dataclass(frozen=True)
class Arrangement:
    """The five arrangement axes as one answer, the way the undo stack treats
    them (``docs/design/undo-redo.md`` §3): a Pattern preset moves four at once,
    and the Pattern picker re-derives itself from them on restore."""

    block_columns: int = 1
    block_rows: int = 1
    block_order: str = "row"
    two_dimensional: bool = False
    bitmap_width: int = 0


@dataclass(frozen=True)
class PaletteSettings:
    """The palette half: format, mode and where the colours come from."""

    preset_id: str
    mode: PaletteMode
    #: ``None`` for the generated default. An ENTRY source names no entry here
    #: until the paste puts the remembered object on it.
    source: PaletteSource | None
    row: int


@dataclass(frozen=True)
class ViewSettings:
    """One entry's view settings, as Copy View Settings takes them.

    ``tilemap`` says which of the two reading halves is filled: ``pixel_preset_id``,
    ``palette_view_preset_id`` and ``arrangement`` for a pixel entry, and
    ``tilemap_preset_id`` for a map. ``palette`` is ``None`` off a palette file.
    """

    tilemap: bool
    compression_id: str
    palette: PaletteSettings | None
    pixel_preset_id: str = ""
    palette_view_preset_id: str = ""
    arrangement: Arrangement | None = None
    tilemap_preset_id: str = ""

    def reads_onto(self, tilemap: bool) -> bool:
        """Whether the reading half applies to an entry of that kind."""
        return self.tilemap == tilemap


def view_settings_payload(settings: ViewSettings, session: str) -> dict:
    """``settings`` as a clipboard payload, stamped with ``session`` so a paste
    in the copying process can resolve an ENTRY palette's source object."""
    from celpix.project.projectfile import (  # noqa: PLC0415 — circular by nature
        _palette_dict,
    )

    data: dict[str, object] = {
        "version": VIEW_SETTINGS_VERSION,
        "session": session,
        "kind": "tilemap" if settings.tilemap else "pixels",
        "compression_id": settings.compression_id,
    }
    if settings.tilemap:
        data["tilemap_preset_id"] = settings.tilemap_preset_id
    else:
        data["pixel_preset_id"] = settings.pixel_preset_id
        data["palette_view_preset_id"] = settings.palette_view_preset_id
        arrangement = settings.arrangement or Arrangement()
        data["arrangement"] = {
            "block_columns": arrangement.block_columns,
            "block_rows": arrangement.block_rows,
            "block_order": arrangement.block_order,
            "two_dimensional": arrangement.two_dimensional,
            "bitmap_width": arrangement.bitmap_width,
        }
    palette = settings.palette
    if palette is not None:
        entry = palette.source.entry if palette.source is not None else None
        data["palette"] = {
            "preset_id": palette.preset_id,
            "mode": palette.mode.value,
            "row": palette.row,
            "source": (
                _palette_dict(
                    palette.source,
                    None,
                    {id(entry): 0} if entry is not None else {},
                    palette.mode,
                )
                if palette.source is not None
                else None
            ),
        }
    return data


def view_settings_from_payload(raw: object) -> ViewSettings | None:
    """A payload back into settings; ``None`` for anything unusable.

    Every field is checked rather than trusted — a payload arrives from outside
    the process — and a part that does not parse falls back to what an absent
    one means, the way a hand-edited project file is read.
    """
    from celpix.project.projectfile import (  # noqa: PLC0415 — circular by nature
        _int,
        _palette_from,
        _plugin_id,
    )

    if not isinstance(raw, dict) or raw.get("version") != VIEW_SETTINGS_VERSION:
        return None
    tilemap = raw.get("kind") == "tilemap"
    palette = None
    pal = raw.get("palette")
    if isinstance(pal, dict):
        mode = PaletteMode.parse(pal.get("mode"))
        source = _palette_from(pal.get("source"), "")
        palette = PaletteSettings(
            preset_id=_plugin_id(pal.get("preset_id"), ""),
            mode=mode,
            source=source,
            row=max(0, _int(pal.get("row"), 0) or 0),
        )
        if not palette.preset_id:
            palette = None
    compression = _plugin_id(raw.get("compression_id"), "")
    if not compression:
        return None
    if tilemap:
        return ViewSettings(
            tilemap=True,
            compression_id=compression,
            palette=palette,
            tilemap_preset_id=_plugin_id(raw.get("tilemap_preset_id"), ""),
        )
    arr = raw.get("arrangement")
    arr = arr if isinstance(arr, dict) else {}
    order = arr.get("block_order")
    pixel = _plugin_id(raw.get("pixel_preset_id"), "")
    if not pixel:
        return None
    return ViewSettings(
        tilemap=False,
        compression_id=compression,
        palette=palette,
        pixel_preset_id=pixel,
        palette_view_preset_id=_plugin_id(raw.get("palette_view_preset_id"), ""),
        arrangement=Arrangement(
            block_columns=max(1, _int(arr.get("block_columns"), 1) or 1),
            block_rows=max(1, _int(arr.get("block_rows"), 1) or 1),
            block_order=str(order) if order in BLOCK_ORDERS else "row",
            two_dimensional=bool(arr.get("two_dimensional", False)),
            bitmap_width=max(0, _int(arr.get("bitmap_width"), 0) or 0),
        ),
    )
