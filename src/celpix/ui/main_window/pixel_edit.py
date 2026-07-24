"""Pixel-mode editing: the drawing tools, the pen, and the tools rail.

This mixin is the brain of pixel mode. It owns the edit-mode switch, the active
tool, the pen color, and the machinery that turns a canvas gesture into an
undoable edit — plus the floating pixel selection and pixel clipboard
(:mod:`~celpix.ui.main_window.pixel_edit` continues into the selection/transform
dispatch those files carry). It is a slice of
:class:`~celpix.ui.main_window.window.MainWindow`, so it reaches the window's
live ``_doc`` and reuses :class:`~celpix.ui.main_window.selection.SelectionMixin`'s
decode/encode helpers through ``self`` rather than re-deriving them.

**The round-trip.** Every pixel edit rides the same path (see the module for the
compositor's inverse, :func:`~celpix.core.arrangement.split_grid`)::

    decode_run(offset, cols*rows)          # storage-order tiles, 2D reflow absorbed
      → compose_window(...)                # the exact image on screen
        → a core.draw tool mutates the grid
          → split_grid(...)                # back to storage-order tiles
            → _apply_tile_edit(...)        # one undoable byte splice

Only the tiles that actually changed are re-encoded (the min..max touched slot),
so untouched tiles are never round-tripped and the undo step is the edit, not the
whole window.

**The float.** A selection whose pixels have been picked up — by a move or a
paste — is *in the air*: it hides what is under it and shows its source blank,
but the document is untouched for as long as it hovers. It comes down when the
selection is cleared or replaced (and on the paths that can't keep it: a drawing
gesture, leaving pixel mode, switching entry, a write), and only then does one
edit blank the source and overwrite the destination. Everything else about a
float is display state, which is why undo can carry one (:class:`FloatState`):
dropping it back out of the air reveals the pixels exactly where they still are.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSettings, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from celpix.core import draw
from celpix.core.arrangement import compose_window, split_grid
from celpix.pipeline import importer
from celpix.ui import clipboard, render_bridge
from celpix.ui.main_window.selection import SELECTION_SHAPE_KEY, SelectionShape
from celpix.ui.tools import TOOL_BY_KEY, TOOL_SPEC, EditMode, Gesture, Tool
from celpix.ui.tools_panel import ToolsPanel
from celpix.ui.undo_commands import FloatState, PixelSelectionCommand
from celpix.ui.widgets import (
    load_enum_setting,
    select_combo_data,
    signals_blocked,
)

# QSettings key for the active tool (an interaction preference, like the
# selection shape). The edit *mode* deliberately starts in Tile each launch — a
# safe default that one toggle click leaves.
TOOL_KEY = "view/tool"


class PixelEditMixin:
    """The drawing tools and pixel-mode state for the main window.

    See the module docstring for the round-trip every edit rides and for how this
    mixin relates to the selection/transform ones.
    """

    # -- state & construction ---------------------------------------------
    def _init_pixel_edit(self) -> None:
        """Seed pixel-mode state; called from the window's ``__init__`` before the
        transform toolbar (which reads ``_edit_mode`` to set the toggle) is built."""
        self._edit_mode = EditMode.TILE
        self._tool = load_enum_setting(TOOL_KEY, Tool.PENCIL)
        # A direct-color eyedropper stores its picked ARGB here (indexed views
        # drive the pen off the palette selection instead); cleared when the user
        # picks a palette swatch, so a click always takes the pen back.
        self._pen_argb: int | None = None
        # Live-stroke scratch: the clean composed window at press time, the
        # working copy the pen paints into, and the last committed preview grid.
        self._stroke_active = False
        self._stroke_base_grid = None
        self._stroke_grid = None
        self._stroke_anchor = (0, 0)
        self._stroke_last = (0, 0)
        # Pixel selection & floating selection. The marquee is a pixel rectangle;
        # a float is a lifted grid hovering at ``_float_pos``, and while one is in
        # the air it *is* the selection, so the marquee tracks it.
        # ``_float_source_rect`` is the hole a *move* owes — shown blank while the
        # pixels are up, written only when they land — and None for a paste, which
        # removes nothing. ``_float_offset`` is the grab point within the float
        # while dragging.
        self._marquee: QRect | None = None
        self._marquee_anchor = (0, 0)
        # The selection as it stood when the current gesture began, so the step it
        # pushes can put it back (see _marquee_press / _marquee_release).
        self._marquee_before: QRect | None = None
        # Whether the press in progress is what lifted the live float (a
        # double-click discards such a float, but sets down one it merely grabbed).
        self._lifted_on_press = False
        self._float_grid = None
        self._float_pos = (0, 0)
        self._float_source_rect: QRect | None = None
        self._float_offset = (0, 0)

    def _build_tools_bar(self) -> ToolsPanel:
        """The drawing-tools rail down the canvas's right edge.

        A plain widget dropped into the canvas layout beside the transform toolbar
        (not a dock), so both read as toolbars belonging to the editing surface.
        Returned for the window to place; disabled until pixel mode with a document
        is active — the tools have nothing to act on otherwise.

        Tile mode is itself a selection mode, so the rail opens showing Select
        rather than the saved pixel tool (the window always starts in tile mode).
        """
        self._tools_panel = ToolsPanel()
        if self._edit_mode is EditMode.TILE:
            self._tool = Tool.SELECT
        self._tools_panel.set_tool(self._tool)
        self._tools_panel.tool_selected.connect(self._on_tool_selected)
        self._tools_panel.setEnabled(False)
        return self._tools_panel

    def _connect_pixel_palette(self) -> None:
        """Picking a palette swatch takes the pen back from a direct-color eyedrop.

        Wired once the palette dock exists (the rail is built earlier, with the
        canvas layout, so this connection can't ride along in its builder)."""
        self._palette_panel.color_selected.connect(self._on_pen_color_selected)

    def _connect_pixel_canvas(self) -> None:
        """Wire the canvas's pixel gestures to the tool handlers (called once the
        canvas exists)."""
        self._canvas.pixel_pressed.connect(self._on_pixel_pressed)
        self._canvas.pixel_moved.connect(self._on_pixel_moved)
        self._canvas.pixel_released.connect(self._on_pixel_released)
        self._canvas.pixel_double_clicked.connect(self._on_pixel_double_clicked)

    # -- mode switching ----------------------------------------------------
    def _set_edit_mode(self, mode: EditMode) -> None:
        """Switch tile ⇄ pixel editing and converge every surface with it.

        Pixel mode is rectangle-only, so the Selection Shape picker is forced to
        Rectangle and disabled (their saved preference is preserved, restored on
        the way back); the canvas switches to painting; the tools rail arms; the
        Block transform group hides. Any tile selection is dropped so its
        highlight doesn't linger over the paint surface — and the status line
        with it, which otherwise still announces a selection that is gone. Pixels
        still in the air are set down: the tile surface has nowhere to keep them.
        """
        if mode == self._edit_mode:
            return
        self._commit_float()
        self._edit_mode = mode
        pixel = mode is EditMode.PIXEL
        with signals_blocked(self._edit_mode_action):
            self._edit_mode_action.setChecked(pixel)
        self._canvas.set_edit_mode(mode)
        # Force Rectangle in pixel mode without clobbering the saved shape.
        with signals_blocked(self._selection_shape):
            shape = (
                SelectionShape.RECT
                if pixel
                else load_enum_setting(SELECTION_SHAPE_KEY, SelectionShape.LINEAR)
            )
            select_combo_data(self._selection_shape, shape)
        self._selection_shape.setEnabled(not pixel)
        self._clear_stroke()
        self._clear_float()
        self._clear_selection()
        self.statusBar().clearMessage()
        if not pixel:
            # Tile mode selects rather than paints, so swap to Select: the rail
            # keeps that button checked (highlighted as the active tool) even while
            # the whole rail is disabled, so it still reads as what a drag does.
            self._on_tool_selected(Tool.SELECT)
        self._sync_mode_ui()
        if self._doc is not None:
            self._refresh_view()

    def _sync_mode_ui(self) -> None:
        """Show/hide the mode-dependent transform-bar groups and the tools rail."""
        pixel = self._edit_mode is EditMode.PIXEL
        # Swap the visible transform groups: Tile + Block for tile editing, the
        # dedicated Pixel group for pixel editing (see transform.py).
        self._sync_transform_bar_mode()
        self._tools_panel.setEnabled(pixel and self._doc is not None)
        self._sync_paint_preview()
        self._sync_transform_actions()

    def _on_tool_selected(self, tool: Tool) -> None:
        # Picking a tool leaves a live float alone: a selection is dropped by
        # clearing it or by making a new one, not by reaching for another tool.
        self._tool = tool
        QSettings().setValue(TOOL_KEY, tool.value)
        self._tools_panel.set_tool(tool)
        self._sync_paint_preview()  # a different tool may not paint at all

    # -- pen ---------------------------------------------------------------
    def _on_pen_color_selected(self, _index: int) -> None:
        """A palette-swatch pick takes the pen back from a direct-color eyedrop."""
        self._pen_argb = None
        self._sync_paint_preview()

    def _pen_value(self) -> int:
        """The value the pen writes into the working grid.

        Indexed views store an index *within the active subpalette* (0..space-1),
        so the pen is the selected swatch minus the subpalette base; direct-color
        views store an ARGB — a picked one, else the selected swatch's color.
        """
        assert self._doc is not None
        selected = self._palette_panel.selected_index()
        if self._is_direct_color():
            if self._pen_argb is not None:
                return self._pen_argb
            return self._doc.palette.color(selected if selected is not None else 0)
        space = self._index_space()
        base = self._subpalette.value() * space
        if selected is None:
            return 0
        return max(0, min(selected - base, space - 1))

    def _sync_paint_preview(self) -> None:
        """Arm the canvas's one-pixel pen preview for the active drawing tool.

        Only the tools that lay down paint get it — the eyedropper samples and the
        marquee selects, so neither previews a colour. Called wherever the pen, the
        tool, the palette or the mode can have moved under it.
        """
        drawing = {Gesture.FREEHAND, Gesture.SHAPE, Gesture.FILL}
        if (
            self._doc is None
            or self._edit_mode is not EditMode.PIXEL
            or TOOL_SPEC[self._tool].gesture not in drawing
        ):
            self._canvas.set_paint_preview(None)
            return
        self._canvas.set_paint_preview(QColor.fromRgba(self._pen_color_argb()))

    def _pen_color_argb(self) -> int:
        """The ARGB the pen would write — what the preview shows.

        Direct-colour views carry the ARGB in the pen value itself; indexed views
        carry an index *within* the active subpalette, so it is rebased to an
        absolute palette index before the lookup.
        """
        assert self._doc is not None
        value = self._pen_value()
        if self._is_direct_color():
            return value
        base = self._subpalette.value() * self._index_space()
        return self._doc.palette.color(base + value)

    # -- the window-grid round-trip ---------------------------------------
    def _window_grid(self):
        """The composed image of the visible window — what the canvas shows.

        ``None`` when the run won't decode. The same compose the copy path uses,
        so a pixel edit works on exactly the pixels on screen.
        """
        assert self._doc is not None
        cols, rows = self._columns.value(), self._rows.value()
        tiles = self._decode_run(self._offset, cols * rows)
        if not tiles:
            return None
        return compose_window(tiles, cols, 0, rows, self._view_layout())

    @staticmethod
    def _clone_grid(grid):
        return type(grid)(grid.width, grid.height, bytes(grid.data))

    def _paint_pixels(self, grid, pixels, value: int | None = None) -> None:
        """Set ``pixels`` on ``grid`` to the pen (or ``value``), clipped to bounds.

        **A selection is a mask**: while a marquee is up, every tool paints only
        inside it, so a stroke can be run right across an edge without touching
        what is beyond it. The single choke point every tool's paint goes through,
        so none of them can be clipped and the rest not.
        """
        if value is None:
            value = self._pen_value()
        w, h = grid.width, grid.height
        mask = self._marquee
        for px, py in pixels:
            if 0 <= px < w and 0 <= py < h and (mask is None or mask.contains(px, py)):
                grid.set(px, py, value)

    def _render_preview(self, grid) -> None:
        """Show a working grid on the canvas without committing it (live preview)."""
        assert self._doc is not None
        base = self._subpalette.value() * self._index_space()
        self._canvas.set_image(render_bridge.render(grid, self._doc.palette, base))

    def _commit_grid(
        self, grid, base_grid, text: str, *, no_op_step: bool = True
    ) -> int:
        """Write only the tiles that differ between ``base_grid`` and ``grid``.

        Both are split back into storage-order tiles; the changed slots form a
        contiguous span (``min..max``) that goes out as one ``_apply_tile_edit``,
        so untouched tiles are never re-encoded. Always ends with a real
        ``_refresh_view`` — that repaints the committed result, and cleanly
        reverts a transient preview when nothing changed.

        ``no_op_step`` records a gesture that changed nothing as an empty step
        anyway, so a *painting* interaction always costs one step. Selection
        gestures pass ``False``: dropping a float back where it started is not an
        edit, and shouldn't litter the history.
        """
        assert self._doc is not None
        tw, th = self._doc.tile_width, self._doc.tile_height
        layout = self._view_layout()
        base_tiles = split_grid(base_grid, tw, th, layout)
        new_tiles = split_grid(grid, tw, th, layout)
        changed = [i for i in range(len(base_tiles)) if base_tiles[i] != new_tiles[i]]
        written = 0
        if changed:
            lo, hi = changed[0], changed[-1]
            written = self._apply_tile_edit(
                self._offset + lo, new_tiles[lo : hi + 1], text
            )
        elif no_op_step:
            # The gesture happened but moved no pixels — still one interaction, so
            # it takes a step of its own rather than vanishing from the history.
            self._push_pixel_interaction(self._marquee, self._marquee, text)
        self._refresh_view()
        return written

    # -- gesture dispatch --------------------------------------------------
    def _on_pixel_pressed(self, x: int, y: int, button) -> None:
        if self._doc is None:
            return
        spec = TOOL_SPEC[self._tool]
        # Right-click is the eyedropper on every tool (the YY-CHR idiom).
        if button == Qt.MouseButton.RightButton or spec.gesture is Gesture.SAMPLE:
            self._eyedrop_at(x, y)
            return
        if button != Qt.MouseButton.LeftButton:
            return
        if spec.gesture is not Gesture.MARQUEE:
            # Painting under a live float would paint *beneath* pixels that are
            # still in the air, against a base captured before the stroke — so a
            # paint/fill gesture lands the float first, keeping the selection it
            # is about to paint through.
            self._commit_float(keep_selection=True)
        if spec.gesture is Gesture.FILL:
            self._fill_at(x, y)
        elif spec.gesture is Gesture.MARQUEE:
            self._marquee_press(x, y)
        else:  # FREEHAND / SHAPE
            self._begin_stroke(x, y)

    def _on_pixel_moved(self, x: int, y: int) -> None:
        if self._stroke_active:
            self._extend_stroke(x, y)
        elif self._marquee is not None or self._float_grid is not None:
            self._marquee_drag(x, y)

    def _on_pixel_released(self, x: int, y: int) -> None:
        if self._stroke_active:
            self._end_stroke(x, y)
        elif self._marquee is not None or self._float_grid is not None:
            self._marquee_release(x, y)

    # -- strokes (pencil / line / rect / ellipse) -------------------------
    def _begin_stroke(self, x: int, y: int) -> None:
        grid = self._window_grid()
        if grid is None:
            return
        self._stroke_base_grid = grid
        self._stroke_grid = self._clone_grid(grid)
        self._stroke_anchor = (x, y)
        self._stroke_last = (x, y)
        self._stroke_active = True
        self._paint_stroke(x, y)

    def _extend_stroke(self, x: int, y: int) -> None:
        self._paint_stroke(x, y)

    def _paint_stroke(self, x: int, y: int) -> None:
        """Redraw the working grid for the current pointer and preview it.

        Freehand paints along the segment from the last sample (so a fast stroke
        has no gaps); a shape tool recomputes from the anchor over a clean copy of
        the base, so dragging replaces the rubber-banded shape rather than piling
        shapes up.
        """
        spec = TOOL_SPEC[self._tool]
        assert spec.rasterize is not None
        if spec.gesture is Gesture.FREEHAND:
            lx, ly = self._stroke_last
            self._paint_pixels(self._stroke_grid, spec.rasterize(lx, ly, x, y))
        else:
            self._stroke_grid = self._clone_grid(self._stroke_base_grid)
            ax, ay = self._stroke_anchor
            self._paint_pixels(self._stroke_grid, spec.rasterize(ax, ay, x, y))
        self._stroke_last = (x, y)
        self._render_preview(self._stroke_grid)

    def _end_stroke(self, x: int, y: int) -> None:
        self._paint_stroke(x, y)
        grid, base = self._stroke_grid, self._stroke_base_grid
        text = f"draw {TOOL_SPEC[self._tool].label.lower()}"
        self._stroke_active = False
        self._stroke_grid = self._stroke_base_grid = None
        self._commit_grid(grid, base, text)

    def _clear_stroke(self) -> None:
        self._stroke_active = False
        self._stroke_grid = self._stroke_base_grid = None

    # -- fill & eyedropper -------------------------------------------------
    def _fill_at(self, x: int, y: int) -> None:
        """Flood the region under ``(x, y)``, confined to the selection if any."""
        base = self._window_grid()
        if base is None:
            return
        grid = self._clone_grid(base)
        mask = self._marquee
        bounds = None
        if mask is not None:
            bounds = (mask.x(), mask.y(), mask.right(), mask.bottom())
        self._paint_pixels(grid, draw.flood_fill(grid, x, y, bounds))
        if self._commit_grid(grid, base, "fill"):
            self.statusBar().showMessage("Filled a region.")

    def _eyedrop_at(self, x: int, y: int) -> None:
        grid = self._window_grid()
        if grid is None or not (0 <= x < grid.width and 0 <= y < grid.height):
            return
        value = grid.get(x, y)
        if self._is_direct_color():
            self._pen_argb = value
            self.statusBar().showMessage(f"Picked color #{value & 0xFFFFFFFF:08X}.")
        else:
            base = self._subpalette.value() * self._index_space()
            self._palette_panel.select_index(base + value)
            self.statusBar().showMessage(f"Picked color index {value}.")
        # The direct-colour branch sets the pen behind the palette panel's back,
        # so the preview is re-armed here rather than off its selection signal.
        self._sync_paint_preview()

    # -- marquee & floating selection --------------------------------------
    def _marquee_press(self, x: int, y: int) -> None:
        """Press with the Select tool: take hold of the selection, else start one.

        Pressing inside the selection picks its pixels up — lifting them into a
        floating selection the first time, then grabbing that same float on every
        later press — so a selection can be nudged as often as the user likes
        before it comes down. Pressing anywhere else **lands** a live float over
        whatever it hovers on and anchors a fresh rectangle the drag grows.
        """
        # The selection as it stood before this gesture — what an undo of the
        # resulting step puts back (see _marquee_release).
        self._marquee_before = None if self._marquee is None else QRect(self._marquee)
        if self._marquee is not None and self._marquee.contains(x, y):
            self._lifted_on_press = self._float_grid is None
            if self._lifted_on_press:
                self._lift_float(cut=True)
            if self._float_grid is not None:
                fx, fy = self._float_pos
                self._float_offset = (x - fx, y - fy)
            return
        self._lifted_on_press = False
        self._commit_float()  # pressing away from the pixels sets them down
        self._marquee_anchor = (x, y)
        self._marquee = QRect(x, y, 1, 1)
        self._canvas.set_marquee(self._marquee)

    def _on_pixel_double_clicked(self, x: int, y: int) -> None:
        """Double-click with Select: take the whole tile the pixel sits in.

        The press that opened the double-click already began a gesture — a fresh
        1x1 marquee, or a grab on the selection's pixels. A float that press
        *lifted* is discarded, since the double-click supersedes it and a lift
        has written nothing; one that was already in the air is a selection of
        its own, so it comes down where it hovers first. ``_marquee_before``
        still holds the selection as it stood before that press, so the
        re-selection lands as the one undo step a single interaction should.
        """
        if self._doc is None or TOOL_SPEC[self._tool].gesture is not Gesture.MARQUEE:
            return
        if self._lifted_on_press:
            self._clear_float()  # also drops the press's marquee
            self._refresh_view()  # repaint over the blanked-source preview
        else:
            self._commit_float()
        grid = self._window_grid()
        if grid is None:
            return
        tile_w, tile_h = self._doc.tile_width, self._doc.tile_height
        # Tiles are composed into the window on tile-sized cells whatever the
        # arrangement, so the cell is a plain floor-divide — and clipping keeps a
        # partial tile at the window's edge inside the image.
        rect = QRect(
            (x // tile_w) * tile_w, (y // tile_h) * tile_h, tile_w, tile_h
        ).intersected(QRect(0, 0, grid.width, grid.height))
        if rect.isEmpty():
            return
        before = self._marquee_before
        self._marquee = rect
        self._marquee_anchor = (rect.x(), rect.y())
        self._canvas.set_marquee(rect)
        if before != rect:
            self._push_pixel_interaction(before, rect, "select tile")
        self._after_pixel_change()
        self.statusBar().showMessage(f"Selected the {tile_w}×{tile_h} tile.")

    def _marquee_drag(self, x: int, y: int) -> None:
        if self._float_grid is not None:
            ox, oy = self._float_offset
            self._float_pos = (x - ox, y - oy)
            self._sync_float_marquee()
            self._show_float()
            return
        if self._marquee is None:
            return
        ax, ay = self._marquee_anchor
        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
            x, y = self._square_corner(ax, ay, x, y)
        self._marquee = QRect(min(ax, x), min(ay, y), abs(x - ax) + 1, abs(y - ay) + 1)
        self._canvas.set_marquee(self._marquee)

    def _square_corner(self, ax: int, ay: int, x: int, y: int) -> tuple[int, int]:
        """Snap the drag corner ``(x, y)`` so the marquee is square (Shift held).

        The side is the larger of the two drag extents, kept in the drag's
        direction from the anchor, then clamped so the square can't run off the
        window on either axis — a square that wouldn't fit shrinks to what does.
        """
        dx, dy = x - ax, y - ay
        side = max(abs(dx), abs(dy))
        if self._doc is not None:
            win_w = self._columns.value() * self._doc.tile_width
            win_h = self._rows.value() * self._doc.tile_height
            room_x = (win_w - 1 - ax) if dx >= 0 else ax
            room_y = (win_h - 1 - ay) if dy >= 0 else ay
            side = min(side, room_x, room_y)
        return ax + (side if dx >= 0 else -side), ay + (side if dy >= 0 else -side)

    def _marquee_release(self, x: int, y: int) -> None:
        """End a marquee gesture: park a moved float, or treat a bare click as
        deselect.

        A **move** sets nothing down here (see :meth:`_land_float`): the pixels
        stay in the air with the selection on them, so they can be dragged on and
        on — each drag its own undo step — until the selection is cleared or
        replaced. Releasing without having dragged leaves everything as it was.

        A bare *click* elsewhere — press and release on the same pixel, so the
        anchor rect never grew past 1×1 — deselects instead. A one-pixel selection
        is of no use, and clicking off a selection to drop it is what the gesture
        reads as.
        """
        if self._float_grid is not None:
            self._land_float()
            self._after_pixel_change()
            return
        before = self._marquee_before
        if (
            self._marquee is not None
            and self._marquee.width() == 1
            and self._marquee.height() == 1
        ):
            self._clear_float()  # drops the marquee and its canvas overlay
        # Making, replacing or dropping a selection is an interaction of its own —
        # but a click that left the selection exactly as it was is not.
        if before != self._marquee:
            self._push_pixel_interaction(before, self._marquee, "select pixels")
        self._after_pixel_change()

    def _land_float(self) -> None:
        """Park a dragged float: the pixels stay in the air, the selection on them.

        Releasing the mouse writes **nothing**. A move's source is *shown* blank
        from the first drag onward, so the pixels read as picked up rather than
        copied — but that hole is only owed, not written, and it stays at the
        rectangle they were first lifted from however far they travel afterwards.
        Both halves are settled at once when the pixels come down (see
        :meth:`_commit_float`), which is what dropping or replacing the selection
        does.

        The drag is still one interaction, so the selection travelling with the
        pixels takes one undo step — and because nothing has been written, undoing
        it simply takes the float back out of the air, revealing the pixels
        untouched where they came from.
        """
        assert self._float_grid is not None
        before, after = self._marquee_before, self._float_rect()
        self._sync_float_marquee()
        if before == after:
            return
        state = FloatState(self._float_grid, self._float_source_rect)
        self._push_pixel_interaction(
            before,
            after,
            "move selection",
            # The press either lifted this float (so before it there was only a
            # marquee) or grabbed one already up, which undo has to put back.
            before_float=None if self._lifted_on_press else state,
            after_float=state,
        )

    def _lift_float(self, cut: bool) -> None:
        """Lift the marquee's pixels into a floating selection.

        Nothing is written here, on the drop, or anywhere in between — the whole
        move becomes one undoable edit when the pixels land. When ``cut`` the
        source is *shown* blank for as long as they are in the air (and blanked
        for real when they come down); a non-cut lift leaves it alone.
        """
        if self._doc is None or self._marquee is None:
            return
        base = self._window_grid()
        if base is None:
            return
        rect = self._marquee
        self._float_grid = draw.extract_region(
            base, rect.x(), rect.y(), rect.width(), rect.height()
        )
        self._float_pos = (rect.x(), rect.y())
        # Copied: the hole outlives the marquee it was lifted from, and rides the
        # undo stack in a FloatState.
        self._float_source_rect = QRect(rect) if cut else None
        self._sync_float_marquee()
        self._refresh_float_preview(base)
        self._show_float()
        self._after_pixel_change()

    def _commit_float(self, *, keep_selection: bool = False) -> None:
        """Set a floating selection down: its pixels overwrite what they hover on.

        Composites the float onto the window **as it stands now** — blanking a
        move's source for real on the way, so the whole move is one undoable edit
        — and drops the selection with it. A no-op when no float is live, so every
        "the selection is going away" path (Esc, a click elsewhere, a drawing
        gesture, leaving pixel mode, a write) can call it freely.

        ``keep_selection`` leaves the marquee on the pixels where they landed, for
        the caller that isn't deselecting at all: a drawing gesture has to set the
        float down before it can paint, but the selection it paints through must
        survive that (see :meth:`_paint_pixels`).
        """
        if self._float_grid is None:
            return
        float_grid = self._float_grid
        fx, fy = self._float_pos
        source = self._float_source_rect
        moved = source is not None
        landed = self._float_rect()
        base = self._window_grid()
        self._clear_float()
        if keep_selection:
            self._marquee = landed
            if hasattr(self, "_canvas"):
                self._canvas.set_marquee(landed)
        if base is None:
            return
        dest = self._clone_grid(base)
        if source is not None:
            self._blank_rect(dest, source)
        draw.blit_region(dest, float_grid, fx, fy)
        # Landing a float is a selection gesture, not a paint: if it never left
        # home there is nothing to record, so no empty step.
        self._commit_grid(
            dest,
            base,
            "move pixels" if moved else "paste pixels",
            no_op_step=False,
        )

    def _discard_float(self, text: str) -> None:
        """Take the floating pixels out of the air without setting them down.

        What a move already lifted stays gone — the hole it owed is written here —
        so cutting or clearing a float means "these pixels are deleted", not "put
        them back". A paste owes no hole, so discarding one writes nothing.
        """
        source = self._float_source_rect
        base = self._window_grid()
        self._clear_float()
        if source is None or base is None:
            if self._doc is not None:
                self._refresh_view()  # take the float's overlay off the canvas
            return
        dest = self._clone_grid(base)
        self._blank_rect(dest, source)
        self._commit_grid(dest, base, text, no_op_step=False)

    def _float_rect(self) -> QRect | None:
        """Where the floating pixels are, or None with nothing in the air."""
        if self._float_grid is None:
            return None
        fx, fy = self._float_pos
        return QRect(fx, fy, self._float_grid.width, self._float_grid.height)

    def _sync_float_marquee(self) -> None:
        """Keep the selection on the floating pixels.

        A float *is* the selection while it is up — the clipboard, the transforms
        and the next press all read it through ``_marquee`` — so the rectangle
        travels with the pixels instead of staying where they were lifted from.
        The canvas draws the float's own outline, so it needs no marquee over it.
        """
        self._marquee = self._float_rect()
        if hasattr(self, "_canvas"):
            self._canvas.set_marquee(None)

    def _refresh_float_preview(self, base=None) -> None:  # noqa: ANN001 — a grid
        """Show the hole a lifted float owes, over the base image as it stands.

        A move's source is only *shown* blank while the pixels are in the air, so
        every repaint of the base — a scroll, a palette edit, an undo elsewhere —
        has to punch it back in. A no-op when nothing is up or the float is a
        paste, which removed nothing.
        """
        if self._float_source_rect is None or self._doc is None:
            return
        if base is None:
            base = self._window_grid()
            if base is None:
                return
        preview = self._clone_grid(base)
        self._blank_rect(preview, self._float_source_rect)
        self._render_preview(preview)

    def _clear_selection_on_background(self) -> None:
        """A click on the canvas surround (off the art) drops the pixel selection.

        Clicking past the edge of the image is the same gesture as clicking away
        from a selection inside it, so it reads as a deselect. A live float comes
        down first — "click away to drop" — and a click with nothing selected
        records nothing.
        """
        if self._edit_mode is not EditMode.PIXEL or self._doc is None:
            return
        if self._float_grid is not None:
            self._commit_float()
            self._after_pixel_change()
            return
        before = self._marquee
        if before is None:
            return
        self._clear_float()  # drops the marquee and its canvas overlay
        self._push_pixel_interaction(before, None, "clear selection")
        self._after_pixel_change()

    def _apply_marquee(
        self, rect: QRect | None, float_state: FloatState | None = None
    ) -> None:
        """Restore a pixel selection from the undo stack — no commit either way.

        A selection that was **in the air** comes back floating, with the hole its
        move still owed: a float writes nothing until it lands, so restoring one is
        pure display state and the pixels it shows are still in the document where
        they were lifted from. Either end of a float transition repaints the base,
        which is what puts that blanked source on screen or takes it off again.
        """
        had_float = self._float_grid is not None
        self._clear_float()
        self._marquee = None if rect is None else QRect(rect)
        if float_state is not None and rect is not None:
            self._float_grid = float_state.grid
            self._float_pos = (rect.x(), rect.y())
            self._float_source_rect = float_state.source
        if hasattr(self, "_canvas"):
            self._canvas.set_marquee(
                None if self._float_grid is not None else self._marquee
            )
        if (had_float or self._float_grid is not None) and self._doc is not None:
            self._refresh_view()  # re-renders the base, hole preview included
        self._show_float()
        self._after_pixel_change()

    def _push_pixel_interaction(
        self,
        before: QRect | None,
        after: QRect | None,
        text: str,
        *,
        before_float: FloatState | None = None,
        after_float: FloatState | None = None,
    ) -> None:
        """Record a pixel interaction that rewrote no bytes as its own undo step.

        Covers a selection made/replaced/moved/dropped and a painting gesture that
        changed nothing, so every interaction costs one step either way. The float
        states say which ends of the transition had pixels in the air.
        """
        entry = self._workspace.current
        if entry is None or self._applying_undo:
            return
        self._undo_stack.push(
            PixelSelectionCommand(
                self,
                entry,
                text,
                before=before,
                after=after,
                before_float=before_float,
                after_float=after_float,
            )
        )

    def _drop_float(self) -> None:
        """Take the floating pixels off the canvas without writing them.

        The marquee is left alone: a float that is dropped rather than landed has
        changed nothing, so what it hovers over — and what it was lifted from —
        are both still exactly as the document has them.
        """
        self._float_grid = None
        self._float_source_rect = None
        if hasattr(self, "_canvas"):
            self._canvas.set_float(None)

    def _clear_float(self) -> None:
        """Drop the float and the selection with it (no commit)."""
        self._drop_float()
        self._marquee = None
        if hasattr(self, "_canvas"):
            self._canvas.set_marquee(None)

    def _blank_rect(self, grid, rect: QRect) -> None:
        """Set every pixel of ``rect`` in ``grid`` to the empty value (index 0 /
        transparent black), clipped to the grid — what Cut/Clear leave behind."""
        for py in range(rect.y(), rect.y() + rect.height()):
            for px in range(rect.x(), rect.x() + rect.width()):
                if 0 <= px < grid.width and 0 <= py < grid.height:
                    grid.set(px, py, 0)

    def _after_pixel_change(self) -> None:
        """Reconverge the actions gated on a pixel selection (clipboard/transform)."""
        self._sync_edit_actions()
        self._sync_transform_actions()

    # -- pixel clipboard (Cut / Copy / Paste / Clear / Select All) ---------
    def _selected_region(self):
        """The selected pixels themselves: the float when one is in the air, else
        what the marquee covers in the window."""
        if self._float_grid is not None:
            return self._float_grid
        rect, base = self._marquee, self._window_grid()
        if rect is None or base is None:
            return None
        return draw.extract_region(
            base, rect.x(), rect.y(), rect.width(), rect.height()
        )

    def _pixel_copy(self) -> None:
        if self._doc is None or self._marquee is None:
            self.statusBar().showMessage("Select a pixel rectangle to copy.")
            return
        region = self._selected_region()
        if region is None:
            return
        self._put_pixel_clipboard(region)
        self.statusBar().showMessage(f"Copied {region.width}×{region.height} pixels.")

    def _pixel_cut(self) -> None:
        rect = self._marquee
        if self._doc is None or rect is None:
            return
        region = self._selected_region()
        if region is None:
            return
        self._put_pixel_clipboard(region)
        size = f"{region.width}×{region.height}"
        if self._float_grid is not None:
            # Already off the page: cutting is just never setting them down.
            self._discard_float("cut pixels")
            self.statusBar().showMessage(f"Cut {size} pixels.")
            return
        base = self._window_grid()
        if base is None:
            return
        grid = self._clone_grid(base)
        self._blank_rect(grid, rect)
        self._clear_float()
        if self._commit_grid(grid, base, "cut pixels"):
            self.statusBar().showMessage(f"Cut {size} pixels.")

    def _pixel_clear(self) -> None:
        rect = self._marquee
        if self._doc is None or rect is None:
            return
        size = f"{rect.width()}×{rect.height()}"
        if self._float_grid is not None:
            self._discard_float("clear pixels")
            self.statusBar().showMessage(f"Cleared {size} pixels.")
            return
        base = self._window_grid()
        if base is None:
            return
        grid = self._clone_grid(base)
        self._blank_rect(grid, rect)
        self._clear_float()
        if self._commit_grid(grid, base, "clear pixels"):
            self.statusBar().showMessage(f"Cleared {size} pixels.")

    def _pixel_paste(self) -> None:
        """Drop the clipboard in as a floating selection, centred on the view.

        A paste arrives **in the air**: it hides what is under it but writes
        nothing, so it can be dragged into place — and thrown away again — before
        it lands, which is what clearing or replacing the selection does. It comes
        in centred on what is on screen rather than at the origin, so it arrives
        where the user is looking however far the view is scrolled — and takes the
        Select tool with it, since a paste that can't be dragged without first
        reaching for a tool may as well have landed where it fell.
        """
        if self._doc is None:
            return
        region = self._take_pixel_clipboard()
        if region is None:
            self.statusBar().showMessage("Nothing on the clipboard to paste here.")
            return
        before = None if self._marquee is None else QRect(self._marquee)
        self._commit_float()  # set any live float down first
        self._float_grid = region
        self._float_pos = self._centred_position(region.width, region.height)
        self._float_source_rect = None  # a paste lifted nothing, so it owes no hole
        self._sync_float_marquee()
        self._show_float()
        # Only the Select tool can pick a float up; any other would land it on the
        # first press, so arm the one the paste is asking to be dragged with.
        if TOOL_SPEC[self._tool].gesture is not Gesture.MARQUEE:
            self._on_tool_selected(Tool.SELECT)
        self._push_pixel_interaction(
            before,
            self._marquee,
            "paste pixels",
            after_float=FloatState(region),
        )
        self._after_pixel_change()
        self.statusBar().showMessage(
            "Pasted - drag it; it lands when the selection is cleared."
        )

    def _centred_position(self, width: int, height: int) -> tuple[int, int]:
        """Top-left for a ``width``×``height`` region centred on the viewport.

        Clamped so it starts inside the window; a region larger than the window
        still overhangs its right/bottom edge, which is the only place it can go.
        """
        assert self._doc is not None
        cx, cy = self._viewport_centre_pixel()
        window_w = self._columns.value() * self._doc.tile_width
        window_h = self._rows.value() * self._doc.tile_height
        return (
            max(0, min(cx - width // 2, window_w - width)),
            max(0, min(cy - height // 2, window_h - height)),
        )

    def _pixel_select_all(self) -> None:
        if self._doc is None:
            return
        self._commit_float()
        grid = self._window_grid()
        if grid is None:
            return
        before = self._marquee
        self._marquee = QRect(0, 0, grid.width, grid.height)
        self._canvas.set_marquee(self._marquee)
        self._push_pixel_interaction(before, self._marquee, "select all pixels")
        self._after_pixel_change()

    def _put_pixel_clipboard(self, region) -> None:
        """Put a pixel region on the OS clipboard as a rendered image.

        Image-only (no tile payload — a pixel rectangle is not tile-aligned), so
        another app receives a normal picture and a paste back re-fits it to the
        active subpalette; for a same-view copy the colors match exactly, so the
        round-trip is lossless.
        """
        assert self._doc is not None
        base = self._subpalette.value() * self._index_space()
        clipboard.put(None, render_bridge.render(region, self._doc.palette, base))

    def _take_pixel_clipboard(self):
        """The clipboard image as a region grid fitted to this view, or None.

        Direct-color views take the ARGB straight; indexed views quantize each
        pixel to the active subpalette (the same importer path a PNG paste uses).
        """
        image = clipboard.take_image()
        if image is None:
            return None
        argb = clipboard.image_to_argb(image)
        if self._is_direct_color():
            return argb
        grid, _report = importer.quantize_grid(argb, self._import_target())
        return grid

    def _pixel_key(self, key, shift: bool, ctrl: bool) -> bool:
        """Pixel-mode bare-key shortcuts routed from the nav event filter.

        Number keys 1–9 pick a tool; Escape stamps a live float, else drops the
        marquee. Returns True when it consumed the key. Inert outside pixel mode
        or with modifiers, so tile-mode navigation is untouched.
        """
        if self._edit_mode is not EditMode.PIXEL or shift or ctrl:
            return False
        if key == Qt.Key.Key_Escape:
            if self._float_grid is not None:
                self._commit_float()
                return True
            if self._marquee is not None:
                self._clear_float()
                self._after_pixel_change()
                return True
            return False
        if Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            tool = TOOL_BY_KEY.get(chr(key))
            if tool is not None:
                self._on_tool_selected(tool)
                return True
        return False

    # -- pixel-region transforms (the Pixel transform group) ---------------
    def _pixel_transform_source(self) -> QRect | None:
        """The pixel rectangle a flip/rotate acts on: the float, else the marquee.

        Returns a :class:`QRect` in image-pixel coordinates, or ``None`` when
        there is nothing to transform — the transform bar's enabled state reads
        this.
        """
        if self._float_grid is not None:
            fx, fy = self._float_pos
            return QRect(fx, fy, self._float_grid.width, self._float_grid.height)
        return self._marquee

    def _transform_pixel_region(self, op) -> None:
        """Flip/rotate the pixel selection — the float in place, else the marquee.

        A lifted float is transformed as a floating grid (still draggable); a bare
        marquee flips/rotates the window pixels under it and commits. Rotation is
        only offered for a square region (see the sync above), so the transformed
        region keeps its footprint.
        """
        if self._float_grid is not None:
            self._float_grid = op.pixel_fn(self._float_grid)
            self._sync_float_marquee()  # a rotate can swap the region's sides
            self._show_float()
            self.statusBar().showMessage(f"{op.past} the floating selection.")
            return
        if self._doc is None or self._marquee is None:
            return
        base = self._window_grid()
        if base is None:
            return
        grid = self._clone_grid(base)
        rect = self._marquee
        region = draw.extract_region(
            grid, rect.x(), rect.y(), rect.width(), rect.height()
        )
        draw.blit_region(grid, op.pixel_fn(region), rect.x(), rect.y())
        if self._commit_grid(grid, base, f"{op.verb} pixels"):
            self.statusBar().showMessage(f"{op.past} the pixel selection.")

    def _show_float(self) -> None:
        """Render the current floating grid onto the canvas overlay (no-op if none)."""
        if self._float_grid is None or self._doc is None:
            return
        base = self._subpalette.value() * self._index_space()
        image = render_bridge.render(self._float_grid, self._doc.palette, base)
        fx, fy = self._float_pos
        self._canvas.set_float(image, fx, fy)
