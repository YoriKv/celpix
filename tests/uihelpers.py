"""Sample files, window builders and small readers the UI suites share.

A helper only lands here once more than one ``test_*.py`` needs it; one used by a
single suite lives in that suite. Fixtures stay in ``conftest.py`` — these are
plain functions, called rather than requested."""

from __future__ import annotations

from celpix.ui.main_window import MainWindow


def _make_snes_file(tmp_path):
    px = tmp_path / "s.4bpp.sfc"
    px.write_bytes(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))  # 8 tiles
    return px


def _combo_ids(combo) -> list:
    """A format picker's actual choices — its rows minus the category headings."""
    return [
        combo.itemData(row) for row in range(combo.count()) if not combo.is_heading(row)
    ]


def _drag_payload(*paths):
    from PySide6.QtCore import QMimeData, QUrl

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return mime


def _section_names(panel) -> list[str]:
    """The section headings on screen, top to bottom."""
    tree = panel._tree
    return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]


def _make_big_snes_file(tmp_path, tiles: int):
    px = tmp_path / "big.4bpp.sfc"
    px.write_bytes(bytes((i * 13 + 1) & 0xFF for i in range(32 * tiles)))
    return px


def _open_big(qtbot, tmp_path, monkeypatch, tiles: int) -> MainWindow:
    from PySide6.QtWidgets import QFileDialog

    px = _make_big_snes_file(tmp_path, tiles)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(px), ""))
    )
    window = MainWindow()
    qtbot.addWidget(window)
    window._open_pixel()
    return window


def _select_address_format(window: MainWindow, entry_id: str) -> None:
    """Pick a dropdown entry by id ('hex', 'custom', or a bank preset id)."""
    combo = window._addr_format
    combo.setCurrentIndex(
        next(
            i
            for i in range(combo.count())
            if getattr(combo.itemData(i), "id", combo.itemData(i)) == entry_id
        )
    )


def _fresh_settings(tmp_path) -> None:
    """An empty QSettings store for this one test, cleared before it starts.

    Distinct from ``conftest._isolate_settings``, which redirects the format and
    path once per session so the suite never touches the developer's real config.
    This narrows that to *this test's* ``tmp_path`` and **clears** what is there,
    which is what a test asserting on a setting's default needs — the session-wide
    redirect keeps whatever an earlier test wrote.
    """
    from PySide6.QtCore import QSettings

    from celpix.ui.widgets import settings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path)
    )
    settings().clear()


def _pattern_name(preset_id: str) -> str:
    from celpix.core.arrangement import ARRANGEMENT_PRESETS

    return next(p.name for p in ARRANGEMENT_PRESETS if p.id == preset_id)


def _select_2d_pattern(window) -> None:
    """Turn on the wide-bitmap walk the way the UI does — via the Pattern preset.

    The width stays editable under a preset (a preset picks the arrangement, not
    one asset's width), so this is all it takes to get at it."""
    window._pattern.setCurrentIndex(window._pattern.findText(_pattern_name("2d")))
    assert window._two_d.isChecked()
    assert window._bitmap_width.isEnabled()


def _scr_file(tmp_path, cells, name="screen.SCR"):
    """A real-shaped screen file whose first cells are ``cells``."""
    from celpix.core.context import PipelineContext
    from celpix.plugins.builtins.scgcad import SCR_SIZE, SIGNATURE
    from celpix.plugins.builtins.tilemap_codec import TilemapCodec
    from celpix.plugins.registry import default_registry

    params = default_registry().preset("preset.tilemap.snes-bg").params
    body = TilemapCodec().encode(cells, params, PipelineContext())
    out = bytearray(SCR_SIZE)
    out[: len(body)] = body
    out[0x2000 : 0x2000 + len(SIGNATURE)] = SIGNATURE
    out[0x2100:] = b"\xff" * 0x200
    path = tmp_path / name
    path.write_bytes(bytes(out))
    return path


def _obj_file(tmp_path, parts, name="sprite.OBJ"):
    """A real-shaped sprite object whose first frame holds ``parts``.

    Each part is ``(x, y, tile)``, ``(x, y, tile, palette_row)`` or
    ``(x, y, tile, palette_row, large)``; the record is built the way the file
    stores one - the row in the attribute word's own bits, the size as the bit
    that picks the object's larger square - so this exercises the container and
    the codec rather than standing in for them.
    """
    from celpix.plugins.builtins.scgcad import OBJ_PAYLOADS, OBJ_SIZE, SIGNATURE

    out = bytearray(OBJ_SIZE)
    for at, (x, y, tile, *rest) in enumerate(parts):
        large = 0x01 if len(rest) > 1 and rest[1] else 0
        head = bytes((0x80 | large, 0, y & 0xFF, x & 0xFF))
        attr = (tile & 0x1FF) | ((rest[0] if rest else 0) & 0x7) << 9
        out[at * 6 : at * 6 + 6] = head + attr.to_bytes(2, "big")
    header = OBJ_PAYLOADS[0]
    out[header : header + len(SIGNATURE)] = SIGNATURE
    path = tmp_path / name
    path.write_bytes(bytes(out))
    return path


def _bound_screen(qtbot, tmp_path, cells):
    """A window with a 4bpp bank at entry 0 and a screen of ``cells`` bound to it.

    Bound through the binding bar's own path, so the seeding and the reload are
    the ones the user's gesture takes.
    """
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    tiles = _make_snes_file(tmp_path)
    window._load_pixel(str(tiles))
    bank = window._workspace.entries[0]
    scr = _scr_file(tmp_path, cells)
    window._load_pixel(str(scr))
    entry = window._workspace.find_file(str(scr))
    window._activate_entry(entry)
    window._rebind_tiles(
        entry, TileSource(mode=TileMode.ENTRY, entry=window._workspace.entries[0])
    )
    return window, bank, entry


def _bound_tilemap(qtbot, tmp_path, cells, maker=None):
    """A tilemap entry bound to a real tile bank, ready to edit.

    A screen file by default. ``maker`` takes a different one for a test that
    needs a free column count: a screen is four pages and its assembly owns Cols,
    so a panel is the fixture for anything that wants to lay cells out its own way.
    """
    from celpix.project.workspace import TileMode, TileSource

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._load_pixel(str((maker or _scr_file)(tmp_path, cells)))
    entry = window._workspace.current
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    window._reload_tilemap(entry)
    return window, entry


def _map_file(tmp_path, entries, name="layout.MAP"):
    """A real-shaped stamp layout whose first entries are ``entries``."""
    from celpix.core.context import PipelineContext
    from celpix.plugins.builtins.scgcad import HEADER, MAP_SIZE, SIGNATURE
    from celpix.plugins.builtins.tilemap_codec import TilemapCodec
    from celpix.plugins.registry import default_registry

    params = default_registry().preset("preset.tilemap.scgcad-map").params
    body = TilemapCodec().encode(entries, params, PipelineContext())
    out = bytearray(MAP_SIZE)
    out[: len(SIGNATURE)] = SIGNATURE
    out[HEADER : HEADER + len(body)] = body
    path = tmp_path / name
    path.write_bytes(bytes(out))
    return path


def _pnl_file(tmp_path, cells, name="panel.PNL"):
    """A real-shaped panel whose first cells are ``cells``."""
    from celpix.core.context import PipelineContext
    from celpix.plugins.builtins.scgcad import HEADER, PNL_SIZE, SIGNATURE
    from celpix.plugins.builtins.tilemap_codec import TilemapCodec
    from celpix.plugins.registry import default_registry

    params = default_registry().preset("preset.tilemap.scgcad-panel").params
    body = TilemapCodec().encode(cells, params, PipelineContext())
    out = bytearray(PNL_SIZE)
    out[: len(SIGNATURE)] = SIGNATURE
    # The exponents a real panel carries at 0x69/0x6A, and what every surveyed
    # panel sets: a 2x2 *stamp*. Not a cell size - the words they sit beside are
    # single 8x8 tiles (`scgcad-formats.md` §3.1) - but the block a layout bound
    # to this panel indexes in.
    out[0x69] = out[0x6A] = 1
    out[HEADER : HEADER + len(body)] = body
    path = tmp_path / name
    path.write_bytes(bytes(out))
    return path


def _cgx_file(tmp_path, size=0x8500, rows=b"", name="bank.CGX", col=(0, 0)):
    """A real-shaped tile bank. ``col`` is the header's (col_half, col_cell)."""
    from celpix.plugins.builtins.scgcad import (
        CGX_BANKS,
        CGX_COL_CELL,
        CGX_COL_HALF,
        HEADER,
        SIGNATURE,
    )

    payload = CGX_BANKS[size][0]
    out = bytearray(size)
    # Distinct tiles so a wrong depth or a stray trailing tile is visible.
    for i in range(payload):
        out[i] = (i * 7 + 1) & 0xFF
    out[payload : payload + len(SIGNATURE)] = SIGNATURE
    out[payload + CGX_COL_HALF], out[payload + CGX_COL_CELL] = col
    if rows:
        out[payload + HEADER : payload + HEADER + len(rows)] = rows
    path = tmp_path / name
    path.write_bytes(bytes(out))
    return path


def _tilemap_file(tmp_path, cells, name="screen.bin"):
    """A raw SNES BG map of ``cells`` — no container, so it opens as a tilemap
    only when asked (File ▸ Open tilemap data)."""
    from celpix.core.context import PipelineContext
    from celpix.plugins.builtins.tilemap_codec import TilemapCodec
    from celpix.plugins.registry import default_registry

    params = default_registry().preset("preset.tilemap.snes-bg").params
    path = tmp_path / name
    path.write_bytes(TilemapCodec().encode(cells, params, PipelineContext()))
    return path


def _bound_to_slice(qtbot, tmp_path, monkeypatch, cells, *, tile_offset=0x400):
    """A window holding a ROM, a slice of its tiles, and a map bound to that
    slice. Returns (window, tilemap entry, slice entry)."""
    from PySide6.QtWidgets import QFileDialog

    from celpix.project.workspace import TileMode, TileSource, new_slice

    rom = tmp_path / "art.bin"
    # Junk, then 64 tiles of 4bpp art — the shape a ripped bank actually has.
    rom.write_bytes(
        bytes(tile_offset) + bytes((i * 7 + 3) & 0xFF for i in range(64 * 32))
    )
    mapfile = _tilemap_file(tmp_path, cells)

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(rom))
    parent = window._workspace.current

    sliced = new_slice(parent.path, "art", offset=tile_offset, length=64 * 32)
    window._workspace.insert(sliced, len(window._workspace.entries))

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(mapfile), ""))
    )
    window._open_tilemap()
    tilemap = window._workspace.current
    tilemap.tile_source = TileSource(mode=TileMode.ENTRY, entry=sliced)
    window._reload_tilemap(tilemap)
    return window, tilemap, sliced
