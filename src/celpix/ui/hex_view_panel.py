"""The hex-view panel — a raw hex dump of the file at the current offset.

A presentation-only companion to the canvas, in the spirit of the decompression
overlay: the main window feeds it the document's raw bytes plus where the view
sits, and it renders a classic address · hex · ASCII dump. It owns no model and
decides nothing — switching entries, moving the offset, or changing the address
format just re-feeds it. It lives in a dock so the Panels menu can toggle it; it
starts hidden and the main window only refreshes it while it is visible.

The dump math (row alignment, the ASCII gutter, which columns fall inside the
on-screen window) lives in :func:`hex_rows`, kept Qt-free so it can be unit
tested headless; the widget only turns those rows into styled HTML.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from html import escape

from PySide6.QtGui import QFontDatabase, QPalette
from PySide6.QtWidgets import QTextEdit, QVBoxLayout, QWidget

from celpix.ui.widgets import take_editing_shortcut

BYTES_PER_ROW = 16


class _HexView(QTextEdit):
    """The dump's text area, a shortcut island while focused.

    The canvas editing shortcuts (Cut/Copy/Paste/Select All/Delete) yield here
    rather than acting on the canvas selection behind the panel. Copy and Select
    All then do their natural thing on the hex text (the view's own keys); the
    rest are inert on this read-only dump - the point is only that they don't
    reach the canvas. Its arrow keys stay with the text cursor too: the app-wide
    navigation filter treats a focused ``QTextEdit`` as one of its yield cases.
    """

    def event(self, event) -> bool:  # noqa: ANN001 — Qt override
        if take_editing_shortcut(event):
            return True
        return super().event(event)


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


def hex_rows(
    data: bytes,
    region_start: int,
    region_end: int,
    addr_of: Callable[[int], str],
    highlight: tuple[int, int] | None = None,
    per_row: int = BYTES_PER_ROW,
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
    """
    hi_start, hi_end = (
        (highlight[0], highlight[0] + highlight[1]) if highlight else (0, 0)
    )
    rows: list[HexRow] = []
    for base in range(region_start, region_end, per_row):
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
        rows.append(HexRow(addr_of(base), cells, "".join(chars), hi_from, hi_to))

    width = max((len(row.address) for row in rows), default=0)
    return [
        HexRow(
            row.address.rjust(width), row.hex_cells, row.ascii, row.hi_from, row.hi_to
        )
        for row in rows
    ]


class HexViewPanel(QWidget):
    """Presentation-only hex dump of the current document around the view."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._view = _HexView()
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        # A fixed-pitch font is what makes the columns line up; the OS monospace
        # face (Consolas/Menlo/DejaVu Sans Mono) is a safe cross-platform pick.
        self._view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)

    def clear(self) -> None:
        """Empty the dump (nothing open)."""
        self._view.clear()

    def show_bytes(
        self,
        data: bytes,
        region_start: int,
        region_end: int,
        addr_of: Callable[[int], str],
        highlight: tuple[int, int] | None = None,
    ) -> None:
        """Render ``data[region_start:region_end]`` as a hex dump.

        ``highlight`` tints the bytes currently shown on the canvas so the dump
        reads as "here is what you're looking at, in hex". The view scrolls back
        to the top, where the current offset's row sits.
        """
        rows = hex_rows(data, region_start, region_end, addr_of, highlight)
        pal = self.palette()
        # Half the brightness of the system highlight, so the tint reads as a
        # marker over the dump rather than a full selection.
        background = pal.color(QPalette.ColorRole.Highlight).darker(60)
        hi_style = (
            f"background-color:{background.name()};"
            f"color:{pal.color(QPalette.ColorRole.HighlightedText).name()}"
        )
        body = "\n".join(self._row_html(row, hi_style) for row in rows)
        self._view.setHtml(f"<pre style='margin:0'>{body}</pre>")
        self._view.moveCursor(self._view.textCursor().MoveOperation.Start)

    @staticmethod
    def _row_html(row: HexRow, hi_style: str) -> str:
        def tint(col: int, text: str) -> str:
            if row.hi_from is not None and row.hi_from <= col < row.hi_to:
                return f"<span style='{hi_style}'>{text}</span>"
            return text

        hex_part = " ".join(
            tint(col, cell if cell else "  ") for col, cell in enumerate(row.hex_cells)
        )
        ascii_part = "".join(tint(col, escape(ch)) for col, ch in enumerate(row.ascii))
        return f"{escape(row.address)}  {hex_part}  {ascii_part}"
