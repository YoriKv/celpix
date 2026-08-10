"""UI wiring that spans areas: the render bridge, the format pickers,
the compression overlay, the menus and the window's own furniture."""

from __future__ import annotations

from celpix.core.index_grid import IndexGrid
from celpix.core.palette import Palette
from celpix.ui import render_bridge
from celpix.ui.main_window import MainWindow
from uihelpers import _combo_ids, _make_snes_file, _scr_file


def test_render_bridge_maps_indices_to_palette(qtbot) -> None:
    grid = IndexGrid(2, 1, bytearray([1, 0]))
    palette = Palette([0xFF000000, 0xFFFF0000])  # black, red
    image = render_bridge.render(grid, palette)
    assert (image.width(), image.height()) == (2, 1)
    assert image.pixel(0, 0) & 0xFFFFFFFF == 0xFFFF0000  # red
    assert image.pixel(1, 0) & 0xFFFFFFFF == 0xFF000000  # black


def test_render_bridge_subpalette_offset(qtbot) -> None:
    grid = IndexGrid(1, 1, bytearray([0]))
    palette = Palette([0xFF111111, 0xFF222222])
    # base=1 shifts index 0 to palette entry 1.
    image = render_bridge.render(grid, palette, subpalette_base=1)
    assert image.pixel(0, 0) & 0xFFFFFFFF == 0xFF222222


def test_render_bridge_paints_undrawn_positions_black(qtbot) -> None:
    """A map's undrawn positions are painted over the composed picture rather than
    composed into it: every index in the grid belongs to the palette and none is
    reserved, so there is no index that could have meant "nothing here".

    Which forces a format change — Qt cannot paint onto ``Format_Indexed8`` — and
    that is why a map with nothing hidden must come back untouched, still indexed
    and still cheap to re-table on a palette change.
    """
    grid = IndexGrid(2, 1, bytearray([1, 1]))
    palette = Palette([0xFF000000, 0xFFFF0000])  # black, red
    indexed = render_bridge.render(grid, palette)
    assert render_bridge.paint_hidden(indexed, ()) is indexed

    painted = render_bridge.paint_hidden(indexed, ((1, 0, 1, 1),))
    assert painted.pixel(0, 0) & 0xFFFFFFFF == 0xFFFF0000  # the cell that draws
    assert painted.pixel(1, 0) & 0xFFFFFFFF == 0xFF000000  # opaque, not a hole


def test_render_bridge_transparent_zero_clears_every_rows_index_0(qtbot) -> None:
    """Which entries mean "index 0" depends on where the palette row lives.

    Unpinned, the table is already one row and only entry 0 is an index 0. Pinned,
    the row is folded into the indices, so every ``row_stride``-th entry is some
    row's index 0 — clearing entry 0 alone would leave the blank pixels of every
    row but the first opaque, which is most of a map.
    """
    palette = Palette([0xFF000000 | i for i in range(64)])

    # Unpinned: index 0 goes clear, and the entry one row up does not.
    grid = IndexGrid(2, 1, bytearray([0, 16]))
    image = render_bridge.render(grid, palette, transparent_zero=True)
    assert image.pixel(0, 0) >> 24 == 0
    assert image.pixel(1, 0) >> 24 == 0xFF

    # Pinned at 16 colours a row: rows 0, 1 and 3's zeros all clear, and a
    # non-zero index inside a row keeps its colour.
    grid = IndexGrid(4, 1, bytearray([0, 16, 48, 17]))
    image = render_bridge.render_pinned(grid, palette, 16, transparent_zero=True)
    assert [image.pixel(x, 0) >> 24 for x in range(4)] == [0, 0, 0, 0xFF]
    assert image.pixel(3, 0) & 0xFFFFFF == 17

    # Off, nothing is cleared — the same call is the old behaviour exactly.
    image = render_bridge.render_pinned(grid, palette, 16)
    assert all(image.pixel(x, 0) >> 24 == 0xFF for x in range(4))


def test_render_bridge_empty_grid_is_null(qtbot) -> None:
    assert render_bridge.render(IndexGrid(0, 0), Palette([])).isNull()


def test_render_bridge_argb_grid(qtbot) -> None:
    # Direct-color grids render straight to ARGB32, ignoring the palette.
    from celpix.core.argb_grid import ArgbGrid

    grid = ArgbGrid(2, 1)
    grid.set(0, 0, 0xFF112233)
    grid.set(1, 0, 0xFF445566)
    image = render_bridge.render(grid, Palette([]))
    assert (image.width(), image.height()) == (2, 1)
    assert image.pixel(0, 0) & 0xFFFFFFFF == 0xFF112233
    assert image.pixel(1, 0) & 0xFFFFFFFF == 0xFF445566


def _write_planar_preset(dirpath, bpp: int) -> None:
    # One 8x8 planar preset at the given bpp (bytes/tile = 8*bpp). Geometry is the
    # engine's fixed unit, so a preset is only bpp + plane offsets. Pixel presets
    # live in the pixel/ subfolder of the plugin root (the folder gives the stage).
    planes = {
        1: "[ { base = 0, stride = 1 } ]",
        2: "[ { base = 0, stride = 1 }, { base = 8, stride = 1 } ]",
    }[bpp]
    pixel_dir = dirpath / "pixel"
    pixel_dir.mkdir(exist_ok=True)
    (pixel_dir / "custom.toml").write_text(
        "id = 'preset.pixel.custom'\n"
        "name = 'Custom'\n"
        "engine_id = 'codec.pixel.planar'\n"
        "[params]\n"
        f"bpp = {bpp}\n"
        f"planes = {planes}\n"
    )


def test_refresh_reloads_edited_preset_and_reruns(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QFileDialog

    from celpix.plugins.discovery import load_user_plugins
    from celpix.plugins.registry import default_registry

    plugdir = tmp_path / "plugins"
    plugdir.mkdir()
    _write_planar_preset(plugdir, bpp=1)  # 8 bytes/tile
    data_file = tmp_path / "d.bin"
    data_file.write_bytes(bytes(64))  # 64 bytes

    def reload(project_path=None):
        reg = default_registry()
        return reg, load_user_plugins(reg, [str(plugdir)])

    registry, _ = reload()
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: (str(data_file), "")),
    )
    window = MainWindow(registry=registry, reload_plugins=reload)
    qtbot.addWidget(window)

    # Select the dropped preset and open: 64 bytes / 8 bytes-per-tile = 8 tiles.
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.custom")
    )
    window._open_pixel()
    assert window._doc.tile_count == 8

    # Edit the preset on disk (bpp 1 -> 2, so 16 bytes/tile) and refresh: the open
    # file is re-decoded through the reloaded preset. 64 / 16 = 4 tiles.
    _write_planar_preset(plugdir, bpp=2)
    window._refresh_plugins()
    assert window._doc.tile_count == 4


def test_project_folder_plugins_load_and_unload_with_the_project(
    qtbot, tmp_path
) -> None:
    from celpix.plugins.discovery import load_user_plugins, project_plugin_dir
    from celpix.plugins.registry import default_registry
    from celpix.project.projectfile import save_project
    from celpix.project.workspace import EntrySession, Workspace

    # A project whose folder carries a plugins/ root of its own: one 1bpp preset
    # (8 bytes/tile) that no other root provides, and an entry saved against it.
    proj_dir = tmp_path / "hack"
    (proj_dir / "plugins").mkdir(parents=True)
    _write_planar_preset(proj_dir / "plugins", bpp=1)
    data_file = proj_dir / "d.bin"
    data_file.write_bytes(bytes(64))  # 64 bytes -> 8 tiles at 8 bytes each

    ws = Workspace()
    entry = ws.open_file(str(data_file))
    entry.session = EntrySession(
        pixel_preset_id="preset.pixel.custom",
        palette_preset_id="preset.palette.bgr555",
    )
    ws.set_current(entry)
    project = proj_dir / "hack.celpix"
    save_project(ws, str(project))

    user_dir = tmp_path / "user-plugins"  # empty: the preset can only come from
    user_dir.mkdir()  # the project's own folder

    def reload(project_path=None):
        reg = default_registry()
        dirs = [str(user_dir)]
        project_plugins = project_plugin_dir(project_path)
        if project_plugins is not None:
            dirs.append(project_plugins)
        return reg, load_user_plugins(reg, dirs)

    registry, _ = reload()
    window = MainWindow(registry=registry, reload_plugins=reload)
    qtbot.addWidget(window)
    assert window._pixel_preset.findData("preset.pixel.custom") < 0

    # Opening the project registers its plugins *before* the restored entry is
    # shown, so the entry decodes through the project's own preset.
    window._load_project(str(project))
    assert window._pixel_preset.findData("preset.pixel.custom") >= 0
    assert window._doc.tile_count == 8

    # Closing the project takes them with it.
    window._new_project()
    assert window._pixel_preset.findData("preset.pixel.custom") < 0


def test_the_format_pickers_open_grouped_and_never_on_a_heading(
    qtbot, tmp_path
) -> None:
    """The wiring, checked on the live window rather than on the widget: every
    picker filled from the registry has to come up grouped, with a real format
    selected — a heading landing in ``currentData`` would be passed to the
    pipeline as a preset id.
    """
    from celpix.plugins.base import NO_COMPRESSION

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))

    for combo in (window._pixel_preset, window._palette_preset, window._compression):
        headings = [i for i in range(combo.count()) if combo.is_heading(i)]
        assert headings, combo.toolTip()
        assert not combo.is_heading(combo.currentIndex())
        assert isinstance(combo.currentData(), str)

    # The pass-through leads the compression list: it names no category, so it
    # sits above every heading, which is where the default belongs.
    assert window._compression.itemData(0) == NO_COMPRESSION
    assert window._compression_id() == NO_COMPRESSION


def test_the_cell_format_picker_tags_each_entry_with_its_layout(
    qtbot, tmp_path
) -> None:
    """Which of the three layouts a cell format declares decides what the entry
    *is* — a grid map, a sprite map or a fontmap, each with a different bar under
    it — and the names carry that unevenly, so the picker says it outright."""
    from celpix.core.tilemap import Cell

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1)])))
    combo = window._tilemap_preset

    tagged = {
        combo.itemData(row): combo.itemText(row)
        for row in range(combo.count())
        if not combo.is_heading(row)
    }

    assert tagged["preset.tilemap.snes-bg"].startswith("[T] ")
    assert tagged["preset.tilemap.scgcad-object"].startswith("[S] ")
    assert tagged["preset.tilemap.text-8bit"].startswith("[F] ")
    # Every entry carries one, so the column reads as a column rather than as
    # some formats being annotated and the rest not.
    assert all(text[:3] in ("[T]", "[S]", "[F]") for text in tagged.values())
    # …and the tag stays out of what the Undo menu says.
    assert window._preset_name("preset.tilemap.snes-bg") == "SNES BG map (16-bit)"


def test_pixel_filter_prunes_the_dropdown_without_losing_the_view(
    qtbot, tmp_path
) -> None:
    """The view-only format filter: what it hides, what it refuses to hide, and
    what survives it.

    One window walks the whole feature because every step asks the same question
    of the same dropdown - which formats are listed, and is the shown one still
    among them - and the invariant that matters is exactly that no combination of
    hiding leaves the view stranded on a format the list no longer offers.
    """
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    combo = window._pixel_preset
    full = len(_combo_ids(combo))
    assert full > 2
    current = combo.currentData()
    other = next(i for i in _combo_ids(combo) if i != current)

    # Hiding everything else leaves the two, with the shown one untouched.
    assert window._apply_pixel_filter({current, other}) == {current, other}
    assert set(_combo_ids(combo)) == {current, other}
    assert combo.currentData() == current
    # The popup model marks exactly the visible formats checked.
    assert {key for key, _name, checked in window._pixel_filter_items() if checked} == {
        current,
        other,
    }

    # A repopulation (plugin reload) keeps the filter and the shown format.
    window._repopulate_presets()
    assert set(_combo_ids(combo)) == {current, other}
    assert combo.currentData() == current

    # Select None can't empty the list; it collapses to the current format.
    assert window._apply_pixel_filter(set()) == {current}
    assert _combo_ids(combo) == [current]
    assert combo.currentData() == current

    # Select All brings every format back and clears the filter.
    window._apply_pixel_filter({p.id for p in window._all_pixel_presets()})
    assert len(_combo_ids(combo)) == full
    assert not window._workspace.hidden_pixel_presets

    # Unchecking the *shown* format moves the view to the remaining one - an
    # ordinary undoable switch, so undo restores the old format and force-shows
    # it even though the filter had hidden it.
    window._apply_pixel_filter({other})
    assert combo.currentData() == other
    assert window._doc.pixel_config.interpret_preset_id == other
    window._undo_stack.undo()
    assert window._doc.pixel_config.interpret_preset_id == current
    assert combo.currentData() == current


def test_pixel_filter_is_saved_and_restored_with_the_project(qtbot, tmp_path) -> None:
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    combo = window._pixel_preset
    current = combo.currentData()
    other = next(i for i in _combo_ids(combo) if i != current)
    window._apply_pixel_filter({current, other})

    project = tmp_path / "s.celpix"
    window._save_project_to(str(project))
    assert not window._project_is_dirty()  # the filter is now part of the baseline

    other_window = MainWindow()
    qtbot.addWidget(other_window)
    other_window._load_project(str(project))
    # The restored project prunes the dropdown to the saved formats.
    restored = other_window._pixel_preset
    assert set(_combo_ids(restored)) == {current, other}
    assert other_window._workspace.hidden_pixel_presets == (
        window._workspace.hidden_pixel_presets
    )
    assert not other_window._project_is_dirty()


def test_compression_overlay_shows_the_decoded_window_beside_the_raw_one(
    qtbot, tmp_path
) -> None:
    """When the preview appears, what it shows, and when it refuses to.

    The overlay is a second, parallel run of the pipeline, and the thing that can
    break in it is divergence from the live view - it must decode the same window,
    lay it out through the same arrangement path, and leave the raw view it sits
    beside completely alone. So one window checks all three against one file.
    """
    from celpix.plugins.builtins import lz_command

    # An LZ2 structure of 4 distinct SNES 4bpp tiles, plus trailing bytes: the
    # main view keeps showing the raw file, the overlay the decompressed tiles.
    tiles = bytes((i * 29 + 5) & 0xFF for i in range(32 * 4))
    px = tmp_path / "packed.bin"
    px.write_bytes(lz_command.compress(tiles, big_endian_offsets=True) + bytes(64))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    assert not window._overlay.isVisible()
    raw_image = window._canvas._image.copy()

    window._compression.setCurrentIndex(window._compression.findData("compression.lz2"))
    assert window._overlay.isVisible()
    assert not window._overlay._canvas._image.isNull()
    # The parallel run leaves the main (raw) view and its config untouched.
    assert window._canvas._image == raw_image
    assert window._doc.pixel_config.compression_id == "compression.none"

    # Viewed 2 tiles wide, both a 2D wide-bitmap read and a 1×2 block grouping
    # re-lay the preview - proving it runs the live arrangement path, not a 1D fork.
    window._columns.setValue(2)
    flat = window._overlay._canvas._image.copy()
    window._two_d.setChecked(True)
    assert window._overlay._canvas._image != flat
    window._two_d.setChecked(False)
    assert window._overlay._canvas._image == flat  # back to the 1D preview
    window._block_rows.setValue(2)
    assert window._overlay._canvas._image != flat
    window._block_rows.setValue(1)

    window._compression.setCurrentIndex(
        window._compression.findData("compression.none")
    )
    assert not window._overlay.isVisible()

    # Data no scheme can claim: a leading backreference into unwritten output can
    # never start a valid structure, so there is nothing to preview.
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"\x83\xff\xff" * 22)
    window._load_pixel(str(junk))
    window._compression.setCurrentIndex(window._compression.findData("compression.lz2"))
    assert not window._overlay.isVisible()


def test_compression_overlay_badge_distinguishes_the_three_decode_states(
    qtbot, tmp_path
) -> None:
    # The preview looks equally finished whether the decompressor reached a
    # structure's end, ran out of window, or was never going to find an end at
    # all - so the status bar's badge is the only thing telling the three apart.
    # The distinction that matters: a scheme *with* an end marker that missed it
    # was cut short (amber, fixable by widening); a stream-based one simply
    # decodes as far as it is fed (plain text, nothing to fix).
    from celpix.plugins.builtins import lz_command, packbits

    tiles = bytes((i * 29 + 5) & 0xFF for i in range(32 * 4))
    ended = tmp_path / "lz2.bin"
    ended.write_bytes(lz_command.compress(tiles, big_endian_offsets=True) + bytes(64))
    endless = tmp_path / "packbits.bin"
    endless.write_bytes(packbits.compress(tiles))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(ended))
    window._compression.setCurrentIndex(window._compression.findData("compression.lz2"))
    assert window._overlay.isVisible()
    assert not window._overlay._badge.isVisible()  # terminator inside the window

    # Same file, a window too small to hold the structure: now it *was* cut
    # short, which is the warning case.
    window._rows.setValue(1)
    window._columns.setValue(1)
    assert window._overlay._badge.isVisible()
    assert window._overlay._badge.text() == "end not in view"
    assert "#c08a30" in window._overlay._badge.styleSheet()

    # A scheme with no end marker: informational, not amber, whatever the window.
    window._load_pixel(str(endless))
    window._compression.setCurrentIndex(
        window._compression.findData("compression.packbits")
    )
    assert window._overlay._badge.isVisible()
    assert window._overlay._badge.text() == "end of view window"
    assert window._overlay._badge.styleSheet() == ""
    # Hard-wrapped so the tooltip doesn't run off the screen edge.
    assert "\n" in window._overlay._badge.toolTip()


def test_promote_bounds_a_stream_scheme_at_the_window_end(qtbot, tmp_path) -> None:
    # A scheme with no end marker never yields a structure extent, so the only
    # extent on offer is where the view window ran out - which To Slice uses, so
    # a PackBits stream can still be promoted into an editable entry. Jump stays
    # off: with no end found there is no "byte after this structure".
    from celpix.plugins.builtins import packbits
    from celpix.project.workspace import EntryKind

    # One 32-byte run per tile, so every tile is exactly one 2-byte PackBits
    # packet: any window then cuts on a packet boundary *and* on a whole tile,
    # which is what lets this test bound the window without the preview simply
    # failing to decode. 128 tiles compress to 256 B - twice the window below.
    packed = packbits.compress(b"".join(bytes([i]) * 32 for i in range(128)))
    px = tmp_path / "packbits.bin"
    px.write_bytes(packed)

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._columns.setValue(2)
    window._rows.setValue(2)
    window._compression.setCurrentIndex(
        window._compression.findData("compression.packbits")
    )
    assert window._promote_button.isEnabled()
    assert not window._jump_next.isEnabled()

    start, extent = window._structure_extent
    window_bytes = 2 * 2 * window._doc.bytes_per_tile
    assert (start, extent) == (0, window_bytes)  # bounded by the window's end
    assert extent < len(packed)  # and the stream really does run on past it

    window._on_promote_structure()
    entry = window._workspace.entries[-1]
    assert entry.kind is EntryKind.SLICE
    assert (entry.slice_offset, entry.slice_length) == (start, extent)
    assert entry.compression_id == "compression.packbits"


def test_copier_header_is_detected_and_skipped(qtbot, tmp_path) -> None:
    """A headered ROM opens on the cartridge, not on the copier header, and its
    addresses count from the file — the container decides both, with no setting
    for the user to get wrong.

    Sized past the copier rule's floor on purpose: the same arithmetic on a small
    file is a tile sheet, not a header (see test_small_rom_sized_file_is_not_headered).
    """
    header = bytes(range(256)) * 2  # 512 bytes of copier metadata
    body = bytes((i * 13 + 1) & 0xFF for i in range(0x8000))
    px = tmp_path / "rom.sfc"
    px.write_bytes(header + body)

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))

    assert window._workspace.current.container_id == "container.copier-header"
    assert bytes(window._doc.pixel_data) == body
    # Offsets stay file-absolute, so ROM addresses still mean what they say.
    assert window._display_base() == 512
    assert window._offset_text() == "0x000200"


def test_side_panels_claim_canvas_editing_shortcuts(qtbot) -> None:
    # The palette grid and the hex dump are shortcut islands: while focused they
    # claim the canvas editing keys (Cut/Copy/Paste/Select All/Delete) so those
    # window-wide shortcuts act on the panel (or nothing), never the canvas
    # selection behind the dock. Accepting the ShortcutOverride is the mechanism.
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    from celpix.ui.hex_view_panel import HexViewPanel
    from celpix.ui.palette_panel import PalettePanel

    def claims(widget, key, mods=Qt.KeyboardModifier.NoModifier) -> bool:
        event = QKeyEvent(QEvent.Type.ShortcutOverride, key, mods)
        QApplication.sendEvent(widget, event)
        return event.isAccepted()

    palette = PalettePanel()
    qtbot.addWidget(palette)
    hex_view = HexViewPanel()
    qtbot.addWidget(hex_view)
    ctrl = Qt.KeyboardModifier.ControlModifier
    for widget in (palette, hex_view._view):
        assert claims(widget, Qt.Key.Key_C, ctrl)  # Copy
        assert claims(widget, Qt.Key.Key_X, ctrl)  # Cut
        assert claims(widget, Qt.Key.Key_A, ctrl)  # Select All
        assert claims(widget, Qt.Key.Key_Delete)  # Clear/Delete
        # A key the canvas doesn't bind is left alone, so normal handling stands.
        assert not claims(widget, Qt.Key.Key_B)


def test_every_input_has_a_tooltip_shared_with_its_label(qtbot) -> None:
    """Every control the user can operate explains itself on hover, and a caption
    answers with the same text as the input it names.

    A caption is half the hover target of the pair and is often where people
    point first, so a tooltip on the input alone reads as "no tooltip". Both
    halves are checked here because they are set at separate call sites and
    silently drift apart otherwise — the failure is invisible in any screenshot.
    """
    from PySide6.QtWidgets import (
        QAbstractSpinBox,
        QCheckBox,
        QComboBox,
        QLabel,
        QLineEdit,
        QSlider,
    )

    window = MainWindow()
    qtbot.addWidget(window)
    inputs = (QAbstractSpinBox, QComboBox, QLineEdit, QCheckBox, QSlider)

    # findChildren takes a single type, so sweep everything and filter here.
    untooltipped = [
        type(widget).__name__
        for widget in window.findChildren(object)
        if isinstance(widget, inputs)
        # Spin boxes and combos embed their own QLineEdit; that Qt internal is
        # covered by the tooltip on the control that owns it.
        and not isinstance(widget.parent(), (QAbstractSpinBox, QComboBox))
        and not widget.toolTip()
    ]
    assert untooltipped == []

    # A caption declares what it names via setBuddy, so the pairing is checkable.
    mismatched = [
        label.text()
        for label in window.findChildren(QLabel)
        if label.buddy() is not None and label.toolTip() != label.buddy().toolTip()
    ]
    assert mismatched == []


def test_the_help_menu_builds_its_dialogs_and_documents_both_key_styles(
    qtbot,
) -> None:
    """Help's two actions build their dialogs (About reads a bundled icon), and the
    guide they show is generated from the live menu bar - so both ways a key can be
    declared have to land in it.

    A real ``QKeySequence`` (Edit > Copy) and the tab-in-the-label form the bare-key
    nav actions use reach the guide by different routes; Undo's label is pinned
    because the undo stack renames it at runtime; and an action with no key at all
    must not become a blank row.
    """
    from PySide6.QtGui import QKeySequence
    from PySide6.QtWidgets import QLabel

    from celpix import __version__
    from celpix.ui.help_dialogs import AboutDialog, ShortcutGuide, shortcut_sections
    from celpix.ui.tools import TOOL_SPECS, TRANSFORM_SPECS

    window = MainWindow()
    qtbot.addWidget(window)

    # Hold the menubar's action list: dropping it collects the QAction wrappers,
    # and the submenu each owns goes with them.
    bar_actions = window.menuBar().actions()
    # "&" is the mnemonic marker, not part of the name.
    help_menu = next(a.menu() for a in bar_actions if a.text() == "&Help")
    for action in help_menu.actions():
        if not action.isSeparator():
            action.trigger()  # conftest stops exec() from blocking

    assert window.findChildren(ShortcutGuide)
    about = window.findChildren(AboutDialog)
    assert about
    blurb = " ".join(label.text() for label in about[0].findChildren(QLabel))
    assert "Epi" in blurb and __version__ in blurb

    sections = dict(shortcut_sections(window))
    copy_keys = QKeySequence(QKeySequence.StandardKey.Copy).toString(
        QKeySequence.SequenceFormat.NativeText
    )
    assert dict(sections["Edit"])["Copy"] == copy_keys
    assert dict(sections["Navigate"])["Next tile"] == "Right"
    assert "Undo" in dict(sections["Edit"])  # not "Undo <command name>"
    # An action carrying both routes lists both; the toolbar's own switches are
    # documented because they were given a menu home.
    assert dict(sections["View"])["Zoom In"].endswith("/ Ctrl + Scroll Up")
    # Keys are rendered as native text, so the spelling is platform-dependent
    # ("Shift+R" everywhere, "⇧R" on macOS).
    assert dict(sections["Edit"])["Show Rearranged Tiles"] == QKeySequence(
        "Shift+R"
    ).toString(QKeySequence.SequenceFormat.NativeText)
    assert dict(sections["Pixel Tools"]) == {s.label: s.key for s in TOOL_SPECS}
    transform_keys = dict(sections["Transform"])
    assert all(transform_keys[s.label] == s.key for s in TRANSFORM_SPECS)
    assert all(name and keys for entries in sections.values() for name, keys in entries)


def test_menu_mnemonics_never_collide(qtbot, tmp_path, opened_menus) -> None:
    """No two entries of one menu answer to the same mnemonic letter.

    A clash is silent - Qt cycles between the entries instead of activating one -
    and the letters are hand-picked per menu, so adding an entry anywhere is what
    breaks it. Every right-click menu is covered as well as the menu bar: the
    files panel builds a different one per entry kind, and the canvas menu shares
    its actions with the Edit, File and Palette menus, which is what makes the
    letters awkward to keep unique in the first place.
    """
    import re

    from PySide6.QtCore import QPoint, Qt

    def clashes(menu, path: str) -> list[str]:
        found: list[str] = []
        seen: dict[str, str] = {}
        for action in menu.actions():
            if action.isSeparator():
                continue
            label = action.text().split("\t", 1)[0]
            mnemonic = re.search(r"&(\w)", label)
            if mnemonic is not None:
                key = mnemonic.group(1).lower()
                if key in seen:
                    found.append(f"{path}: {key!r} is {seen[key]!r} and {label!r}")
                seen[key] = label
            submenu = action.menu()
            if submenu is not None:
                found += clashes(submenu, f"{path} > {label}")
        return found

    px = _make_snes_file(tmp_path)
    pal = tmp_path / "colors.pal"
    pal.write_bytes(bytes(range(32)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._workspace.add_slice(str(px), "slice", 64, 64)
    window._add_palette_file(str(pal))
    window._new_bookmark_current()

    found: list[str] = []
    for action in window.menuBar().actions():
        found += clashes(action.menu(), action.text())

    # One right-click menu per entry kind, taken off the tree so each is built
    # the way the panel builds it.
    tree = window._files_panel._tree
    kinds = set()

    def visit(item) -> None:
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if entry is not None and entry.kind not in kinds:
            kinds.add(entry.kind)
            window._files_panel._show_menu(tree.visualItemRect(item).center())
            found.extend(clashes(opened_menus[-1], f"files:{entry.kind.name}"))
        for i in range(item.childCount()):
            visit(item.child(i))

    for i in range(tree.topLevelItemCount()):
        visit(tree.topLevelItem(i))
    assert len(kinds) == 4  # file, slice, palette, bookmark - all four covered

    window._show_canvas_menu(QPoint(4, 4))
    found += clashes(opened_menus[-1], "canvas")
    window._show_palette_menu(QPoint(4, 4))
    found += clashes(opened_menus[-1], "palette grid")

    assert not found, "\n".join(found)


def test_a_moved_panel_is_written_out_and_comes_back_on_the_next_window(
    qtbot, monkeypatch
) -> None:
    """The layout a user arrives at is their answer to a question the defaults
    only guess at, so rebuilding it every launch is a tax paid dozens of times.

    Written on a delay rather than at quit, and both halves of that matter: a
    dock separator dragged to a new width emits no signal to hang a save off, and
    a layout kept only until a clean shutdown is a layout lost to a crash. So no
    save is asked for here - the move alone has to reach the settings. The delay
    is shortened to keep the test off the wall clock; what it is set to is a
    tuning value, not behaviour.
    """
    from PySide6.QtCore import Qt

    from celpix.ui import window_layout

    monkeypatch.setattr(window_layout, "SAVE_DELAY_MS", 10)
    first = MainWindow()
    qtbot.addWidget(first)
    first.show()
    qtbot.waitUntil(lambda: _stored_layout() is not None, timeout=3000)
    settled = _stored_layout()

    # Both a panel moved and a panel opened: Qt's state carries visibility too,
    # and the Hex dock is the one that starts hidden.
    first.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, first._hex_dock)
    first._hex_dock.show()
    # Waited for the write the *move* caused, not the one opening the window did,
    # so what the second one reads below cannot be the layout from before it.
    qtbot.waitUntil(lambda: _stored_layout() != settled, timeout=3000)

    second = MainWindow()
    qtbot.addWidget(second)
    second.show()
    assert (
        second.dockWidgetArea(second._hex_dock) == Qt.DockWidgetArea.RightDockWidgetArea
    )
    assert second._hex_dock.isVisible()
    # The one that was left alone is still where its own code put it, rather than
    # the restore having flattened everything into one remembered arrangement.
    assert (
        second.dockWidgetArea(second._files_dock)
        == Qt.DockWidgetArea.LeftDockWidgetArea
    )


def test_a_tool_window_keeps_the_size_it_was_given(qtbot) -> None:
    """The floating windows are remembered the same way, minus the docks they
    have none of: the text window is where a fontmap is actually typed, and its
    default size is a starting point rather than an answer.

    Its position counts as set once it is remembered, so the first-show placement
    beside the main window stops overriding it — that move is for a window nobody
    has put anywhere yet."""
    from celpix.ui.text_window import TextWindow

    first = TextWindow()
    qtbot.addWidget(first)
    first.show()
    first.resize(600, 500)
    first._layout_memory.save()

    second = TextWindow()
    qtbot.addWidget(second)
    assert second.size().toTuple() == (600, 500)
    assert second._positioned


def _stored_layout():
    """The main window's saved dock arrangement, or None with nothing written."""
    from celpix.ui.main_window.window import MAIN_WINDOW_LAYOUT_KEY
    from celpix.ui.widgets import settings

    return settings().value(f"{MAIN_WINDOW_LAYOUT_KEY}/state")


def test_reset_panel_layout_is_the_way_back_from_a_panel_dragged_anywhere(
    qtbot,
) -> None:
    """The escape hatch the remembering makes necessary: a panel dropped
    somewhere unusable now stays unusable across a restart, and a dock shrunk
    past its own separator cannot be dragged back by hand.

    It puts the *panels* back and leaves the window where it is - resetting the
    size and position too would be answering a question nobody asked."""
    from PySide6.QtCore import Qt

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, window._files_dock)
    window._hex_dock.show()
    was = window.size()

    window._reset_panel_layout()
    assert (
        window.dockWidgetArea(window._files_dock)
        == Qt.DockWidgetArea.LeftDockWidgetArea
    )
    assert not window._hex_dock.isVisible()  # hidden again, as a fresh install has it
    assert window.size() == was

    # And the reset is what a following window reads, rather than the arrangement
    # it replaced still sitting in the settings waiting to come back.
    other = MainWindow()
    qtbot.addWidget(other)
    other.show()
    assert (
        other.dockWidgetArea(other._files_dock) == Qt.DockWidgetArea.LeftDockWidgetArea
    )


def test_a_project_naming_a_missing_format_opens_on_the_default_and_says_so(
    qtbot, tmp_path, captured_alerts
) -> None:
    """The plugin that supplied a format can be gone by the next launch — the
    project's own folder was closed, the plugin was deleted or left untrusted.
    The entry has to open on a format this build does have, with the swap said
    out loud: it is now what a save would write.
    """
    from celpix.core.capabilities import ContentKind
    from celpix.project.workspace import EntrySession

    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px), content_kind=ContentKind.TILEMAP)
    entry = window._workspace.current
    entry.tilemap_preset_id = "preset.tilemap.gone"
    entry.session = EntrySession(
        pixel_preset_id="preset.pixel.gone",
        palette_preset_id="preset.palette.bgr555",
    )
    project = tmp_path / "p.celpix"
    window._save_project_to(str(project))

    window._load_project(str(project))

    restored = window._workspace.current
    assert restored.tilemap_preset_id == "preset.tilemap.snes-bg"
    assert restored.session.pixel_preset_id == "preset.pixel.snes-4bpp"
    assert any(title == "celPix - missing formats" for title, _msg in captured_alerts)


def test_a_declined_code_plugin_is_not_reported_as_a_failure(
    qtbot, captured_alerts
) -> None:
    """Saying no at the trust prompt is a decision, not a breakage.

    The decline is recorded every launch and every F5 for as long as the answer
    stands, so counting it among the failures put a "plugins failed to load"
    modal in front of the user forever. It still has to be *findable* — the
    status bar says how many are sitting there — and it still rides along in a
    real failure's details, since that list is "what is not running".
    """
    from celpix.plugins.discovery import PluginLoadIssue

    window = MainWindow(
        plugin_issues=[PluginLoadIssue("/p/mine.py", "declined", declined=True)]
    )
    qtbot.addWidget(window)

    window._alert_plugin_issues()
    assert not captured_alerts
    assert "not run" in window.statusBar().currentMessage()

    window._plugin_issues.append(PluginLoadIssue("/p/broken.py", "boom"))
    window._alert_plugin_issues()
    ((_title, message),) = captured_alerts
    assert message.startswith("1 plugin failed to load")  # the decline is not one
