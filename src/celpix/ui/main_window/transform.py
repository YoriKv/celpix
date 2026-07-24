"""The canvas transform toolbar: flip and rotate, per tile or per block.

Geometric transforms are **destructive, undoable edits** — they rewrite the
interpreted pixels and round-trip through the active codec, unlike the byte-nudge
(``−B / +B / 0B``), which only realigns where tiles start and touches no data.
The realignment need the reference tools cover with a 1px *shift* is already the
byte-nudge's job, so only flip/rotate live here.

The bar carries **two groups**, because "flip the selection" means two different
things:

- **Tile** — transform each selected tile **in place**; tile positions never
  change. Works on any selection (linear or rectangle, one tile or many). This is
  the "mirror every tile" operation.
- **Block** — transform the selected **rectangle as one picture**: flip/rotate
  each tile *and* permute the tiles' positions within the block. Needs a 2D block:
  a rectangle selection, or a **single** selected tile — in any selection shape —
  which expands to the arrangement block (Block W×H) it sits in, so one click turns
  a whole metatile. Only a linear *multi*-tile run has no block.

Each button decodes the selection's enclosing run, transforms it, and re-encodes
through :meth:`~celpix.ui.main_window.selection.SelectionMixin._apply_tile_edit`,
which pushes one :class:`~celpix.ui.undo_commands.PixelEditCommand` — so a
transform is a single Ctrl+Z step, exactly like a paste.

Rotation swaps a tile's width and height, so it needs **square tiles** in both
groups; the block group additionally needs a **square block** (``cols == rows``),
since a non-square block would swap the block's own dimensions. Flips have no such
constraint.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QLabel, QSizePolicy, QToolBar, QWidget

from celpix.core import transform
from celpix.ui.main_window.selection import SELECTION_SHAPE_KEY, SelectionShape
from celpix.ui.tools import EditMode
from celpix.ui.widgets import (
    CompactComboBox,
    load_enum_setting,
    select_combo_data,
)


@dataclass(frozen=True)
class _TransformOp:
    """One transform direction: how it moves pixels and, for a block, cells.

    ``pixel_fn`` transforms a single decoded tile (from :mod:`celpix.core.transform`).
    ``cell_src`` answers, for a destination cell ``(dx, dy)`` in a ``cols×rows``
    block, which source cell's (transformed) tile lands there — the block-level
    half of the permutation, unused by the in-place tile group. ``verb``/``past``
    feed the undo label and the status line.
    """

    verb: str
    past: str
    pixel_fn: Callable[[object], object]
    cell_src: Callable[[int, int, int, int], tuple[int, int]]


# The four directions. The cell maps invert the pixel transform at tile
# granularity: a horizontal flip reverses the column axis, a CW rotation
# transposes (block rotation is only ever applied to a square block, cols == rows).
_FLIP_H = _TransformOp(
    "flip",
    "Flipped",
    transform.flip_horizontal,
    lambda dx, dy, cols, rows: (cols - 1 - dx, dy),
)
_FLIP_V = _TransformOp(
    "flip",
    "Flipped",
    transform.flip_vertical,
    lambda dx, dy, cols, rows: (dx, rows - 1 - dy),
)
_ROTATE_CCW = _TransformOp(
    "rotate",
    "Rotated",
    transform.rotate_ccw,
    lambda dx, dy, cols, rows: (cols - 1 - dy, dx),
)
_ROTATE_CW = _TransformOp(
    "rotate",
    "Rotated",
    transform.rotate_cw,
    lambda dx, dy, cols, rows: (dy, rows - 1 - dx),
)


@dataclass
class _TransformGroup:
    """One group of four toolbar actions (flip H/V, rotate CW/CCW)."""

    flip_h: QAction
    flip_v: QAction
    rotate_cw: QAction
    rotate_ccw: QAction

    @property
    def flips(self) -> tuple[QAction, QAction]:
        return (self.flip_h, self.flip_v)

    @property
    def rotates(self) -> tuple[QAction, QAction]:
        return (self.rotate_cw, self.rotate_ccw)


class TransformMixin:
    """Flip/rotate the selection — per tile or per block — from a canvas toolbar.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`; it reads the
    window's live selection state and its single ``_doc``, and reuses the
    selection mixin's decode/encode helpers. See the module docstring for the
    tile-vs-block semantics.
    """

    def _build_transform_toolbar(self) -> QToolBar:
        """The canvas-top transform bar (a plain widget, not ``addToolBar``).

        Placed in the layout above the canvas rather than docked at the window
        top like the Codecs/Arrangement/View bars, so it reads as belonging to
        the editing surface. Two labelled groups — Tile and Block — each start
        disabled; :meth:`_sync_transform_actions` (driven from the selection
        convergence) turns them on for what the selection supports.
        """
        bar = QToolBar("Transform")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._build_selection_shape_combo(bar)
        bar.addSeparator()
        bar.addWidget(QLabel(" Tile: "))
        self._tile_group = self._add_transform_group(
            bar, self._transform_tiles, "each selected tile in place"
        )
        bar.addSeparator()
        bar.addWidget(QLabel(" Block: "))
        self._block_group = self._add_transform_group(
            bar, self._transform_block, "the whole block (tiles and their positions)"
        )
        self._build_edit_mode_toggle(bar)
        return bar

    def _build_edit_mode_toggle(self, bar: QToolBar) -> None:
        """The Tile ⇄ Pixel mode toggle, pinned to the toolbar's right edge.

        An expanding spacer pushes it hard right, away from the transform groups,
        so it reads as a mode switch for the whole editing surface rather than one
        more transform button. Wired to :meth:`_set_edit_mode` (the pixel-edit
        mixin), which converges the canvas, tools dock and selection shape.
        """
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        self._edit_mode_action = QAction("Pixel Mode", self)
        self._edit_mode_action.setCheckable(True)
        self._edit_mode_action.setChecked(self._edit_mode is EditMode.PIXEL)
        self._edit_mode_action.setToolTip(
            "Pixel editing mode — draw individual pixels with the Tools panel; "
            "selection becomes a pixel rectangle. Off is tile editing."
        )
        self._edit_mode_action.toggled.connect(
            lambda on: self._set_edit_mode(EditMode.PIXEL if on else EditMode.TILE)
        )
        bar.addAction(self._edit_mode_action)

    def _build_selection_shape_combo(self, bar: QToolBar) -> None:
        """The canvas-drag Selection Shape picker, hosted on the transform bar.

        An app-wide interaction preference (QSettings), not per-document state: it
        changes how the mouse is read, not how anything renders, so it does not go
        through ``_on_view_change``. It lives here because it feeds the Block group
        — a *multi*-tile block transform needs a Rectangle selection (a single tile
        expands to its arrangement block in any shape) — so the choice sits right
        beside them. Connected after the initial select so seeding the saved value
        doesn't fire the change handler.
        """
        self._selection_shape = CompactComboBox(1.00)
        for shape, label in (
            (SelectionShape.LINEAR, "Linear"),
            (SelectionShape.RECT, "Rectangle"),
        ):
            self._selection_shape.addItem(label, shape)
        self._selection_shape.setToolTip(
            "What dragging on the canvas selects:\n"
            "• Linear - the run of tiles between press and pointer (storage order)\n"
            "• Rectangle - the block of tiles the drag spans on screen"
        )
        select_combo_data(
            self._selection_shape,
            load_enum_setting(SELECTION_SHAPE_KEY, SelectionShape.LINEAR),
        )
        self._selection_shape.currentIndexChanged.connect(
            self._on_selection_shape_change
        )
        bar.addWidget(QLabel("Selection: "))
        bar.addWidget(self._selection_shape)

    def _add_transform_group(
        self, bar: QToolBar, handler: Callable[[_TransformOp], None], scope: str
    ) -> _TransformGroup:
        """Build one flip/rotate group on ``bar``, wired to ``handler``.

        ``scope`` completes each tooltip so the two groups' otherwise-identical
        glyphs read unambiguously.
        """
        # Left-to-right button order; keyed by field so display order and the
        # dataclass mapping stay independent (clockwise rotate comes first).
        specs = (
            ("flip_h", "↔", "Flip horizontal", _FLIP_H),
            ("flip_v", "↕", "Flip vertical", _FLIP_V),
            ("rotate_cw", "↻", "Rotate 90° right", _ROTATE_CW),
            ("rotate_ccw", "↺", "Rotate 90° left", _ROTATE_CCW),
        )
        actions = {}
        for field, glyph, tip, op in specs:
            action = QAction(glyph, self)
            action.setToolTip(f"{tip} — {scope}")
            action.setEnabled(False)
            action.triggered.connect(lambda _=False, op=op: handler(op))
            bar.addAction(action)
            actions[field] = action
        return _TransformGroup(**actions)

    def _sync_transform_actions(self) -> None:
        """Enable each group for what the current selection supports.

        Tile transforms take any selection (rotation needs square tiles); block
        transforms need a 2D block, which :meth:`_block_geometry` resolves (a
        rectangle selection, or a single tile's arrangement block in any shape;
        rotation additionally needs that block square). Called from the selection
        convergence, so the bar tracks every selection change without a separate
        signal.
        """
        if self._edit_mode is EditMode.PIXEL:
            self._sync_pixel_transform_actions()
            return
        has = self._doc is not None and self._selected_tile is not None
        square_tiles = has and self._doc.tile_width == self._doc.tile_height
        for action in self._tile_group.flips:
            action.setEnabled(has)
        for action in self._tile_group.rotates:
            action.setEnabled(square_tiles)

        geom = self._block_geometry() if has else None
        for action in self._block_group.flips:
            action.setEnabled(geom is not None)
        square_block = geom is not None and geom[0] == geom[1]
        for action in self._block_group.rotates:
            action.setEnabled(square_block and square_tiles)

    def _block_geometry(self) -> tuple[int, int, int, int] | None:
        """The block a block-transform acts on: ``(cols, rows, x0, y0)`` in cells.

        A **single** selected tile expands to the arrangement block (Block W×H) it
        sits in, snapped to the ``bc×br`` cell grid the arrangement lays down (see
        :class:`~celpix.core.arrangement.BlockLayout`) — so one click turns a whole
        metatile, in **any** selection shape. A multi-tile Rectangle selection *is*
        the block (its own cell dimensions, anchored at its top-left cell). A linear
        multi-tile run has no 2D block, so it returns ``None``.
        """
        if self._doc is None or self._selected_tile is None:
            return None
        cx, cy = self._view_layout().slot_to_cell(self._selected_tile - self._offset)
        if len(self._selection_tiles()) == 1:
            # Match BlockLayout's block sizing (columns clamps block width).
            bc = max(1, min(self._block_cols.value(), self._columns.value()))
            br = max(1, self._block_rows.value())
            return bc, br, (cx // bc) * bc, (cy // br) * br
        if self._rect_cells is not None:
            cols, rows = self._rect_cells
            return cols, rows, cx, cy
        return None  # a linear multi-tile run has no 2D block

    def _transform_tiles(self, op: _TransformOp) -> None:
        """Transform every selected tile in place — positions unchanged.

        Each selected tile passes through the op's pixel transform;
        :meth:`~celpix.ui.main_window.selection.SelectionMixin._map_selected_tiles`
        handles the run bookkeeping (a rectangle's gap tiles ride along unchanged).
        """
        # In pixel mode the Tile group is repurposed to transform the pixel
        # region (the floating selection, or the whole window).
        if self._edit_mode is EditMode.PIXEL:
            self._transform_pixel_region(op)
            return
        if self._doc is None or self._selected_tile is None:
            return
        moved = len(self._selection_tiles())
        if self._map_selected_tiles(op.pixel_fn, f"{op.verb} tiles"):
            self.statusBar().showMessage(f"{op.past} {self._tiles_label(moved)}.")

    def _transform_block(self, op: _TransformOp) -> None:
        """Transform the block: permute the tiles *and* transform each.

        The block comes from :meth:`_block_geometry` — the whole rectangle, or the
        arrangement block a lone selected tile sits in. For each destination cell
        the block map names the source cell, and the destination tile takes that
        source tile transformed. Cells resolve through the view's arrangement, so a
        blocked view stays correct; the write covers the block's enclosing run,
        with gap/off-run cells skipped. Flip and square rotation map the block's
        cell set onto itself, so every tile stays within the run.
        """
        if self._doc is None:
            return
        geom = self._block_geometry()
        if geom is None:
            return
        cols, rows, x0, y0 = geom
        layout = self._view_layout()
        # Resolve every block cell to its absolute tile; the enclosing run spans them.
        placements = []  # (dest_tile, src_slot)
        for dy in range(rows):
            for dx in range(cols):
                dest_tile = self._cell_tile(layout, x0 + dx, y0 + dy)
                if dest_tile is None:
                    continue
                sx, sy = op.cell_src(dx, dy, cols, rows)
                placements.append((dest_tile, layout.cell_to_slot(x0 + sx, y0 + sy)))
        if not placements:
            return
        first, last = min(t for t, _ in placements), max(t for t, _ in placements)

        def mutate(decoded: list) -> None:
            # Snapshot before mutating: the block reads source tiles while writing
            # destinations, and the two overlap.
            original = list(decoded)
            for dest_tile, src_slot in placements:
                if src_slot is None:
                    continue
                didx = dest_tile - first
                sidx = self._offset + src_slot - first
                if 0 <= didx < len(decoded) and 0 <= sidx < len(original):
                    decoded[didx] = op.pixel_fn(original[sidx])

        if self._edit_run(first, last - first + 1, mutate, f"{op.verb} block"):
            self.statusBar().showMessage(f"{op.past} the {cols}×{rows} block.")

    # -- pixel mode --------------------------------------------------------
    def _sync_pixel_transform_actions(self) -> None:
        """Enable the Tile group for a pixel-region transform; hide the Block one.

        In pixel mode the Tile group flips/rotates the floating selection (or the
        whole visible window when nothing is lifted); a rectangle needs to be
        square to rotate, matching the tile-mode rule. The Block group is hidden.
        """
        region = self._pixel_transform_source()
        has = self._doc is not None and region is not None
        square = has and region.width() == region.height()
        for action in self._tile_group.flips:
            action.setEnabled(has)
        for action in self._tile_group.rotates:
            action.setEnabled(square)
        for action in (*self._block_group.flips, *self._block_group.rotates):
            action.setEnabled(False)
