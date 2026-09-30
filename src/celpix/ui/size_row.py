"""The "how many units" row three dialogs share: a count and what it comes to.

New File, the container dialog and the slice dialog each state a size in the
units the user thinks in — tiles, cells, a palette's colours — and each shows
what that count works out to in bytes, which is the codec's arithmetic
(:func:`~celpix.pipeline.pipeline.blank_size`). The limits, the captions and
the "N bytes (currently M)" note live here once, so the three dialogs cannot
drift into three spellings of the one question.

The limits are **this row's own**. They bound a size someone is *choosing* to
work at — a file bigger than any of them is still a file, and a row seeded from
one is never clamped below it — so they are deliberately not the view's Cols
and Rows ranges, which answer a different question (how a region is looked at).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QHBoxLayout, QLabel, QSpinBox, QWidget

from celpix.core.capabilities import ContentKind
from celpix.core.errors import PipelineError
from celpix.pipeline import pipeline
from celpix.plugins.registry import Registry
from celpix.ui.widgets import value_spin

__all__ = [
    "GROWTH_TIPS",
    "MAX_COLORS",
    "MAX_COLUMNS",
    "MAX_ROWS",
    "SIZE_CAPTIONS",
    "UnitCountRow",
]

# A grid of up to 512 x 256 units: room for any sheet or screen a user is
# likely to *start* at, without letting a slip of the keyboard make a file of
# hundreds of megabytes. The dialog's own limits, not the view's (module docs).
MAX_COLUMNS = 512
MAX_ROWS = 256
# A palette is a run rather than a grid, so it is bounded by what a palette is
# instead: 4096 is sixteen 256-color CGRAM dumps' worth, comfortably past every
# hardware palette celPix reads.
MAX_COLORS = 4096

SIZE_CAPTIONS = {
    ContentKind.PIXELS: "Tiles:",
    ContentKind.TILEMAP: "Cells:",
    ContentKind.PALETTE: "Colors:",
}

# What moving a total does to the file, per kind — the second line of every
# size tooltip that counts a total, under a first line saying what is counted.
GROWTH_TIPS = {
    ContentKind.PIXELS: "Growing appends blank tiles; shrinking drops the last ones",
    ContentKind.TILEMAP: "Growing appends empty cells; shrinking drops the last ones",
    ContentKind.PALETTE: "Growing appends black; shrinking drops the last ones",
}


class UnitCountRow:
    """A spin stating a total count of units, and a note saying it in bytes.

    Seeded to exactly what the region holds (``units_before``), so it asks for
    no resize until it is moved: :meth:`resize_units` compares in **bytes**
    rather than units, because a packed palette rounds up to a whole read unit
    and several counts share one length. The ceiling is what the limits above
    allow, raised where the region is **already** bigger: an 8 MB ROM holds
    more tiles than any grid, and a spin that clamped the seed below the true
    size would read as a shrink nobody asked for.

    Not a widget: the dialogs lay out :attr:`caption`, :attr:`field` and
    :attr:`note` in rows of their own forms, and each decides separately when
    the spin is live (:meth:`show_bytes` and :meth:`show_reason`).
    """

    def __init__(
        self,
        kind: ContentKind,
        codec_id: str,
        registry: Registry,
        units_before: int,
        tip: str,
        on_change: Callable[..., None],
        *,
        noun: str = "bytes",
    ) -> None:
        self._kind = kind
        self._codec_id = codec_id
        self._registry = registry
        self.units_before = max(0, units_before)
        self._noun = noun
        ceiling = MAX_COLORS if kind is ContentKind.PALETTE else MAX_COLUMNS * MAX_ROWS
        # An empty region seeds 0 and may say so; anything else keeps 1 as the
        # floor, since a file emptied by a spin is a stranger request than a
        # file that already was.
        self.spin: QSpinBox = value_spin(
            min(1, self.units_before),
            max(ceiling, self.units_before),
            self.units_before,
            on_change,
        )
        self.caption = QLabel(SIZE_CAPTIONS.get(kind, "Size:"))
        self.note = QLabel()
        self.note.setWordWrap(True)
        for widget in (self.spin, self.caption):
            widget.setToolTip(tip)
        # The spin in a holder that keeps its own width: a form's field column
        # stretches, and a count box pulled to the dialog's width reads as a
        # text field.
        self.field = QWidget()
        self.field.setToolTip(tip)
        row = QHBoxLayout(self.field)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.spin)
        row.addStretch(1)
        # The length the region has now, to measure a change against. Asked
        # once: it is the codec's arithmetic and cannot move while a dialog is
        # open.
        self.bytes_before = self.byte_size(self.units_before)

    def units(self) -> int:
        """Tiles, cells or colors the spin is stating."""
        return self.spin.value()

    def byte_size(self, units: int) -> int | None:
        """``units`` in bytes, or ``None`` where the codec would not say.

        A codec is a plugin and may refuse a size — a preset whose params its
        engine rejects, a third-party format that raises. That is a reason to
        stop offering the row, not to stop the dialog around it.
        """
        if not self._codec_id:
            return None
        try:
            return pipeline.blank_size(
                self._kind, self._codec_id, units, self._registry
            )
        except PipelineError:
            return None

    def show_bytes(self) -> None:
        """Live the spin and say what it comes to, against what there is now.

        The current length is worth repeating only when it is not the answer: a
        spin still on its seed is describing the region as it stands.
        """
        size = self.byte_size(self.units())
        self.spin.setEnabled(size is not None and self.bytes_before is not None)
        if size is None or self.bytes_before is None:
            self.note.setText("This format reports no size for that count.")
        elif size == self.bytes_before:
            self.note.setText(f"{size:,} {self._noun}")
        else:
            self.note.setText(
                f"{size:,} {self._noun} (currently {self.bytes_before:,})"
            )

    def show_reason(self, reason: str) -> None:
        """Grey the spin and say why it cannot be acted on."""
        self.spin.setEnabled(False)
        self.note.setText(reason)

    def resize_units(self) -> int | None:
        """The count to resize to, or ``None`` where it asks for nothing.

        ``None`` while the spin is greyed, where the codec will not measure the
        count, and where the count works out to the length the region already
        has — which is what the spin opens on.
        """
        if not self.spin.isEnabled():
            return None
        size = self.byte_size(self.units())
        if size is None or size == self.bytes_before:
            return None
        return self.units()
