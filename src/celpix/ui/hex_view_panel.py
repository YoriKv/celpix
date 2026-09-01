"""The hex-view panel — a scrollable raw hex dump of the file being edited.

A presentation-only companion to the canvas, in the spirit of the decompression
overlay: the main window feeds it the document's raw bytes plus where the view
sits, and it renders a classic address · hex · ASCII dump. It owns no model and
decides nothing — switching entries, moving the offset, or changing the address
format just re-feeds it, and nothing typed here can alter a byte. It lives in a
dock so the Panels menu can toggle it; it starts hidden and the main window only
refreshes it while it is visible.

The dump covers the **whole** file rather than a window around the offset, so
the panel can be read as an inspector in its own right: its own Go to box and
find box move the dump without touching the canvas, and its Follow selection
switch says whether picking something on the canvas drags the dump onto those
bytes or leaves it where it was scrolled. That is affordable because
the view is virtualized — the scrollbar counts rows, and :meth:`HexDumpView.paintEvent`
builds only the rows on screen — so a multi-megabyte ROM costs the same as a
tile bank.

The dump math (row alignment, the ASCII gutter, which columns fall inside the
on-screen window), the find-box grammar and the search itself are Qt-free so
they can be unit tested headless; the widget turns those rows into pixels.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import ceil

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QFontDatabase, QFontMetricsF, QKeyEvent, QMouseEvent, QPainter
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from celpix.ui.widgets import ShortcutIsland, load_bool_setting, save_bool_setting

BYTES_PER_ROW = 16

# QSettings key for the Follow selection switch. A local preference like the
# grid's and the pins': whether picking a tile should drag the dump along says
# how you are reading the file right now, not anything about the file.
FOLLOW_SELECTION_KEY = "hex/follow_selection"

# Character columns of one rendered line, past the address: two spaces, the hex
# cells ("xx " each, no trailing space), two spaces, then the ASCII gutter.
_HEX_GAP = 2
_HEX_COLS = BYTES_PER_ROW * 3 - 1
_ASCII_GAP = 2


@dataclass(frozen=True)
class HexRow:
    """One rendered dump line: the row's address, its byte cells, its ASCII
    gutter, and the half-open column span ``[hi_from, hi_to)`` that falls inside
    the highlighted range (``hi_from`` is ``None`` when the row has none).

    ``hex_cells`` and ``ascii`` are always ``per_row`` wide; a cell past the end
    of the data is an empty string (hex) and a space (ASCII), so trailing
    partial rows still line up under the columns above them.
    """

    address: str
    hex_cells: list[str]
    ascii: str
    hi_from: int | None
    hi_to: int

    def text(self) -> str:
        """The line as the dump draws it — address, hex cells, ASCII gutter."""
        cells = " ".join(cell if cell else "  " for cell in self.hex_cells)
        return f"{self.address}{' ' * _HEX_GAP}{cells}{' ' * _ASCII_GAP}{self.ascii}"


def hex_rows(
    data: bytes,
    region_start: int,
    region_end: int,
    addr_of: Callable[[int], str],
    highlight: tuple[int, int] | None = None,
    per_row: int = BYTES_PER_ROW,
    min_addr_width: int = 0,
) -> list[HexRow]:
    """Build the dump rows for ``data[region_start:region_end]``.

    ``region_start`` is expected to be a multiple of ``per_row`` (the caller
    aligns to a row boundary so columns stay put as the offset moves).
    ``addr_of`` maps a byte index in ``data`` to its displayed address — the
    same address format the navbar uses, so the two agree. ``highlight`` is a
    ``(start, length)`` byte range (typically the window currently on the
    canvas); each row reports the sub-span of its columns that it covers.

    Addresses are right-justified to a common width, so the hex and ASCII
    columns align even when the address format yields varying lengths.
    ``min_addr_width`` floors that width: a scrolling view builds only the rows
    on screen, and without it the columns would shuffle sideways the moment
    scrolling brought a longer address (``0xffff`` → ``0x10000``) into view.
    """
    hi_start, hi_end = (
        (highlight[0], highlight[0] + highlight[1]) if highlight else (0, 0)
    )
    # Addresses first: they are right-justified to the widest one, and building
    # every row twice to discover that width is a whole extra pass over a dump
    # the view rebuilds on each offset move.
    starts = list(range(region_start, region_end, per_row))
    addresses = [addr_of(base) for base in starts]
    width = max((len(address) for address in addresses), default=0)
    width = max(width, min_addr_width)
    rows: list[HexRow] = []
    for address, base in zip(addresses, starts, strict=True):
        cells: list[str] = []
        chars: list[str] = []
        for col in range(per_row):
            idx = base + col
            if idx < len(data):
                value = data[idx]
                cells.append(f"{value:02x}")
                chars.append(chr(value) if 0x20 <= value <= 0x7E else ".")
            else:
                cells.append("")
                chars.append(" ")
        # Overlap of this row's byte span with the highlighted range, expressed
        # in columns; None when the range misses the row entirely.
        lo, hi = max(hi_start, base), min(hi_end, base + per_row)
        if lo < hi:
            hi_from, hi_to = lo - base, hi - base
        else:
            hi_from, hi_to = None, 0
        rows.append(HexRow(address.rjust(width), cells, "".join(chars), hi_from, hi_to))
    return rows


def parse_find_query(text: str) -> bytes | None:
    """Parse the find box's text into the bytes to look for, or None if it names
    no byte string.

    Two grammars, told apart by quoting, because both are how a byte pattern is
    naturally written down: ``"NES"`` (either quote character) searches for
    those characters, while ``4e 45 53``, ``4e4553`` and ``$4e $45 $53`` all
    search for the same three bytes. An odd number of hex digits is rejected
    rather than padded — half a byte names no byte, and guessing which half
    would be a coin toss.
    """
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        body = text[1:-1]
        # latin-1 maps every code point below 256 to the byte of that value, so
        # a typed 'é' searches for 0xe9 instead of UTF-8's two bytes - a dump is
        # read one byte per character, so that is the match the user can see.
        return body.encode("latin-1", "replace") if body else None
    digits = ""
    for token in text.replace(",", " ").split():
        digits += token.removeprefix("$").removeprefix("0x").removeprefix("0X")
    if not digits or len(digits) % 2:
        return None
    try:
        return bytes.fromhex(digits)
    except ValueError:
        return None


def find_bytes(
    data: bytes, needle: bytes, start: int, backwards: bool = False
) -> tuple[int, bool] | None:
    """Search ``data`` for ``needle`` from byte ``start``, wrapping around.

    Returns the match's offset and whether the search had to wrap to find it
    (the panel says so, so a match *behind* where the user was looking doesn't
    read as the search having stayed put), or None when there is no match
    anywhere.
    """
    if not needle:
        return None
    start = max(0, start)
    if backwards:
        # rfind's end bound is exclusive of matches starting past it, so this
        # asks for the last match beginning strictly before `start`.
        at = data.rfind(needle, 0, start + len(needle) - 1)
    else:
        at = data.find(needle, start)
    if at >= 0:
        return at, False
    at = data.rfind(needle) if backwards else data.find(needle)
    return (at, True) if at >= 0 else None


class HexDumpView(ShortcutIsland, QAbstractScrollArea):
    """The dump itself: a virtualized address · hex · ASCII view of ``data``.

    Holds the whole byte string but renders only the rows the viewport can show,
    so the scrollbar spans the file at any size. Two tints sit on top of it, and
    they mean different things: the **window** highlight is what the canvas is
    currently drawing (fed by the main window, dimmed so it reads as a marker
    rather than a selection) and the **selection** is the user's own click-drag
    in the dump, which is what Copy copies.

    Copy and Select All do their natural thing on the dump (its own keys); the
    rest of the claimed editing shortcuts are inert on this read-only view - the
    point is only that they don't reach the canvas behind it
    (:class:`~celpix.ui.widgets.ShortcutIsland`). Its arrow keys stay with its
    own byte cursor too: the app-wide navigation filter lists this widget as one
    of its yield cases.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._data: bytes = b""
        self._addr_of: Callable[[int], str] = lambda index: f"{index:04x}"
        self._highlight: tuple[int, int] | None = None
        self._anchor: int | None = None
        self._addr_width = 0
        # The selection is a half-open byte range kept as anchor + cursor, so a
        # drag backwards past its start reads as a selection the other way round
        # rather than an empty one.
        self._sel_anchor: int | None = None
        self._sel_cursor: int | None = None
        # A fixed-pitch font is what makes the columns line up; the OS monospace
        # face (Consolas/Menlo/DejaVu Sans Mono) is a safe cross-platform pick.
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setBackgroundRole(self.backgroundRole())
        self.viewport().setAutoFillBackground(True)

    @property
    def data(self) -> bytes:
        """The bytes on show — the whole file, not just the rows drawn."""
        return self._data

    # -- geometry ---------------------------------------------------------------

    def _cell(self) -> tuple[float, float]:
        """One character's width and one row's height, in device pixels."""
        metrics = QFontMetricsF(self.font())
        return metrics.horizontalAdvance("0"), metrics.height()

    def _ascii_col(self) -> int:
        """Character column the ASCII gutter starts at."""
        return self._addr_width + _HEX_GAP + _HEX_COLS + _ASCII_GAP

    def _line_cols(self) -> int:
        return self._ascii_col() + BYTES_PER_ROW

    def _total_rows(self) -> int:
        return ceil(len(self._data) / BYTES_PER_ROW)

    def rows_per_page(self) -> int:
        """How many whole rows fit in the viewport (at least one)."""
        _, row_height = self._cell()
        return max(1, int(self.viewport().height() // row_height))

    def first_visible_row(self) -> int:
        return self.verticalScrollBar().value()

    def visible_rows(self) -> list[HexRow]:
        """The rows currently on screen, as :func:`hex_rows` built them.

        The panel's own read-back, and what the tests assert on: it is the dump
        as drawn, without going through pixels.
        """
        first = self.first_visible_row()
        start = first * BYTES_PER_ROW
        # One row past the page: the last row is usually cut off by the viewport
        # rather than falling outside it, and an undrawn one reads as the dump
        # ending early.
        end = min(len(self._data), (first + self.rows_per_page() + 1) * BYTES_PER_ROW)
        if start >= end:
            return []
        return hex_rows(
            self._data,
            start,
            end,
            self._addr_of,
            self._highlight,
            min_addr_width=self._addr_width,
        )

    def dump_text(self) -> str:
        """The visible rows as plain text — one line per row."""
        return "\n".join(row.text() for row in self.visible_rows())

    # -- feeding ----------------------------------------------------------------

    def set_data(
        self,
        data: bytes,
        anchor: int,
        addr_of: Callable[[int], str],
        highlight: tuple[int, int] | None = None,
        *,
        follow_selection: bool = True,
        anchor_is_selection: bool = False,
    ) -> None:
        """Show ``data``, with ``anchor``'s row scrolled to the top.

        The dump only jumps to ``anchor`` when the anchor or the byte string
        itself has changed. A refresh that changes neither - a re-render, a
        theme change, a selection that stays inside the same window - leaves the
        scroll position alone, so scrolling off to read another part of the file
        is not undone by the next repaint.

        ``follow_selection`` additionally brings a *changed* ``highlight`` into
        view, which is what makes clicking a tile on the canvas land on its
        bytes here. Only a change moves the dump: a repaint that re-feeds the
        same selection leaves a reader who scrolled elsewhere where they are.
        ``anchor_is_selection`` says the anchor *is* that selection (a tilemap
        has no view window to anchor to, so it anchors on the selected record),
        which puts its jump under the same switch instead of moving the dump
        with following off.
        """
        moved = anchor != self._anchor and (follow_selection or not anchor_is_selection)
        follow = moved or data is not self._data
        changed = highlight != self._highlight
        self._data, self._anchor, self._addr_of = data, anchor, addr_of
        self._highlight = highlight
        # The address column is floored by the widest address in the *file*, not
        # in the rows on screen, so the columns hold still while scrolling. The
        # first and last rows bound it for every format we render: flat hex and
        # every bank layout grow their text with the offset.
        last_row = max(0, (len(data) - 1) // BYTES_PER_ROW * BYTES_PER_ROW)
        self._addr_width = max(len(addr_of(0)), len(addr_of(last_row))) if data else 0
        self._update_scrollbars()
        if follow:
            self.scroll_to_byte(anchor)
        if follow_selection and changed and highlight is not None:
            self.reveal_range(*highlight)
        self.viewport().update()

    def clear(self) -> None:
        """Empty the dump (nothing open)."""
        self._data, self._anchor, self._addr_width = b"", None, 0
        self._highlight = None
        self._sel_anchor = self._sel_cursor = None
        self._update_scrollbars()
        self.viewport().update()

    def scroll_to_byte(self, index: int) -> None:
        """Put ``index``'s row on the top line."""
        row = max(0, min(index, max(0, len(self._data) - 1))) // BYTES_PER_ROW
        self.verticalScrollBar().setValue(row)

    def reveal_byte(self, index: int) -> None:
        """Scroll only as far as it takes to bring ``index`` on screen."""
        row = max(0, min(index, max(0, len(self._data) - 1))) // BYTES_PER_ROW
        first, page = self.first_visible_row(), self.rows_per_page()
        if row < first:
            self.verticalScrollBar().setValue(row)
        elif row >= first + page:
            self.verticalScrollBar().setValue(row - page + 1)

    def reveal_range(self, start: int, length: int) -> None:
        """Scroll ``length`` bytes from ``start`` into view, showing as much of
        the range as fits.

        The end first, then the start: a range taller than the viewport can only
        have one of its ends on screen, and the start is the one that says where
        the range *is*.
        """
        if length <= 0:
            return
        self.reveal_byte(start + length - 1)
        self.reveal_byte(start)

    def reveal_highlight(self) -> None:
        """Scroll the highlighted range - what the canvas has selected - into
        view. What the panel's Follow selection switch does when it is turned
        on, so the dump catches up with a selection made while it was off."""
        if self._highlight is not None:
            self.reveal_range(*self._highlight)

    # -- selection --------------------------------------------------------------

    def selection(self) -> tuple[int, int] | None:
        """The user's selected ``(start, length)`` byte range, or None."""
        if self._sel_anchor is None or self._sel_cursor is None:
            return None
        lo, hi = sorted((self._sel_anchor, self._sel_cursor))
        return lo, hi - lo + 1

    def select(self, start: int, length: int) -> None:
        """Select ``length`` bytes from ``start`` and scroll them into view."""
        if not self._data or length <= 0:
            return
        last = len(self._data) - 1
        anchor = max(0, min(start, last))
        self._sel_anchor = anchor
        self._sel_cursor = max(0, min(start + length - 1, last))
        self.reveal_byte(anchor)
        self.viewport().update()

    def select_all(self) -> None:
        self.select(0, len(self._data))

    def copy(self) -> None:
        """Copy the selected rows as dump text, or the visible page if nothing
        is selected — the same lines the panel is showing, so what lands in the
        clipboard is what was on screen."""
        span = self.selection()
        if span is None:
            text = self.dump_text()
        else:
            start = span[0] // BYTES_PER_ROW * BYTES_PER_ROW
            end = min(
                len(self._data),
                ceil((span[0] + span[1]) / BYTES_PER_ROW) * BYTES_PER_ROW,
            )
            rows = hex_rows(
                self._data,
                start,
                end,
                self._addr_of,
                self._highlight,
                min_addr_width=self._addr_width,
            )
            text = "\n".join(row.text() for row in rows)
        if text:
            QApplication.clipboard().setText(text)

    def _byte_at(self, x: float, y: float) -> int | None:
        """The byte under a viewport point, or None off the hex/ASCII columns."""
        if not self._data:
            return None
        char_width, row_height = self._cell()
        row = self.first_visible_row() + int(y // row_height)
        col_char = int((x + self.horizontalScrollBar().value()) // char_width)
        hex_start = self._addr_width + _HEX_GAP
        ascii_start = self._ascii_col()
        if hex_start <= col_char < hex_start + _HEX_COLS:
            col = (col_char - hex_start) // 3
        elif ascii_start <= col_char < ascii_start + BYTES_PER_ROW:
            col = col_char - ascii_start
        else:
            return None
        index = row * BYTES_PER_ROW + col
        return index if 0 <= index < len(self._data) else None

    # -- events -----------------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: D102, N802 — Qt override
        super().resizeEvent(event)
        self._update_scrollbars()

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: D102, N802
        self.viewport().update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: D102, N802
        index = self._byte_at(event.position().x(), event.position().y())
        if index is None:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            if self._sel_anchor is None:
                self._sel_anchor = index
        else:
            self._sel_anchor = index
        self._sel_cursor = index
        self.viewport().update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: D102, N802
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        index = self._byte_at(event.position().x(), event.position().y())
        if index is not None:
            self._sel_cursor = index
            self.reveal_byte(index)
            self.viewport().update()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: D102, N802
        key, mods = event.key(), event.modifiers()
        ctrl = mods & Qt.KeyboardModifier.ControlModifier
        if ctrl and key == Qt.Key.Key_C:
            self.copy()
        elif ctrl and key == Qt.Key.Key_A:
            self.select_all()
        elif key == Qt.Key.Key_Escape:
            self._sel_anchor = self._sel_cursor = None
            self.viewport().update()
        elif key in _CURSOR_KEYS and self._data:
            target = self._cursor_target(key)
            if not mods & Qt.KeyboardModifier.ShiftModifier or self._sel_anchor is None:
                self._sel_anchor = target
            self._sel_cursor = target
            self.reveal_byte(target)
            self.viewport().update()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def _cursor_target(self, key: int) -> int:
        """Where a cursor key moves the byte cursor, clamped to the file."""
        here = self._sel_cursor if self._sel_cursor is not None else 0
        page = self.rows_per_page() * BYTES_PER_ROW
        if key == Qt.Key.Key_Home:
            target = 0
        elif key == Qt.Key.Key_End:
            target = len(self._data) - 1
        elif key == Qt.Key.Key_PageUp:
            target = here - page
        elif key == Qt.Key.Key_PageDown:
            target = here + page
        else:
            target = here + _CURSOR_STEPS[key]
        return max(0, min(target, len(self._data) - 1))

    def _update_scrollbars(self) -> None:
        char_width, row_height = self._cell()
        page = self.rows_per_page()
        vertical = self.verticalScrollBar()
        vertical.setRange(0, max(0, self._total_rows() - page))
        vertical.setPageStep(page)
        vertical.setSingleStep(1)
        line_width = self._line_cols() * char_width
        horizontal = self.horizontalScrollBar()
        horizontal.setRange(0, max(0, int(line_width - self.viewport().width())))
        horizontal.setPageStep(self.viewport().width())
        horizontal.setSingleStep(int(char_width) or 1)

    def paintEvent(self, event) -> None:  # noqa: D102, N802 — Qt override
        rows = self.visible_rows()
        if not rows:
            return
        painter = QPainter(self.viewport())
        painter.setFont(self.font())
        metrics = QFontMetricsF(self.font())
        char_width, row_height = self._cell()
        left = -self.horizontalScrollBar().value()
        palette = self.palette()
        text_pen = palette.text().color()
        marked_pen = palette.highlightedText().color()
        # Half the brightness of the system highlight, so the window tint reads
        # as a marker over the dump; the user's own selection gets the real one.
        window_fill = palette.highlight().color().darker(60)
        selection_fill = palette.highlight().color()
        selection = self.selection()
        sel_start, sel_end = (
            (selection[0], selection[0] + selection[1]) if selection else (0, 0)
        )
        hex_start, ascii_start = self._addr_width + _HEX_GAP, self._ascii_col()
        first = self.first_visible_row()

        for line, row in enumerate(rows):
            top = line * row_height
            baseline = top + metrics.ascent()
            base_index = (first + line) * BYTES_PER_ROW
            # Role per column: the user's selection wins over the window tint,
            # since it is the one they are actively pointing at.
            roles = [
                2
                if sel_start <= base_index + col < sel_end
                else 1
                if row.hi_from is not None and row.hi_from <= col < row.hi_to
                else 0
                for col in range(BYTES_PER_ROW)
            ]
            for col, role in enumerate(roles):
                if not role:
                    continue
                fill = selection_fill if role == 2 else window_fill
                # A run of same-role columns is filled across the separator too,
                # so a highlighted range reads as one band and not as gapped
                # pairs of digits.
                joined = col + 1 < BYTES_PER_ROW and roles[col + 1] == role
                painter.fillRect(
                    QRectF(
                        left + (hex_start + col * 3) * char_width,
                        top,
                        char_width * (3 if joined else 2),
                        row_height,
                    ),
                    fill,
                )
                painter.fillRect(
                    QRectF(
                        left + (ascii_start + col) * char_width,
                        top,
                        char_width,
                        row_height,
                    ),
                    fill,
                )
            painter.setPen(text_pen)
            painter.drawText(QPointF(left, baseline), row.text())
            # Repaint the tinted cells in the highlight's own text colour; drawn
            # over the line rather than instead of it, so the untinted run needs
            # no splitting.
            painter.setPen(marked_pen)
            for col, role in enumerate(roles):
                if not role:
                    continue
                cell = row.hex_cells[col] or "  "
                painter.drawText(
                    QPointF(left + (hex_start + col * 3) * char_width, baseline), cell
                )
                painter.drawText(
                    QPointF(left + (ascii_start + col) * char_width, baseline),
                    row.ascii[col],
                )


# Byte-cursor steps for the keys that move it by a fixed distance. The page keys
# and Home/End move by a distance only the viewport or the file knows, so they
# are resolved in _cursor_target rather than tabulated here.
_CURSOR_STEPS: dict[int, int] = {
    Qt.Key.Key_Left: -1,
    Qt.Key.Key_Right: 1,
    Qt.Key.Key_Up: -BYTES_PER_ROW,
    Qt.Key.Key_Down: BYTES_PER_ROW,
}
_CURSOR_KEYS = (
    *_CURSOR_STEPS,
    Qt.Key.Key_PageUp,
    Qt.Key.Key_PageDown,
    Qt.Key.Key_Home,
    Qt.Key.Key_End,
)


def _no_address(_text: str) -> int | None:
    """The Go to box's parser before the panel has been fed a document: with no
    address format in hand, nothing typed names a byte."""
    return None


class HexViewPanel(QWidget):
    """Presentation-only hex dump of the current document, with its own Go to
    and find boxes.

    Both boxes move the **dump** and nothing else. That is the point of them:
    the navbar's offset box moves the canvas, so checking a header or a pointer
    table through it means losing the view you were working on and putting it
    back afterwards. Here the canvas holds still and its window stays tinted in
    the dump, so what you looked up and what you are editing are on screen
    together.

    Traffic the other way — the canvas moving the dump — is the Follow selection
    switch, on by default: selecting a tile or a cell scrolls its bytes into
    view. It is a switch rather than a rule because the two readings are both
    wanted: following, the dump answers "what are these bytes"; not following,
    it stays on the header or table you scrolled to while you edit elsewhere.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._view = HexDumpView()
        self._parse_addr: Callable[[str], int | None] = _no_address
        self._needle: bytes | None = None

        goto_tip = (
            "Scroll the dump to an address, written the way\n"
            "the navbar writes one. The canvas does not move —\n"
            "this jumps the dump only.\n"
            "Enter to jump."
        )
        self._goto = QLineEdit()
        self._goto.setPlaceholderText("address")
        self._goto.setToolTip(goto_tip)
        self._goto.setMaximumWidth(120)
        self._goto.returnPressed.connect(self._do_goto)
        goto_label = QLabel("&Go to:")
        goto_label.setBuddy(self._goto)
        goto_label.setToolTip(goto_tip)

        find_tip = (
            "Find bytes in the file: hex digits (4e 45 53, 4e4553,\n"
            '$4e $45 $53), or characters in quotes ("NES").\n'
            "Enter finds the next match, Shift+Enter the previous;\n"
            "the search wraps around the end of the file."
        )
        self._find = QLineEdit()
        self._find.setPlaceholderText('bytes or "text"')
        self._find.setToolTip(find_tip)
        self._find.setMaximumWidth(160)
        self._find.returnPressed.connect(self._do_find_from_box)
        self._find.textChanged.connect(lambda _text: self._status.clear())
        find_label = QLabel("&Find:")
        find_label.setBuddy(self._find)
        find_label.setToolTip(find_tip)

        previous = QToolButton()
        previous.setText("◀")
        previous.setToolTip("Find the previous match (Shift+Enter)")
        previous.clicked.connect(lambda: self._do_find(backwards=True))
        following = QToolButton()
        following.setText("▶")
        following.setToolTip("Find the next match (Enter)")
        following.clicked.connect(lambda: self._do_find(backwards=False))

        self._status = QLabel()
        # A found/not-found note is feedback on what was just typed, not a label
        # for anything, so it must never stretch the boxes off to the left.
        self._status.setMinimumWidth(0)

        self._follow = QCheckBox("Follow &selection")
        self._follow.setToolTip(
            "Scroll the dump to whatever is selected on the canvas.\n"
            "Off, the dump stays where you left it and the selection\n"
            "is only tinted when it happens to be on screen."
        )
        self._follow.setChecked(load_bool_setting(FOLLOW_SELECTION_KEY, True))
        self._follow.toggled.connect(self._on_follow_toggled)

        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 2)
        bar.setSpacing(4)
        for widget in (
            goto_label,
            self._goto,
            find_label,
            self._find,
            previous,
            following,
            self._follow,
            self._status,
        ):
            bar.addWidget(widget)
        bar.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(bar)
        layout.addWidget(self._view)

    def clear(self) -> None:
        """Empty the dump (nothing open)."""
        self._view.clear()
        self._status.clear()

    def show_bytes(
        self,
        data: bytes,
        anchor: int,
        addr_of: Callable[[int], str],
        parse_addr: Callable[[str], int | None],
        highlight: tuple[int, int] | None = None,
        *,
        anchor_is_selection: bool = False,
    ) -> None:
        """Show the whole of ``data``, anchored on byte ``anchor``.

        ``addr_of`` and ``parse_addr`` are the two directions of the same
        address format the navbar uses - byte index to displayed address, and
        typed address back to byte index - so what the dump prints and what its
        Go to box accepts both agree with the rest of the window. ``highlight``
        tints the bytes currently shown on the canvas, so the dump reads as
        "here is what you're looking at, in hex", and with Follow selection on
        it also scrolls the dump onto it. ``anchor_is_selection`` puts an anchor
        that is itself the selection under that same switch
        (:meth:`HexDumpView.set_data`).
        """
        self._parse_addr = parse_addr
        self._view.set_data(
            data,
            anchor,
            addr_of,
            highlight,
            follow_selection=self._follow.isChecked(),
            anchor_is_selection=anchor_is_selection,
        )

    def _on_follow_toggled(self, checked: bool) -> None:
        """Remember the switch, and catch the dump up when it is turned on: the
        selection it should be following was made while it was off."""
        save_bool_setting(FOLLOW_SELECTION_KEY, checked)
        if checked:
            self._view.reveal_highlight()

    # -- the two boxes ----------------------------------------------------------

    def _do_goto(self) -> None:
        index = self._parse_addr(self._goto.text())
        if index is None:
            self._status.setText("Bad address")
            return
        self._status.clear()
        self._view.scroll_to_byte(index)
        self._view.select(index, 1)

    def _do_find_from_box(self) -> None:
        shift = QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
        self._do_find(backwards=bool(shift))

    def _do_find(self, backwards: bool) -> None:
        needle = parse_find_query(self._find.text())
        if needle is None:
            self._status.setText("Bad query")
            return
        # Search on from the current match rather than from where the dump is
        # scrolled, so repeated Enter walks the matches instead of finding the
        # same one each time.
        found = self._view.selection()
        if found is None:
            start = len(self._view.data) if backwards else 0
        else:
            start = found[0] - 1 if backwards else found[0] + 1
        hit = find_bytes(self._view.data, needle, start, backwards)
        if hit is None:
            self._status.setText("Not found")
            return
        index, wrapped = hit
        self._status.setText("Wrapped" if wrapped else "")
        self._view.select(index, len(needle))
