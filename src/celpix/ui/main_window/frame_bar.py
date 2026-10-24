"""The Frame bar: how a sprite map's frames are boxed, and the origin mark.

A sprite map's frames are each drawn in one shared box, and with nothing said
that box is the pieces' own bounds (:func:`~celpix.core.sprite.frame_bounds`) —
right for reading an object, and wrong for working on one, because the box then
moves every time a piece is dragged past its edge and the origin the offsets
are measured from sits wherever the bounds happen to leave it. This bar is where
the box is **said** instead: an alignment per axis for where (0, 0) sits in it,
a size it is at least, and whether the origin is drawn
(``docs/design/sprite-map.md`` §2, "The frame box"). Whatever is said, the box
still grows to hold every piece; nothing here can clip the art.

It takes the **Arrangement row's slot** — the Pattern, block and 2D controls say
how a linear run of bytes is cut into tiles, which a sprite map does not do, so
that row is hidden on one (:mod:`~celpix.ui.main_window.capability_sync`) and
this one stands in the same place, saying how *its* picture is cut. Gated on
the document being a sprite map rather than on a capability, as the binding
bar's sprite controls are: having frames is a property of the cell format, not
of the content kind.

The layout is the entry's (:attr:`~celpix.core.document.ViewOptions.frame`) and
the project file keeps it, so every control here is an undo step — the size
spins and the alignment pickers through :class:`~celpix.ui.undo_commands.
FrameCommand`, merging per control as the view axes merge per axis, and the
Origin box through the toggle command the binding bar's boxes use. The window
holds the live answer (``_frame``, ``_show_origin``), the refresh captures it
into ``doc.view`` where the sheet geometry reads it, and the bar is refilled
from that cycle, which is what lets an undo and an entry switch re-fill the
controls without each knowing about them.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QToolBar, QWidget

from celpix.core.sprite import FRAME_SIZE_MAX, FrameLayout
from celpix.ui.undo_commands import FrameCommand
from celpix.ui.widgets import CompactComboBox, add_labelled, signals_blocked, value_spin

if TYPE_CHECKING:
    from celpix.ui.main_window.window import MainWindow as _Host
else:
    _Host = object

# The pickers' rows, in the model's words (:data:`~celpix.core.sprite.FRAME_ALIGN_X`
# and ``_Y``): "Fit" first, being the default and the reading every sheet had
# before the bar.
_ALIGN_X = (("Fit", "fit"), ("Left", "left"), ("Center", "center"), ("Right", "right"))
_ALIGN_Y = (("Fit", "fit"), ("Top", "top"), ("Center", "center"), ("Bottom", "bottom"))
_ALIGN_WIDTH = 72  # "Center" and a drop arrow
_SIZE_WIDTH = 52  # four digits, the spins' range


class FrameBarMixin(_Host):
    """The Frame bar — a slice of :class:`~celpix.ui.main_window.window.MainWindow`.

    See the module docstring for what it owns and which attributes it reads that
    others create (``_frame`` and ``_show_origin`` are the window's, set in its
    constructor before the toolbar exists because the refresh captures them).
    """

    _frame: FrameLayout
    _show_origin: bool
    _frame_toolbar: QToolBar

    def _build_frame_toolbar(self, bar: QToolBar) -> None:
        """Fill ``bar`` — the row :meth:`~...interpretation.InterpretationMixin.
        _build_toolbar` made for it — and start it hidden, since nothing is open."""
        self._frame_toolbar = bar
        bar.setVisible(False)

        self._frame_align_x = CompactComboBox(_ALIGN_WIDTH)
        self._frame_align_y = CompactComboBox(_ALIGN_WIDTH)
        for combo, rows in (
            (self._frame_align_x, _ALIGN_X),
            (self._frame_align_y, _ALIGN_Y),
        ):
            for label, data in rows:
                combo.addItem(label, data)
        align_tip = (
            "Where the object's origin (0, 0) sits in each frame's box,\n"
            "across and down. Fit: the box is just the pieces' bounds\n"
            "The box always grows to hold every piece"
        )
        self._frame_align_x.setToolTip(align_tip)
        self._frame_align_y.setToolTip(align_tip)
        self._frame_align_x.currentIndexChanged.connect(
            lambda _index: self._on_frame_align_change("x")
        )
        self._frame_align_y.currentIndexChanged.connect(
            lambda _index: self._on_frame_align_change("y")
        )
        # The two read as one control, so they share a tight container rather
        # than the toolbar's own gap - the Block spins' treatment.
        align_pair = QWidget()
        pair = QHBoxLayout(align_pair)
        pair.setContentsMargins(0, 0, 0, 0)
        pair.setSpacing(2)
        pair.addWidget(self._frame_align_x)
        pair.addWidget(self._frame_align_y)
        add_labelled(bar, "Align:", align_pair, align_tip, buddy=self._frame_align_x)

        self._frame_width = value_spin(
            0, FRAME_SIZE_MAX, 0, lambda *_args: self._on_frame_size_change("width")
        )
        self._frame_height = value_spin(
            0, FRAME_SIZE_MAX, 0, lambda *_args: self._on_frame_size_change("height")
        )
        self._frame_width.setFixedWidth(_SIZE_WIDTH)
        self._frame_height.setFixedWidth(_SIZE_WIDTH)
        size_tip = (
            "Each frame's box is at least this many pixels,\n"
            "measured from the aligned side. 0: only what the pieces need\n"
            "The box always grows to hold every piece"
        )
        self._frame_width.setToolTip(size_tip)
        self._frame_height.setToolTip(size_tip)
        times = QLabel("×")
        times.setToolTip(size_tip)
        size_pair = QWidget()
        pair = QHBoxLayout(size_pair)
        pair.setContentsMargins(0, 0, 0, 0)
        pair.setSpacing(2)
        pair.addWidget(self._frame_width)
        pair.addWidget(times)
        pair.addWidget(self._frame_height)
        add_labelled(bar, "Size:", size_pair, size_tip, buddy=self._frame_width)

        self._show_origin_box = QCheckBox("Origin")
        self._show_origin_box.setToolTip(
            "Draw the two axis lines through the object's origin (0, 0)\n"
            "across every frame"
        )
        self._show_origin_box.toggled.connect(
            lambda on: self._push_view_toggle("_show_origin", "show origin", on)
        )
        bar.addWidget(self._show_origin_box)

    # -- sync ------------------------------------------------------------------
    def _sync_frame_toolbar(self) -> None:
        """Show the bar on a sprite map, holding the entry's layout.

        Signal-blocked, because this is a restore: the refresh that brought us
        here has already put the entry's answer on the window, and letting a
        control re-emit would push it again as a step - or, on an entry switch,
        push the outgoing entry's answer onto the incoming one.
        """
        doc = self._doc
        sprite = doc is not None and doc.is_sprite
        self._frame_toolbar.setVisible(sprite)
        if not sprite:
            return
        frame = self._frame
        with signals_blocked(
            self._frame_align_x,
            self._frame_align_y,
            self._frame_width,
            self._frame_height,
            self._show_origin_box,
        ):
            self._frame_align_x.setCurrentIndex(
                max(0, self._frame_align_x.findData(frame.align_x))
            )
            self._frame_align_y.setCurrentIndex(
                max(0, self._frame_align_y.findData(frame.align_y))
            )
            self._frame_width.setValue(frame.width)
            self._frame_height.setValue(frame.height)
            self._show_origin_box.setChecked(self._show_origin)

    # -- gestures --------------------------------------------------------------
    def _on_frame_align_change(self, axis: str) -> None:
        combo = self._frame_align_x if axis == "x" else self._frame_align_y
        self._push_frame(
            f"align {axis}",
            replace(self._frame, **{f"align_{axis}": combo.currentData()}),
        )

    def _on_frame_size_change(self, side: str) -> None:
        spin = self._frame_width if side == "width" else self._frame_height
        self._push_frame(side, replace(self._frame, **{side: spin.value()}))

    def _push_frame(self, control: str, after: FrameLayout) -> None:
        """Push one move of the frame layout as the step named ``control``;
        nothing when the layout already stands there or an undo is landing it."""
        entry = self._workspace.current
        if self._doc is None or entry is None or self._applying_undo:
            return
        if after == self._frame:
            return
        self._push_command(
            FrameCommand(self, entry, control, before=self._frame, after=after)
        )

    def _apply_frame(self, state: FrameLayout) -> None:
        """Land one half of a :class:`FrameCommand` (commands only - gestures
        push): the window's answer moves and the refresh captures it into the
        view, re-cuts the sheet and refills the bar."""
        self._frame = state
        if self._doc is not None:
            self._refresh_view()
