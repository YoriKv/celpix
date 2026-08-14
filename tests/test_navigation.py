"""Moving around a file: the offset box and its address format,
the position bar, the arrangement controls and the canvas they lay out."""

from __future__ import annotations

from celpix.core.arrangement import ARRANGEMENT_PRESETS
from celpix.ui.main_window import MainWindow
from uihelpers import (
    _fresh_settings,
    _make_snes_file,
    _open_big,
    _pattern_name,
    _select_2d_pattern,
    _select_address_format,
)


def test_navigation_steps_by_row_and_tile_and_clamps(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)  # 32-tile page; last page top-left = 64 - 32 = 32.
    assert window._offset == 0

    window._nav_rows(1)  # down one row = +columns tiles
    assert window._offset == 16
    window._nav_tiles(1)  # right one tile
    assert window._offset == 17
    window._nav_tiles(-1)
    window._nav_rows(-1)
    assert window._offset == 0

    # Both ends hold: the top absorbs an up-step, the end absorbs an over-scroll.
    window._nav_rows(-1)
    assert window._offset == 0
    window._nav_end()
    assert window._offset == 32
    window._nav_rows(5)
    assert window._offset == 32
    window._nav_home()
    assert window._offset == 0


def test_typing_hex_offset_jumps_byte_exact(qtbot, tmp_path, monkeypatch) -> None:
    # Integration of the offset box with the window: commit -> jump -> normalised
    # display. Byte-exact: a sub-tile address becomes the grid's byte nudge. The
    # hex-form variants (0x/bare/$) are covered by the test_address unit tests;
    # the jump path is form-independent.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)
    window._address_edit.setText("0x210")  # tile 16 plus a 16-byte nudge
    window._address_edit.commit()
    assert (window._offset, window._nudge) == (16, 16)
    assert window._address_edit.text() == "0x000210"  # normalised, byte-exact
    # Past the end clamps to the last full page, which sits on the tile grid.
    window._address_edit.setText("0xFFFF")
    window._address_edit.commit()
    assert (window._offset, window._nudge) == (32, 0)
    assert window._address_edit.text() == "0x000400"


def test_byte_nudge_steps_wrap_and_clamp(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)

    window._nav_bytes(1)
    assert (window._offset, window._nudge) == (0, 1)
    assert window._nudge_info.text() == "+1 B"
    assert window._address_edit.text() == "0x000001"
    # Tile-based moves keep the nudge — it is alignment, not position.
    window._nav_rows(1)
    assert (window._offset, window._nudge) == (16, 1)
    # A byte step back wraps across the tile boundary.
    window._nav_bytes(-2)
    assert (window._offset, window._nudge) == (15, 31)
    # Home keeps the alignment; stepping below byte 0 clamps to the file start.
    window._nav_home()
    assert (window._offset, window._nudge) == (0, 31)
    window._nav_bytes(-40)
    assert (window._offset, window._nudge) == (0, 0)
    # And the origin can't nudge past the last full page.
    window._nav_end()
    window._nav_bytes(1)
    assert (window._offset, window._nudge) == (32, 0)
    # The 0B button clears the nudge without moving the tile origin.
    from PySide6.QtWidgets import QPushButton

    window._set_byte_position(16 * 32 + 5)
    assert (window._offset, window._nudge) == (16, 5)
    next(b for b in window.findChildren(QPushButton) if b.text() == "0B").click()
    assert (window._offset, window._nudge) == (16, 0)

    # Ctrl+Left/Right and 0 route to the byte actions (Ctrl passes the filter).
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(QApplication, "focusWidget", staticmethod(lambda: None))

    def press(key, mods=Qt.KeyboardModifier.NoModifier):
        return window._handle_nav_key(QKeyEvent(QEvent.Type.KeyPress, key, mods))

    ctrl = Qt.KeyboardModifier.ControlModifier
    assert press(Qt.Key.Key_Right, ctrl) is True
    assert (window._offset, window._nudge) == (16, 1)
    assert press(Qt.Key.Key_Left, ctrl) is True
    assert (window._offset, window._nudge) == (16, 0)
    window._nav_bytes(3)
    assert press(Qt.Key.Key_0) is True
    assert (window._offset, window._nudge) == (16, 0)
    # An unregistered Ctrl combo is not consumed (normal shortcuts still work).
    assert press(Qt.Key.Key_S, ctrl) is False


def test_the_address_format_dropdown_drives_the_offset_box(
    qtbot, tmp_path, monkeypatch
) -> None:
    """Picking an address format swaps the offset box's render/parse pair, and the
    bank spins that parameterize it follow.

    All of it is one wiring question - which layout is the box speaking right now -
    so it is checked as one walk through the formats rather than per format. The
    mapping arithmetic itself lives in test_address.
    """
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)
    window._nav_rows(1)  # byte 0x200
    before = window._offset

    # Flat hex needs no bank settings, and invalid input reverts to the current
    # offset. The commit path is focus-independent (CommittingLineEdit always
    # re-renders), so this one case covers focused and unfocused alike.
    assert not window._bank_size.isEnabled()
    window._address_edit.setText("nonsense")
    window._address_edit.commit()
    assert window._offset == before
    assert window._address_edit.text() == f"0x{before * 32:06X}"

    # A banked preset re-renders the displayed text and parses typed addresses
    # under the new layout, and fills the spins it is described by.
    _select_address_format(window, "snes-lorom")
    assert window._address_edit.text() == "$00:8200"
    assert window._bank_size.isEnabled()
    assert (
        window._bank_size.value(),
        window._bank_addr.value(),
        window._bank_first.value(),
    ) == (0x8000, 0x8000, 0x00)
    window._address_edit.setText("$00:8400")  # byte 0x400 -> tile 32
    window._address_edit.commit()
    assert window._offset == 32
    assert window._address_edit.text() == "$00:8400"

    # Hand-editing a spin flips the dropdown to Custom - the preset no longer
    # describes the settings - and re-renders under the edited layout. Re-selecting
    # the preset restores its values.
    window._bank_first.setValue(0x40)  # e.g. SuperFX-style bank numbering
    assert window._addr_format.currentData() == "custom"
    assert window._address_edit.text() == "$40:8400"
    _select_address_format(window, "snes-lorom")
    assert window._bank_first.value() == 0x00
    assert window._address_edit.text() == "$00:8400"

    # ExHiROM/ExLoROM are piecewise mappings the three-spin model can't express:
    # selecting one hides the settings entirely and renders through the split
    # layout; a banked preset brings them back.
    window._nav_home()
    _select_address_format(window, "snes-exhirom")
    assert window._bank_settings.isHidden()
    assert window._address_edit.text() == "$C0:0000"
    _select_address_format(window, "snes-lorom")
    assert not window._bank_settings.isHidden()


def test_entire_file_view_grows_the_window_and_locks_rows(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtCore import Qt

    # View > Entire File takes the window height off the Rows setting: the window
    # grows to every row the data fills, so the offset has nowhere left to go.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(2)  # a 16-tile window, four pages into the 64 tiles
    window._set_offset(32)
    assert window._offset == 32

    window._entire_file.setChecked(True)
    assert window._view_rows() == 8  # 64 tiles / 8 columns
    assert window._offset == 0  # the whole file is on screen; nothing to page to
    assert window._canvas._image.height() == 8 * window._doc.tile_height
    # Rows is locked, not overwritten: the number stays the user's, and stays
    # what the project stores.
    assert not window._rows.isEnabled()
    assert window._rows.value() == 2
    assert window._doc.view.rows == 2
    window._nav_keys[(Qt.Key.Key_Down, True, False)]()  # Shift+Down = more rows
    assert window._rows.value() == 2

    # A file that already fits inside Rows was never being limited, so the window
    # stays as it is rather than shrinking onto the data.
    window._columns.setValue(64)  # one row of tiles, in a two-row window
    assert window._view_rows() == 2

    window._entire_file.setChecked(False)
    assert window._rows.isEnabled()
    assert window._view_rows() == 2


def test_leaving_entire_file_re_anchors_on_the_selection(
    qtbot, tmp_path, monkeypatch
) -> None:
    # The window collapsing back to Rows would strand it at the file's start, so
    # it lands on the tile picked out of the full view instead - snapped to the
    # nearest whole row, the position bar's own rule.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(2)
    window._entire_file.setChecked(True)
    window._select_tiles(35, 35)  # row 4 (tiles 32-39), three tiles in

    window._entire_file.setChecked(False)
    assert window._offset == 32

    # A selection past the last page still leaves a reachable window, and with no
    # selection at all there is nothing to re-anchor on: the offset stays put.
    window._entire_file.setChecked(True)
    window._select_tiles(63, 63)
    window._entire_file.setChecked(False)
    assert window._offset == 48  # the last page (64 tiles - a 16-tile window)
    window._entire_file.setChecked(True)
    window._clear_selection()
    window._entire_file.setChecked(False)
    assert window._offset == 0  # where the whole-file view left it


def test_offset_scrollbar_jumps_and_stays_in_sync(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)  # page = 32 tiles; scrollbar max = 64 - 32 = 32.
    assert window._tile_offset_bar.maximum() == 32
    assert window._tile_offset_bar.pageStep() == 32
    assert window._tile_offset_bar.singleStep() == 16  # one row

    # A drag can land on any tile; the offset snaps to the nearest whole row
    # and the bar is pulled back onto it.
    window._tile_offset_bar.setValue(20)
    assert window._offset == 16
    assert window._tile_offset_bar.value() == 16

    # Moving via keys/buttons keeps the scrollbar in step (no feedback loop).
    window._nav_home()
    assert window._offset == 0
    assert window._tile_offset_bar.value() == 0


def test_block_pattern_scales_vertical_step(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtCore import Qt

    # A 2-tile-high block makes the vertical unit a block-row: Up/Down move two
    # rows and the position bar snaps to block-row multiples, so no vertical
    # move can re-cut blocks from a mid-block origin.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(2)
    window._pattern.setCurrentIndex(window._pattern.findText(_pattern_name("nes-8x16")))
    window._nav_keys[(Qt.Key.Key_Down, False, False)]()
    assert window._offset == 16  # two rows of 8 tiles
    assert window._tile_offset_bar.singleStep() == 16
    window._tile_offset_bar.setValue(28)  # nearest block-row multiple is 32
    assert window._offset == 32


def test_switching_codec_preserves_byte_offset(qtbot, tmp_path, monkeypatch) -> None:
    # Opened as SNES 4bpp (32 bytes/tile); a small window leaves room to scroll.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(4)
    window._rows.setValue(4)
    window._set_offset(10)  # tile 10 -> byte 320 (10 * 32)
    assert window._doc.bytes_per_tile == 32
    assert window._offset == 10

    # Switch to GB 2bpp (16 bytes/tile): the file byte position (320) is preserved,
    # snapped to the new tile boundary -> tile 20.
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.gb-2bpp")
    )
    assert window._doc.bytes_per_tile == 16
    assert window._offset == 20  # 320 // 16


def test_cycling_pixel_formats_keeps_target_offset_across_whole_bank(
    qtbot, tmp_path, monkeypatch
) -> None:
    # Cycling the pixel dropdown to eyeball formats must keep re-anchoring on the
    # position where the run started, even when an intermediate format has tiles
    # so large the view clamps back to page 0. The whole-bank 8bpp format is one
    # 16384-byte tile; a 2-bank file holds only ~2 of those, so any multi-tile
    # window collapses to offset 0 — and the sub-tile remainder it would keep is
    # NOT the position. Before the target latch, switching back landed there.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=1024)  # 32768 B = 2 banks
    window._columns.setValue(16)
    window._rows.setValue(2)  # 32-tile page, small enough to leave room to scroll
    window._set_offset(900)  # tile 900 -> byte 28800 (900 * 32), well past bank 0
    assert window._doc.bytes_per_tile == 32
    assert window._offset == 900

    # Switch to whole-bank 8bpp (16384 B/tile). byte 28800 -> tile 1 + nudge 12416,
    # but a 16x2 page needs 32 whole-bank tiles and only 2 exist, so the offset
    # clamps to 0 — the exact case that used to lose the position. The scratch
    # target latches the true byte position so the next switch can recover it.
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.snes-8bpp-bank")
    )
    assert window._doc.bytes_per_tile == 16384
    assert window._offset == 0  # clamped away from the real position
    assert window._pixel_switch_target == 28800  # ...but the target remembers it

    # Switch back to 4bpp mid-run: re-anchoring on the latched 28800 (not the
    # clamped view) restores tile 900. Before the fix this read the clamped view's
    # byte 12416 and landed at tile 388 (12416 // 32).
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.snes-4bpp")
    )
    assert window._doc.bytes_per_tile == 32
    assert window._offset == 900  # position survived the round-trip

    # Leaving the dropdown ends the run, so a fresh switch re-anchors on the live
    # view rather than resurrecting this stale target.
    window._end_pixel_switch_run()
    assert window._pixel_switch_target is None


def test_scrolling_the_position_bar_ends_a_pixel_switch_run(
    qtbot, tmp_path, monkeypatch
) -> None:
    # The position bar never takes focus, so focus_lost can't end a switching run
    # after a drag: navigating with it has to end the run itself, or the next
    # format switch would resurrect the pre-scroll target and jump back there.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=1024)
    window._columns.setValue(16)
    window._rows.setValue(2)
    window._set_offset(100)  # byte 3200

    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.gb-2bpp")
    )
    assert window._pixel_switch_target == 3200  # latched by the first switch

    window._tile_offset_bar.setValue(400)  # a drag, not a button or a key
    assert window._offset == 400
    assert window._pixel_switch_target is None

    # 16 B/tile -> 32: the switch anchors on byte 6400, where the drag landed.
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.snes-4bpp")
    )
    assert window._offset == 200  # 400 * 16 // 32, not the pre-scroll 100


def test_nav_keys_act_unless_an_arrow_input_is_focused(
    qtbot, tmp_path, monkeypatch
) -> None:
    # Navigation keys work wherever focus is, EXCEPT when an arrow-consuming input
    # (dropdown, spin box, text field) is focused — that keeps the keys for itself.
    # focusWidget is monkeypatched because real focus delivery is environment-dependent.
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)  # page 32 -> room to scroll down one row (16 tiles)
    down = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier
    )

    def focus_is(widget):
        monkeypatch.setattr(QApplication, "focusWidget", staticmethod(lambda: widget))

    # Arrow-consuming inputs keep the key: not handled, no navigation.
    for control in (window._pixel_preset, window._rows, window._address_edit):
        focus_is(control)
        assert window._handle_nav_key(down) is False
        assert window._offset == 0

    # A non-input widget (the canvas) lets the key navigate.
    focus_is(window._canvas)
    assert window._handle_nav_key(down) is True
    assert window._offset == 16


def test_shift_arrow_resizes_and_reclamps(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)
    window._nav_end()
    assert window._offset == 32  # last page with a 32-tile window

    # Grow the window (more rows): the page gets bigger, so the offset re-clamps.
    window._adjust_spin(window._rows, 2)  # rows 2 -> 4 = 64-tile page
    assert window._rows.value() == 4
    assert window._offset == 0  # whole file now fits in one page


def test_g_key_cycles_grid(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    from celpix.core.document import GridMode

    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=8)
    press_g = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_G, Qt.KeyboardModifier.NoModifier
    )

    # Focused text input keeps the letter (it may be typing).
    monkeypatch.setattr(
        QApplication, "focusWidget", staticmethod(lambda: window._address_edit)
    )
    assert window._handle_nav_key(press_g) is False
    assert not window._grid.isChecked()

    # Otherwise G flips View > Grid, flowing through to the project-wide setting.
    monkeypatch.setattr(
        QApplication, "focusWidget", staticmethod(lambda: window._canvas)
    )
    assert window._handle_nav_key(press_g) is True
    assert window._grid.isChecked()
    assert window._canvas._show_grid
    # The scale it comes on at is the menu's, and survives being switched off.
    window._grid_actions[GridMode.PIXEL].setChecked(True)
    window._on_grid_change()
    window._handle_nav_key(press_g)
    assert not window._canvas._show_grid
    window._handle_nav_key(press_g)
    assert window._canvas._show_grid
    assert window._canvas._grid_mode is GridMode.PIXEL

    # Shift+G cycles the app-wide grid style, on the same routing.
    from celpix.ui.canvas import GridStyle

    press_shift_g = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_G, Qt.KeyboardModifier.ShiftModifier
    )
    styles = [action.data() for action in window._grid_style_group.actions()]
    start = styles.index(window._grid_style_group.checkedAction().data())
    for step in range(1, len(styles) + 1):  # all the way round, back to the start
        assert window._handle_nav_key(press_shift_g) is True
        expected = styles[(start + step) % len(styles)]
        assert window._grid_style_group.checkedAction().data() is expected
        assert window._canvas._grid_style is expected
    assert isinstance(window._canvas._grid_style, GridStyle)

    # The grid is a preference, not entry state: switching files leaves it be.
    window._workspace.add_slice(window._workspace.entries[0].path, "gfx", 0, 32)
    window._activate_entry(window._workspace.entries[1])
    assert window._grid.isChecked()
    assert window._grid_mode() is GridMode.PIXEL


def test_a_bare_letter_key_is_dead_while_its_control_is(
    qtbot, tmp_path, monkeypatch
) -> None:
    """The one rule the key table exists to keep: a bare letter presses a control
    on screen, and does nothing at all while that control is switched off - so a
    new key cannot arrive carrying its own idea of when it applies.

    Both halves are checked over *every* registered key rather than over the
    handful that have a lockout today, because the mistake this guards against is
    made by the next key added, not by these.
    """
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QAction, QActionGroup, QKeyEvent

    from celpix.ui.main_window.navigation import KeyControl

    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    fired: list[tuple] = []
    # Stand in for each key's effect, so "did the key act?" is answerable for all
    # of them at once; the controls, and the gate on them, stay the real ones.
    window._key_controls = {
        combo: KeyControl(binding.control, lambda c=combo: fired.append(c))
        for combo, binding in window._key_controls.items()
    }

    def press(combo) -> bool:
        key, shift, ctrl = combo
        mods = Qt.KeyboardModifier.NoModifier
        if shift:
            mods |= Qt.KeyboardModifier.ShiftModifier
        if ctrl:
            mods |= Qt.KeyboardModifier.ControlModifier
        return window._handle_nav_key(QKeyEvent(QEvent.Type.KeyPress, key, mods))

    for combo, binding in window._key_controls.items():
        control = binding.control
        # Only an action's visibility is its own; a widget's answers for the
        # unshown test window, which is why ``fire`` doesn't ask it.
        hideable = isinstance(control, QAction | QActionGroup)
        enabled = control.isEnabled()

        control.setEnabled(False)
        # Swallowed, and inert: the letter means this control everywhere, so
        # falling through to whatever else wanted it would be the surprise.
        assert press(combo) is True
        assert fired == [], f"{combo} acted with its control disabled"

        control.setEnabled(True)
        if hideable:
            # An action hidden as "not a thing on this document" takes its key
            # with it - the Edit Tiles mode off a tilemap is the live case.
            control.setVisible(False)
            assert press(combo) is True
            assert fired == [], f"{combo} acted with its control hidden"
            control.setVisible(True)
        assert press(combo) is True
        assert fired == [combo], f"{combo} did not press its control"

        fired.clear()
        control.setEnabled(enabled)


def test_grid_menu_applies_and_persists_as_a_local_preference(qtbot, tmp_path) -> None:
    # Every part of the grid is a local preference, not project state: a fresh
    # window comes up with the grid the last one was left on, whatever project it
    # opens.
    from celpix.core.document import GridMode
    from celpix.ui.canvas import GridStyle

    _fresh_settings(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)

    dot = next(
        a for a in window._grid_style_group.actions() if a.data() is GridStyle.DOT
    )
    dot.trigger()
    assert window._canvas._grid_style is GridStyle.DOT
    window._grid.setChecked(True)
    window._grid_actions[GridMode.PIXEL].trigger()
    window._block_grid.setChecked(True)
    assert window._canvas._grid_mode is GridMode.PIXEL

    reopened = MainWindow()
    qtbot.addWidget(reopened)
    assert reopened._canvas._grid_style is GridStyle.DOT
    checked = [a.data() for a in reopened._grid_style_group.actions() if a.isChecked()]
    assert checked == [GridStyle.DOT]
    # The scale and the two toggles come back on both the menu and the canvas.
    assert (reopened._grid.isChecked(), reopened._block_grid.isChecked()) == (
        True,
        True,
    )
    assert reopened._grid_mode() is GridMode.PIXEL
    assert reopened._canvas._grid_levels(4, 4)[0][0] == (1, 1)  # drawing at that scale


def test_theme_menu_repaints_the_app_and_persists(qtbot, tmp_path) -> None:
    # The theme is one palette on the QApplication, so what proves it took is the
    # *application's* palette going dark - not the window's own. Like the grid, it
    # is a local preference: a fresh window comes up on the last theme chosen.
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication

    from celpix.ui.theme import Theme, apply_theme

    def surface_lightness() -> int:
        return app.palette().color(QPalette.ColorRole.Window).lightness()

    _fresh_settings(tmp_path)
    app = QApplication.instance()
    try:
        window = MainWindow()
        qtbot.addWidget(window)
        light = surface_lightness()
        assert light > 128

        dark = next(a for a in window._theme_group.actions() if a.data() is Theme.DARK)
        dark.trigger()
        assert surface_lightness() < 128
        # The rail's accent is baked into a stylesheet string, so it is the one
        # thing a repolish can't refresh: it has to name the new Highlight.
        accent = app.palette().color(QPalette.ColorRole.Highlight).name()
        assert accent in window._tile_offset_bar.styleSheet()

        reopened = MainWindow()
        qtbot.addWidget(reopened)
        checked = [a.data() for a in reopened._theme_group.actions() if a.isChecked()]
        assert checked == [Theme.DARK]
    finally:
        # The QApplication outlives the test; leave it as the rest of the suite
        # expects to find it.
        apply_theme(Theme.LIGHT)
    assert surface_lightness() == light


def test_bitmap_width_recuts_tiles_and_derives_columns(qtbot, tmp_path) -> None:
    # A 306-px-wide RGB888 bitmap (the width an 8-px tile cannot span). Setting
    # the width re-cuts the codec to 6x6 tiles and points Cols at the 51 that
    # span it exactly - the two together are what makes the picture line up
    # instead of shearing two pixels per row.
    px = tmp_path / "bitmap.bin"
    px.write_bytes(bytes((i * 7 + 3) & 0xFF for i in range(306 * 24 * 3)))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.dc-rgb888-be")
    )
    assert (window._doc.tile_width, window._doc.tile_height) == (8, 8)
    # The width belongs to the wide-bitmap walk, so it is dead until 2D is on.
    assert not window._bitmap_width.isEnabled()
    _select_2d_pattern(window)
    assert window._bitmap_width.isEnabled()

    window._bitmap_width.setValue(306)
    assert (window._doc.tile_width, window._doc.tile_height) == (6, 6)
    assert window._doc.bytes_per_tile == 6 * 6 * 3
    assert window._columns.value() == 51  # 306 / 6, and no longer the user's
    assert not window._columns.isEnabled()
    # The re-cut size is what Cols was derived from, so the label beside it has
    # to report the override rather than the codec's nominal 8x8.
    assert window._tile_size.text() == "6×6"
    assert "6x6" in window.statusBar().currentMessage()
    # The rendered window really is 306 pixels across, which is the whole point.
    assert window._canvas._image.width() == 306

    # Back to 0: the codec's own geometry returns, and Cols is the user's again -
    # at the count it had before the width took it over, not the derived 51.
    window._bitmap_width.setValue(0)
    assert (window._doc.tile_width, window._doc.tile_height) == (8, 8)
    assert window._columns.isEnabled()
    assert window._columns.value() == 16
    assert window._tile_size.text() == "8×8"

    # Changing the Pattern drops the width outright: it described this one asset,
    # so the geometry, Cols and the field itself all reset rather than the width
    # staying in force invisibly under the new arrangement.
    window._bitmap_width.setValue(306)
    assert window._doc.tile_width == 6
    window._pattern.setCurrentIndex(window._pattern.findText(_pattern_name("linear")))
    assert (window._doc.tile_width, window._doc.tile_height) == (8, 8)
    assert window._bitmap_width.value() == 0
    assert not window._bitmap_width.isEnabled()  # the width needs the 2D walk
    assert window._columns.isEnabled()
    assert window._columns.value() == 16
    # And coming back to a 2D arrangement starts from a clean, editable field
    # instead of springing the old width back on.
    _select_2d_pattern(window)
    assert window._bitmap_width.isEnabled()
    assert window._bitmap_width.value() == 0
    assert (window._doc.tile_width, window._doc.tile_height) == (8, 8)

    # An entry round trip reads the Pattern back as the 2D preset - the four axes
    # match it, and the width is not one of them. The width must survive that
    # editable: locking it under the preset would leave 306 in force with no way
    # to change it, which is what a restored session always lands on.
    window._bitmap_width.setValue(306)
    other = tmp_path / "other.bin"
    other.write_bytes(bytes(4096))
    window._load_pixel(str(other))
    window._workspace.set_current(window._workspace.find_file(str(px)))
    assert window._pattern.currentData().id == "2d"
    assert window._bitmap_width.value() == 306
    assert window._bitmap_width.isEnabled()
    assert window._doc.tile_width == 6


def test_bitmap_width_leaves_a_fixed_tile_codec_alone(qtbot, tmp_path) -> None:
    # A planar codec's row is eight pixels of bitplane, so there is no 6-px tile
    # to re-cut to. The view must keep its real geometry (a claimed 6x6 would
    # decode as garbage) and say so, rather than silently doing nothing.
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    _select_2d_pattern(window)
    window._bitmap_width.setValue(306)
    assert (window._doc.tile_width, window._doc.tile_height) == (8, 8)
    assert window._columns.isEnabled()  # nothing spans 306 with an 8-px tile
    assert "no effect" in window.statusBar().currentMessage()


def test_rows_past_end_of_file_are_not_clamped_or_black_filled(qtbot, tmp_path) -> None:
    small = tmp_path / "small.4bpp.sfc"
    small.write_bytes(bytes(32 * 6))  # 6 tiles of 32 B (SNES 4bpp)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(small))
    window._columns.setValue(4)  # tile_count 6 => only ceil(6/4)=2 rows of data
    window._rows.setValue(8)  # far more rows than the file fills

    # The rows spin is a free display-window height: its max is a fixed 256, not
    # bound to the data, so a value larger than the file survives instead of being
    # dragged down to the 2 rows that exist.
    assert window._rows.value() == 8
    assert window._rows.maximum() == 256

    # The composed image is narrowed to the rows that actually hold tiles, so the
    # extra 6 empty rows show the neutral viewport background rather than black
    # filler tiles: height is 2 data rows * 8 px, not 8 rows * 8 px, and the width
    # is the 4 columns * 8 px. (SNES 4bpp tiles are 8x8.)
    assert (window._doc.tile_width, window._doc.tile_height) == (8, 8)
    assert window._canvas._image.height() == 2 * 8
    assert window._canvas._image.width() == 4 * 8


def _canvas_with_3x2_red(qtbot):
    """A Canvas holding a 3-col x 2-row image of opaque red 8x8 tiles at zoom 1.

    Red (not the gray backing) so a real tile's pixels are unmistakably distinct
    from the past-end fill.
    """
    from PySide6.QtGui import QImage

    from celpix.ui.canvas import Canvas

    c = Canvas()
    qtbot.addWidget(c)
    c.set_tile_size(8, 8)
    c.set_zoom(1)
    img = QImage(3 * 8, 2 * 8, QImage.Format.Format_RGB32)
    img.fill(0xFFFF0000)  # opaque red
    c.set_image(img)
    return c


def test_past_end_region_maps_linear_padding_to_last_row_block(qtbot) -> None:
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QRegion

    c = _canvas_with_3x2_red(qtbot)

    # 5 of 6 slots filled: the stream ends one slot into the bottom row, so slot 5
    # (row 1, col 2) is padding. The trailing block is that single last-row cell:
    # x = col 2 * 8px, y = row 1 * 8px, one column wide, one tile tall.
    c.set_filled_tiles(5)
    assert c._background_region() == QRegion(QRect(2 * 8, 1 * 8, 1 * 8, 8))

    # The region is in device coords, so it scales with zoom.
    c.set_zoom(3)
    assert c._background_region() == QRegion(
        QRect(2 * 8 * 3, 1 * 8 * 3, 1 * 8 * 3, 8 * 3)
    )
    c.set_zoom(1)

    # A full window has no padding; neither does an unset count.
    c.set_filled_tiles(6)
    assert c._background_region() is None
    c.set_filled_tiles(None)
    assert c._background_region() is None

    # An exactly-full last row (remainder 0) is not padding either: 3 fills the
    # top row completely, leaving the bottom row entirely absent, not partial.
    c.set_filled_tiles(3)
    assert c._background_region() is None


def test_half_zoom_halves_the_canvas_and_still_hits_the_right_pixel(qtbot) -> None:
    """The one zoom level under 1 reduces, so every device-pixel geometry the
    canvas derives has to survive a fraction — the size it asks for, the cell
    rects it paints, and the hit-test that turns a click back into a pixel."""
    from PySide6.QtCore import QPointF, QRect
    from PySide6.QtGui import QImage

    c = _canvas_with_3x2_red(qtbot)  # 24x16 image of 8px tiles
    c.set_zoom(0.5)
    assert (c.width(), c.height()) == (12, 8)
    assert c._slot_rect(1, 1) == QRect(4, 4, 4, 4)
    # Two image pixels to the device pixel, and a position left of the canvas
    # still reads as outside rather than snapping onto the edge column.
    assert c._pixel_at(QPointF(3, 1)) == (6, 2)
    assert c._pixel_at(QPointF(-1, 1)) is None
    # A whole paint at the fractional level: the grid is gated off below zoom 2
    # (so the lattice keeps its integer arithmetic), and the two label overlays
    # bail on their fit test - but every one of them measures in device pixels
    # off the zoom first, which is where a stray fraction would land.
    c.set_grid(True)
    c.set_palette_rows([0, 1, None, 2, None, 3])
    c.set_tile_ids([0, 1, 2, 3, 4, 5])
    c.set_filled_tiles(5)  # and the past-end fill, which is a scaled region
    c.render(QImage(c.size(), QImage.Format.Format_RGB32))


def test_grid_levels_follow_the_mode_the_block_and_the_zoom(qtbot) -> None:
    # The steps the whole grid is drawn from: everything below _grid_levels works
    # from this list alone, so this is where the mode's meaning lives.
    from celpix.core.document import GridMode
    from celpix.ui import canvas as canvas_mod

    c = _canvas_with_3x2_red(qtbot)  # 8x8 tiles

    def steps(z: int) -> list[tuple[int, int]]:
        return [step for step, _color in c._grid_levels(z, z)]

    def alphas(z: int) -> list[int]:
        return [color.alpha() for _step, color in c._grid_levels(z, z)]

    # The fine (grey) level is the unit being worked in; the structural (blue)
    # one is what it sits inside — the 8-tile square at tile scale, the tile at
    # pixel scale.
    c.set_grid(True, GridMode.TILE)
    assert steps(4) == [(8, 8), (64, 64)]
    c.set_grid(True, GridMode.PIXEL)
    assert steps(4) == [(1, 1), (8, 8)]

    # Block Grid moves that structural level onto the arrangement's own block,
    # at either scale.
    c.set_arrangement(2, 4, "row")
    c.set_grid(True, GridMode.TILE, True)
    assert steps(4) == [(8, 8), (16, 32)]
    c.set_grid(True, GridMode.PIXEL, True)
    assert steps(4) == [(1, 1), (16, 32)]

    # Off it again, and each scale is back to its own step — the block being set
    # is not what draws it.
    c.set_grid(True, GridMode.PIXEL)
    assert steps(4) == [(1, 1), (8, 8)]
    c.set_grid(True, GridMode.TILE)
    assert steps(4) == [(8, 8), (64, 64)]
    c.set_arrangement(1, 1, "row")
    c.set_grid(True, GridMode.PIXEL)

    # The pixel level fades in with the zoom instead of popping: gone at 2x (the
    # coarse level carries the lattice alone), partway at 4x, full from
    # GRID_PIXEL_FULL_ZOOM on.
    assert steps(2) == [(8, 8)]
    assert 0 < alphas(4)[0] < canvas_mod.GRID_ALPHA
    assert alphas(canvas_mod.GRID_PIXEL_FULL_ZOOM)[0] == canvas_mod.GRID_ALPHA

    # And the levels are told apart by hue, not opacity alone — by role, so the
    # tile level is the grey one in tile mode and the blue one in pixel mode.
    fine, coarse = (color for _step, color in c._grid_levels(16, 16))
    assert (fine.rgb(), coarse.rgb()) == (
        canvas_mod.GRID_FINE_COLOR.rgb(),
        canvas_mod.GRID_STRUCTURE_COLOR.rgb(),
    )
    c.set_grid(True, GridMode.TILE)
    assert c._grid_levels(16, 16)[0][1].rgb() == canvas_mod.GRID_FINE_COLOR.rgb()


def test_arrangement_controls_reach_the_view_and_canvas(qtbot, tmp_path) -> None:
    # The toolbar's block/order/2D controls must flow through _refresh_view into
    # the stored ViewOptions and the canvas's placement — otherwise the feature
    # renders nothing.
    px = _make_snes_file(tmp_path)  # 8 tiles
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))

    window._block_cols.setValue(2)
    window._block_rows.setValue(2)
    window._block_order.setCurrentIndex(window._block_order.findData("column"))
    window._two_d.setChecked(True)

    view = window._doc.view
    assert (view.block_columns, view.block_rows) == (2, 2)
    assert view.block_order == "column"
    assert view.two_dimensional is True
    # The canvas got the same placement, so clicks/selection map correctly.
    assert window._canvas._block_rows == 2
    assert window._canvas._block_order == "column"

    # Settings survive a round-trip through another entry and back (session state).
    window._workspace.add_slice(str(px), "gfx", 64, 64)
    window._activate_entry(window._workspace.entries[1])
    window._activate_entry(window._workspace.entries[0])
    back = window._doc.view
    assert (back.block_rows, back.block_order, back.two_dimensional) == (
        2,
        "column",
        True,
    )


def test_pattern_preset_fills_and_locks_arrangement_controls(qtbot, tmp_path) -> None:
    # The Pattern picker is the arrangement analogue of the Offset format picker:
    # a preset fills the block/order/2D controls and locks them; Custom unlocks
    # them. The lock keeps a preset's values from being edited out from under it.
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))

    # Default view is Linear (first preset) — controls locked.
    assert window._pattern.currentData().id == "linear"
    assert not window._block_cols.isEnabled()

    idx = window._pattern.findData(
        next(p for p in ARRANGEMENT_PRESETS if p.id == "genesis-sprite")
    )
    window._pattern.setCurrentIndex(idx)
    view = window._doc.view
    assert (view.block_columns, view.block_rows, view.block_order) == (2, 2, "column")
    assert view.two_dimensional is False
    # A preset owns the controls, so they stay read-only.
    assert not window._block_cols.isEnabled()
    assert not window._two_d.isEnabled()

    # Custom unlocks them without changing the values it inherits.
    window._pattern.setCurrentIndex(window._pattern.findData("custom"))
    assert window._block_cols.isEnabled() and window._two_d.isEnabled()
    assert window._doc.view.block_order == "column"


def test_pattern_selection_is_rederived_on_session_restore(qtbot, tmp_path) -> None:
    # Restoring an entry sets the block/order/2D widgets from its saved view; the
    # Pattern picker must reselect the matching preset (or Custom) to match — the
    # selection isn't persisted, it's derived from those four values.
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))

    window._pattern.setCurrentIndex(
        window._pattern.findData(
            next(p for p in ARRANGEMENT_PRESETS if p.id == "nes-8x16")
        )
    )
    window._workspace.add_slice(str(px), "gfx", 64, 64)
    window._activate_entry(window._workspace.entries[1])
    # The fresh slice inherits the default arrangement → Linear, controls locked.
    assert window._pattern.currentData().id == "linear"
    assert not window._block_rows.isEnabled()

    window._activate_entry(window._workspace.entries[0])
    # Back on the file, the 8×16 preset is reselected (and stays locked).
    assert window._pattern.currentData().id == "nes-8x16"
    assert not window._block_rows.isEnabled()


def test_canvas_block_layout_maps_clicks_and_backgrounds_gaps(qtbot) -> None:
    from PySide6.QtCore import QPointF, QRect
    from PySide6.QtGui import QRegion

    c = _canvas_with_3x2_red(qtbot)
    # 1×2 blocks: consecutive tiles stack vertically, so the cell at (col 0, row 1)
    # is slot 1 (the bottom of the first sprite), not slot 3 as in row-major.
    c.set_arrangement(1, 2, "row")
    assert c._slot_at(QPointF(0 * 8 + 4, 1 * 8 + 4)) == 1
    assert c._slot_at(QPointF(1 * 8 + 4, 0 * 8 + 4)) == 2  # second sprite's top

    # With only 4 of 6 slots filled, slots 4 and 5 (the third column's block) are
    # padding — the canvas backgrounds that whole column.
    c.set_filled_tiles(4)
    region = c._background_region()
    assert region == QRegion(QRect(2 * 8, 0, 8, 2 * 8))


def test_canvas_column_order_maps_clicks_genesis_style(qtbot) -> None:
    from PySide6.QtCore import QPointF

    # A 3×2-tile canvas as one column-major block: tiles run down each column, so
    # the top of the second column (cell 1,0) is slot 2, and the bottom of the
    # first column (cell 0,1) is slot 1 — the Mega Drive sprite order.
    c = _canvas_with_3x2_red(qtbot)
    c.set_arrangement(3, 2, "column")
    assert c._slot_at(QPointF(0 * 8 + 4, 1 * 8 + 4)) == 1  # bottom of column 0
    assert c._slot_at(QPointF(1 * 8 + 4, 0 * 8 + 4)) == 2  # top of column 1


def test_canvas_paints_past_end_slots_as_background(qtbot) -> None:
    from PySide6.QtGui import QColor

    from celpix.ui.canvas import CANVAS_BACKGROUND

    c = _canvas_with_3x2_red(qtbot)
    c.set_filled_tiles(5)  # slot 5 (bottom-right cell) is padding

    img_out = c.grab().toImage()

    # Sample the centre of the padding cell (col 2, row 1): x = 2*8 + 4 = 20,
    # y = 1*8 + 4 = 12. It must show the neutral backing, not a black index-0 tile.
    # Compare RGB only — grab() may carry an alpha the fill color doesn't.
    assert img_out.pixelColor(20, 12).rgb() == CANVAS_BACKGROUND.rgb()

    # A real (filled) tile still paints its data: slot 0's centre (4, 4) is red,
    # and definitely not the gray backing.
    assert img_out.pixelColor(4, 4).rgb() == QColor(0xFF, 0x00, 0x00).rgb()
    assert img_out.pixelColor(4, 4).rgb() != CANVAS_BACKGROUND.rgb()


def test_cell_overlays_repaint_in_strips_exactly_as_in_one_pass(qtbot) -> None:
    """The overlays draw the exposed band, so a strip must hold its whole share.

    Every per-cell overlay - ids, palette rows, line ends - now loops the slots
    the exposed rectangle covers rather than the window's (``Canvas.
    _exposed_slots``), which is what keeps a repaint of a metatile map the cost
    of what is on screen. The band is where that can go wrong: a label written
    from a cell just outside the strip still puts ink inside it, so scrolling
    would leave numbers behind. Repainting in strips has to come out pixel-for-
    pixel identical to repainting the lot.

    Under a **block** arrangement, where a slot does not sit where its number
    says and the band is a period of the mapping rather than a run of rows - and
    under a **non-square pixel**, where a row boundary in device pixels is not
    the row height times the zoom but a rounded edge either side of it, which is
    the case the band's slack is there for.
    """
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtGui import QImage, QRegion

    from celpix.ui.canvas import Canvas

    canvas = Canvas()
    qtbot.addWidget(canvas)
    canvas.set_tile_size(8, 8)
    canvas.set_zoom(4)  # big enough that the labels are drawn at all
    canvas.set_pixel_aspect((8, 7))
    image = QImage(8 * 8, 8 * 8, QImage.Format.Format_RGB32)
    image.fill(0xFF202020)
    canvas.set_image(image)
    canvas.set_arrangement(2, 2, "row")  # 2x2 metatiles: 16 cells, 64 slots
    # One id per cell (its first slot), a row on every slot, a line end on a
    # cell in the middle of the picture - each overlay drawn from a different
    # corner of its cell, which is what the band's slack has to cover.
    canvas.set_tile_ids([slot if slot % 4 == 0 else None for slot in range(64)])
    canvas.set_palette_rows([slot % 8 for slot in range(64)])
    canvas.set_line_ends(frozenset({20, 40}))
    canvas.set_selection([12, 13], as_rect=True)

    whole = QImage(canvas.size(), QImage.Format.Format_ARGB32)
    canvas.render(whole, QPoint(), QRegion(canvas.rect()))

    strips = QImage(canvas.size(), QImage.Format.Format_ARGB32)
    step = 13  # narrow, and no multiple of it lands on a cell boundary
    for top in range(0, canvas.height(), step):
        for left in range(0, canvas.width(), step):
            band = QRect(left, top, step, step)
            canvas.render(strips, QPoint(left, top), QRegion(band))

    assert strips == whole


def test_entire_file_still_locks_rows_on_a_pixel_entry(qtbot, tmp_path) -> None:
    """The temporary reason and the permanent one stay distinguishable."""
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    window._entire_file.setChecked(True)
    # The toggle persists to QSettings; the settings store is emptied between
    # tests (``conftest._fresh_settings``), so it goes no further than this one.
    assert not window._rows.isEnabled()
    assert "Entire File" in window._rows.toolTip()


def test_pixel_aspect_stretches_the_canvas_without_moving_a_pixel(qtbot) -> None:
    """A non-square pixel is a display scale and nothing more.

    The picture grows on one axis and the hit test still answers in the image's
    own pixels — which is the whole contract the rest of the app relies on, since
    a selection, an edit and an export all address those
    (``docs/design/pixel-aspect.md`` §1).
    """
    from PySide6.QtCore import QPointF

    c = _canvas_with_3x2_red(qtbot)  # 8x8 tiles, so a 24x16 image
    c.set_zoom(3)
    square = (c.size().width(), c.size().height())

    # 1:2 — a pixel twice as tall as it is wide. The *taller* axis moves, so
    # nothing is ever drawn at less than the zoom asked for.
    c.set_pixel_aspect((1, 2))
    assert (c.size().width(), c.size().height()) == (square[0], square[1] * 2)
    c.set_pixel_aspect((2, 1))
    assert (c.size().width(), c.size().height()) == (square[0] * 2, square[1])

    # The hit test divides the stretch back out: under 1:2 at zoom 3 a pixel is
    # 3 device pixels wide and 6 tall.
    c.set_pixel_aspect((1, 2))
    assert c._pixel_at(QPointF(3, 5)) == (1, 0)
    assert c._pixel_at(QPointF(3, 7)) == (1, 1)
    assert c._pixel_at(QPointF(7, 13)) == (2, 2)

    # And square puts it back exactly, rather than to something that rounds to it.
    c.set_pixel_aspect((1, 1))
    assert (c.size().width(), c.size().height()) == square


def test_pixel_aspect_reaches_every_surface_that_draws_pixels(qtbot, tmp_path) -> None:
    """One project setting, one loop, every magnifying surface.

    Named surfaces rather than a walk of the widget tree, because the point of
    the check is that a surface cannot be *left out* of the sync — which is
    exactly what a tree walk would hide.
    """
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))

    window._workspace.pixel_aspect = (1, 2)
    window._sync_pixel_aspect()
    surfaces = (
        window._canvas,
        window._tile_source_panel,
        window._overlay._canvas,
        window._animation._frame,
        window._subsprites._panel,
        window._font_alphabet._sheet,
    )
    for surface in surfaces:
        assert surface._pixel_aspect == (1, 2), surface
        assert (surface._zoom_x, surface._zoom_y) == (surface._zoom, surface._zoom * 2)


def test_a_container_hint_seeds_the_pixel_aspect_but_only_once(qtbot, tmp_path) -> None:
    """The hint answers a question nobody has answered, and never overrules one.

    Both halves matter: the setting is one for the whole project, so the second
    file to state a shape must not move it under the first — and a user who has
    chosen must not be overruled by opening a file
    (``docs/design/pixel-aspect.md`` §4).
    """
    from celpix.core.context import KEY_PIXEL_ASPECT, PipelineContext
    from celpix.core.errors import Stage
    from celpix.plugins.base import PluginInfo, ReadSource
    from celpix.plugins.registry import default_registry

    class _Machine:
        """A container that knows which machine it is reading."""

        info = PluginInfo(
            id="container.tall-pixels", name="Tall", stage=Stage.CONTAINER
        )

        def read(self, src: ReadSource, ctx: PipelineContext) -> bytes:
            ctx.set(KEY_PIXEL_ASPECT, (1, 2))
            return src.window()

    registry = default_registry()
    registry.register(_Machine())
    window = MainWindow(registry=registry)
    qtbot.addWidget(window)

    first = _make_snes_file(tmp_path)
    window._load_pixel(str(first))
    entry = window._workspace.entries[0]
    assert window._workspace.pixel_aspect is None  # plain bytes state nothing

    # Re-read through the container that does state one: the project takes it.
    entry.container_id = "container.tall-pixels"
    window._workspace.drop_document(entry)
    window._load_entry(entry)
    window._activate_entry(entry)
    assert window._workspace.pixel_aspect == (1, 2)
    assert window._canvas._pixel_aspect == (1, 2)

    # A second file stating the same kind of thing does not get to re-answer,
    # and neither does the container once the user has chosen.
    window._workspace.pixel_aspect = (2, 1)
    window._workspace.drop_document(entry)
    window._load_entry(entry)
    assert window._workspace.pixel_aspect == (2, 1)
