"""Hex-view panel: the dump-building math (Qt-free) and the panel wiring."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from celpix.ui.hex_view_panel import find_bytes, hex_rows, parse_find_query
from celpix.ui.main_window import MainWindow


def _hex4(index: int) -> str:
    return f"{index:04x}"


def test_hex_rows_lays_out_bytes_addresses_and_ascii_gutter() -> None:
    # 'A', space, '~' are printable; 0x7f and the control bytes are not; the
    # region runs past the data so the last row is a padded partial.
    data = bytes([0x41, 0x20, 0x7E, 0x7F]) + bytes(range(0x10, 0x1C))
    rows = hex_rows(data, 0, 32, _hex4)

    assert [row.address for row in rows] == ["0000", "0010"]
    assert rows[0].hex_cells[:4] == ["41", "20", "7e", "7f"]
    assert rows[0].ascii[:4] == "A ~."
    # Row 1 holds the 6 trailing bytes (idx 10..15), then padding: empty hex
    # cells and blank ASCII line up under the columns above.
    assert rows[1].hex_cells[6] == ""
    assert rows[1].ascii[6:] == " " * 10


def test_hex_rows_highlight_spans_columns_across_rows() -> None:
    # Bytes 14..17 highlighted: the tail of row 0 and the head of row 1.
    rows = hex_rows(bytes(48), 0, 48, _hex4, highlight=(14, 4))

    assert (rows[0].hi_from, rows[0].hi_to) == (14, 16)
    assert (rows[1].hi_from, rows[1].hi_to) == (0, 2)
    assert rows[2].hi_from is None  # the range misses the third row entirely


def test_hex_rows_right_justifies_addresses_to_a_common_width() -> None:
    # A varying-width address format (no leading zeros) still yields aligned
    # columns: every address is padded to the widest one.
    rows = hex_rows(bytes(32), 0, 32, hex)  # hex(0) = "0x0", hex(16) = "0x10"
    assert [row.address for row in rows] == [" 0x0", "0x10"]


def test_hex_panel_dumps_current_offset_and_toggles(qtbot, tmp_path) -> None:
    # Large enough that the default 16x16 tile window can page off byte 0.
    data = bytes((i * 7 + 3) & 0xFF for i in range(0x4000))
    px = tmp_path / "gfx.bin"
    px.write_bytes(data)

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()  # a dock only counts as visible once its window is shown
    window._load_pixel(str(px))

    # Hidden by default: no refresh work, nothing rendered.
    assert not window._hex_dock.isVisible()
    assert window._hex_panel._view.dump_text() == ""

    window._hex_dock.setVisible(True)  # fires visibilityChanged -> refresh
    text = window._hex_panel._view.dump_text()
    # The dump starts at the current offset's row and shows the file's bytes
    # (03, 0a, 11, ...), addressed in the navbar's format (flat hex here).
    assert text.splitlines()[0].split()[:4] == ["0x000000", "03", "0a", "11"]

    # Moving the view re-dumps from the new position.
    window._set_byte_position(0x40)
    line = window._hex_panel._view.dump_text().splitlines()[0]
    assert line.startswith("0x000040")
    assert line.split()[1] == f"{data[0x40]:02x}"


def test_hex_panel_highlights_selected_tiles(qtbot, tmp_path) -> None:
    data = bytes((i * 7 + 3) & 0xFF for i in range(0x4000))
    px = tmp_path / "gfx.bin"
    px.write_bytes(data)

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window._load_pixel(str(px))
    window._hex_dock.setVisible(True)
    tb = window._doc.bytes_per_tile

    # Nothing selected: no highlight range, and the dump has no tinted span.
    assert window._selection_byte_range() is None
    assert all(row.hi_from is None for row in window._hex_panel._view.visible_rows())

    # Selecting a run of on-screen slots highlights those tiles' bytes: the run
    # maps to a contiguous byte range (nudge + tile*bytes_per_tile) and the
    # dump gains a highlighted span.
    window._on_slots_selected(0, 2)  # slots 0..2 at offset 0 -> tiles 0..2
    assert window._selection_byte_range() == (0, 3 * tb)
    assert any(
        row.hi_from is not None for row in window._hex_panel._view.visible_rows()
    )


def test_hex_rows_floors_the_address_column_to_a_stable_width() -> None:
    """A scrolling dump builds only the rows on screen, so the width the visible
    addresses agree on is not the width the *file* needs. Without the floor the
    columns would step sideways the moment scrolling reached 0x10000."""
    rows = hex_rows(bytes(32), 0, 32, hex, min_addr_width=7)
    assert [row.address for row in rows] == ["    0x0", "   0x10"]
    # The rows' own widest address still wins when it is the larger of the two.
    wide = hex_rows(bytes(32), 0, 32, lambda i: f"bank:{i:06x}", min_addr_width=4)
    assert wide[0].address == "bank:000000"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('"NES"', b"NES"),
        ("'NES'", b"NES"),
        ("4e 45 53", b"NES"),
        ("4e4553", b"NES"),
        ("$4e $45 $53", b"NES"),
        ("0x4e,0x45", b"NE"),
        ("4e4", None),  # half a byte names no byte
        ("zz", None),
        ("", None),
        ('""', None),
    ],
)
def test_parse_find_query_reads_both_ways_a_byte_pattern_is_written(
    text: str, expected: bytes | None
) -> None:
    assert parse_find_query(text) == expected


def test_find_bytes_walks_matches_and_reports_the_wrap() -> None:
    data = b"..NES....NES.."

    assert find_bytes(data, b"NES", 0) == (2, False)
    # Past the first match: the next one, without wrapping.
    assert find_bytes(data, b"NES", 3) == (9, False)
    # Past the last: wraps to the first, and says so.
    assert find_bytes(data, b"NES", 10) == (2, True)
    assert find_bytes(data, b"NES", 9, backwards=True) == (2, False)
    assert find_bytes(data, b"NES", 2, backwards=True) == (9, True)
    assert find_bytes(data, b"SNES", 0) is None


def test_hex_panel_go_to_and_find_move_the_dump_but_not_the_canvas(
    qtbot, tmp_path
) -> None:
    """The two boxes are why the dump covers the whole file rather than a window
    around the offset: a header or a pointer table can be read without the canvas
    losing its place, and the view's own bytes stay tinted while you look."""
    data = bytearray((i * 7 + 3) & 0xFF for i in range(0x4000))
    data[0x2000:0x2003] = b"NES"
    px = tmp_path / "gfx.bin"
    px.write_bytes(bytes(data))

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window._load_pixel(str(px))
    window._hex_dock.setVisible(True)
    panel = window._hex_panel
    origin = window._byte_position()

    # Go to scrolls the dump to the typed address and marks the byte there.
    panel._goto.setText("0x2000")
    panel._goto.returnPressed.emit()
    assert panel._view.dump_text().splitlines()[0].startswith("0x002000")
    assert panel._view.selection() == (0x2000, 1)
    # The canvas has not moved - that is the whole point of a separate box.
    assert window._byte_position() == origin

    # Find lands on the match and wraps round the end of the file to say so.
    panel._find.setText('"NES"')
    panel._do_find(backwards=False)
    assert panel._view.selection() == (0x2000, 3)
    assert panel._status.text() == "Wrapped"
    assert window._byte_position() == origin

    panel._find.setText("ffffffff")
    panel._do_find(backwards=False)
    assert panel._status.text() == "Not found"


def test_hex_dump_scrolls_the_whole_file_and_holds_its_place(qtbot, tmp_path) -> None:
    """The dump is virtualized over the whole file, so the scrollbar spans it and
    a repaint that does not move the offset leaves the reader where they were -
    otherwise scrolling off to read elsewhere would be undone by the next
    refresh."""
    px = tmp_path / "gfx.bin"
    px.write_bytes(bytes((i * 7 + 3) & 0xFF for i in range(0x4000)))

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window._load_pixel(str(px))
    window._hex_dock.setVisible(True)
    view = window._hex_panel._view

    # The scrollbar counts the file's rows, not the window's.
    assert view.verticalScrollBar().maximum() >= 0x4000 // 16 - view.rows_per_page()

    view.verticalScrollBar().setValue(0x300)
    window._refresh_hex()  # the offset has not moved
    assert view.first_visible_row() == 0x300

    # Moving the view does pull the dump back to the offset's row.
    window._set_byte_position(0x800)
    assert view.first_visible_row() == 0x800 // 16


def test_hex_dump_cursor_keys_move_by_byte_row_and_page(qtbot, tmp_path) -> None:
    """The byte cursor's steps: a byte, a row, a whole page of rows, and the ends
    of the file. The page step is the one worth pinning - it is the only one that
    depends on how tall the dock happens to be."""
    px = tmp_path / "gfx.bin"
    px.write_bytes(bytes((i * 7 + 3) & 0xFF for i in range(0x4000)))

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window._load_pixel(str(px))
    window._hex_dock.setVisible(True)
    view = window._hex_panel._view
    view.select(0x100, 1)
    page = view.rows_per_page() * 16

    for key, expected in (
        (Qt.Key.Key_Right, 0x101),
        (Qt.Key.Key_Left, 0x100),
        (Qt.Key.Key_Down, 0x110),
        (Qt.Key.Key_Up, 0x100),
        (Qt.Key.Key_PageDown, 0x100 + page),
        (Qt.Key.Key_PageUp, 0x100),
        (Qt.Key.Key_Home, 0),
        (Qt.Key.Key_End, 0x3FFF),
    ):
        qtbot.keyClick(view, key)
        assert view.selection() == (expected, 1), key

    # Shift extends from the anchor instead of dragging it along.
    view.select(0x20, 1)
    qtbot.keyClick(view, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert view.selection() == (0x20, 17)


def test_hex_dump_follows_the_canvas_selection_while_the_switch_is_on(
    qtbot, tmp_path
) -> None:
    """Selecting on the canvas scrolls the dump onto those bytes — the point of
    the switch is that it can be turned off, and then the dump holds the place
    the reader scrolled it to whatever the canvas does."""
    px = tmp_path / "gfx.bin"
    px.write_bytes(bytes((i * 7 + 3) & 0xFF for i in range(0x4000)))

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window._load_pixel(str(px))
    window._hex_dock.setVisible(True)
    panel = window._hex_panel
    view = panel._view
    tb = window._doc.bytes_per_tile

    # Scrolled off elsewhere, a selection pulls the dump back onto its bytes.
    view.verticalScrollBar().setValue(0x300)
    window._on_slots_selected(0, 2)
    assert view.first_visible_row() == 0

    # Off: the selection is still tinted, but the dump stays where it was put.
    panel._follow.setChecked(False)
    view.verticalScrollBar().setValue(0x300)
    window._on_slots_selected(4, 6)
    assert view.first_visible_row() == 0x300
    assert window._selection_byte_range() == (4 * tb, 3 * tb)

    # Turning it back on catches up with the selection made while it was off.
    panel._follow.setChecked(True)
    assert view.first_visible_row() == 4 * tb // 16
