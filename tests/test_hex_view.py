"""Hex-view panel: the dump-building math (Qt-free) and the panel wiring."""

from __future__ import annotations

from celpix.ui.hex_view_panel import hex_rows
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
    assert window._hex_panel._view.toPlainText() == ""

    window._hex_dock.setVisible(True)  # fires visibilityChanged -> refresh
    text = window._hex_panel._view.toPlainText()
    # The dump starts at the current offset's row and shows the file's bytes
    # (03, 0a, 11, ...), addressed in the navbar's format (flat hex here).
    assert text.splitlines()[0].split()[:4] == ["0x000000", "03", "0a", "11"]

    # Moving the view re-dumps from the new position.
    window._set_byte_position(0x40)
    line = window._hex_panel._view.toPlainText().splitlines()[0]
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
    assert "<span" not in window._hex_panel._view.toHtml()

    # Selecting a run of on-screen slots highlights those tiles' bytes: the run
    # maps to a contiguous byte range (nudge + tile*bytes_per_tile) and the
    # dump gains a highlighted span.
    window._on_tiles_selected(0, 2)  # slots 0..2 at offset 0 -> tiles 0..2
    assert window._selection_byte_range() == (0, 3 * tb)
    assert "<span" in window._hex_panel._view.toHtml()
