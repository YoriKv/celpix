"""Pixel-mode editing: the drawing tools, the pen, and the tools dock.

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
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSettings, Qt
from PySide6.QtWidgets import QDockWidget

from celpix.core import draw
from celpix.core.arrangement import compose_window, split_grid
from celpix.pipeline import importer
from celpix.ui import clipboard, render_bridge
from celpix.ui.main_window.selection import SELECTION_SHAPE_KEY, SelectionShape
from celpix.ui.tools import TOOL_BY_KEY, TOOL_SPEC, EditMode, Gesture, Tool
from celpix.ui.tools_panel import ToolsPanel
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
        # a float is a lifted grid the user drags at ``_float_pos``. ``_float_base``
        # is the window grid to composite it onto at stamp time, and
        # ``_float_source_rect`` the hole a *move* leaves (blanked on stamp) — None
        # for a paste, which removes nothing. ``_float_offset`` is the grab point
        # within the float while dragging.
        self._marquee: QRect | None = None
        self._marquee_anchor = (0, 0)
        self._float_grid = None
        self._float_pos = (0, 0)
        self._float_base = None
        self._float_source_rect: QRect | None = None
        self._float_offset = (0, 0)

    def _build_tools_dock(self) -> None:
        """The drawing-tools dock, stacked **below** the palette dock.

        Built after the palette dock so ``splitDockWidget`` can put it in the same
        right-hand column. Disabled until pixel mode with a document is active —
        the tools have nothing to act on otherwise.
        """
        self._tools_panel = ToolsPanel()
        self._tools_panel.set_tool(self._tool)
        self._tools_panel.tool_selected.connect(self._on_tool_selected)
        self._tools_dock = QDockWidget("Tools", self)
        self._tools_dock.setObjectName("tools-dock")  # keeps saveState usable
        self._tools_dock.setWidget(self._tools_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._tools_dock)
        self.splitDockWidget(
            self._palette_dock, self._tools_dock, Qt.Orientation.Vertical
        )
        self._tools_dock.setEnabled(False)
        # Picking a palette swatch takes the pen back from a direct-color eyedrop.
        self._palette_panel.color_selected.connect(self._on_pen_color_selected)

    def _connect_pixel_canvas(self) -> None:
        """Wire the canvas's pixel gestures to the tool handlers (called once the
        canvas exists)."""
        self._canvas.pixel_pressed.connect(self._on_pixel_pressed)
        self._canvas.pixel_moved.connect(self._on_pixel_moved)
        self._canvas.pixel_released.connect(self._on_pixel_released)

    # -- mode switching ----------------------------------------------------
    def _set_edit_mode(self, mode: EditMode) -> None:
        """Switch tile ⇄ pixel editing and converge every surface with it.

        Pixel mode is rectangle-only, so the Selection Shape picker is forced to
        Rectangle and disabled (their saved preference is preserved, restored on
        the way back); the canvas switches to painting; the tools dock arms; the
        Block transform group hides. Any tile selection is dropped so its
        highlight doesn't linger over the paint surface.
        """
        if mode == self._edit_mode:
            return
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
        self._sync_mode_ui()
        if self._doc is not None:
            self._refresh_view()

    def _sync_mode_ui(self) -> None:
        """Show/hide the mode-dependent transform-bar bits and the tools dock."""
        pixel = self._edit_mode is EditMode.PIXEL
        # The Block group has no meaning on a pixel selection; hide it. The Tile
        # group is repurposed to transform the pixel region (see transform.py).
        for action in (*self._block_group.flips, *self._block_group.rotates):
            action.setVisible(not pixel)
        self._tools_dock.setEnabled(pixel and self._doc is not None)
        self._sync_transform_actions()

    def _on_tool_selected(self, tool: Tool) -> None:
        # Switching tools stamps any live floating selection first, so it can't
        # straddle two tools.
        self._commit_float()
        self._tool = tool
        QSettings().setValue(TOOL_KEY, tool.value)
        self._tools_panel.set_tool(tool)

    # -- pen ---------------------------------------------------------------
    def _on_pen_color_selected(self, _index: int) -> None:
        """A palette-swatch pick takes the pen back from a direct-color eyedrop."""
        self._pen_argb = None

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
        """Set ``pixels`` on ``grid`` to the pen (or ``value``), clipped to bounds."""
        if value is None:
            value = self._pen_value()
        w, h = grid.width, grid.height
        for px, py in pixels:
            if 0 <= px < w and 0 <= py < h:
                grid.set(px, py, value)

    def _render_preview(self, grid) -> None:
        """Show a working grid on the canvas without committing it (live preview)."""
        assert self._doc is not None
        base = self._subpalette.value() * self._index_space()
        self._canvas.set_image(render_bridge.render(grid, self._doc.palette, base))

    def _commit_grid(self, grid, base_grid, text: str) -> int:
        """Write only the tiles that differ between ``base_grid`` and ``grid``.

        Both are split back into storage-order tiles; the changed slots form a
        contiguous span (``min..max``) that goes out as one ``_apply_tile_edit``,
        so untouched tiles are never re-encoded. Always ends with a real
        ``_refresh_view`` — that repaints the committed result, and cleanly
        reverts a transient preview when nothing changed.
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
        base = self._window_grid()
        if base is None:
            return
        grid = self._clone_grid(base)
        self._paint_pixels(grid, draw.flood_fill(grid, x, y))
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

    # -- marquee & floating selection --------------------------------------
    def _marquee_press(self, x: int, y: int) -> None:
        """Press with the Select tool: move an existing selection, else start one.

        Pressing inside the current marquee lifts its pixels into a floating
        selection and begins a move (the source shows blanked until the drop);
        anywhere else stamps any live float and anchors a fresh rectangle the drag
        grows.
        """
        self._commit_float()
        if self._marquee is not None and self._marquee.contains(x, y):
            self._lift_float(cut=True)
            if self._float_grid is not None:
                fx, fy = self._float_pos
                self._float_offset = (x - fx, y - fy)
            return
        self._marquee_anchor = (x, y)
        self._marquee = QRect(x, y, 1, 1)
        self._canvas.set_marquee(self._marquee)

    def _marquee_drag(self, x: int, y: int) -> None:
        if self._float_grid is not None:
            ox, oy = self._float_offset
            self._float_pos = (x - ox, y - oy)
            self._show_float()
            return
        if self._marquee is None:
            return
        ax, ay = self._marquee_anchor
        self._marquee = QRect(min(ax, x), min(ay, y), abs(x - ax) + 1, abs(y - ay) + 1)
        self._canvas.set_marquee(self._marquee)

    def _marquee_release(self, x: int, y: int) -> None:
        # A drawn marquee stays as the selection; a moved float keeps floating
        # until it is stamped. Refresh the actions gated on "a selection exists".
        self._after_pixel_change()

    def _lift_float(self, cut: bool) -> None:
        """Lift the marquee's pixels into a floating selection.

        Nothing is committed here — the move becomes a single undo step at stamp
        time. When ``cut`` the source is shown blanked (a hole) while the float is
        dragged and blanked for real on the drop; a non-cut lift leaves it intact.
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
        self._float_base = base
        self._float_source_rect = rect if cut else None
        self._marquee = None
        self._canvas.set_marquee(None)
        if cut:
            preview = self._clone_grid(base)
            self._blank_rect(preview, rect)
            self._render_preview(preview)
        self._show_float()
        self._after_pixel_change()

    def _commit_float(self) -> None:
        """Stamp a live floating selection into the document as one undo step.

        Composites the float onto the base it was lifted/pasted over — blanking a
        move's source hole first — and commits the net change. A no-op when no
        float is live, so the drawing tools can call it freely before a new
        gesture.
        """
        if self._float_grid is None:
            return
        base = self._float_base if self._float_base is not None else self._window_grid()
        float_grid = self._float_grid
        fx, fy = self._float_pos
        source = self._float_source_rect
        moved = source is not None
        self._clear_float()
        if base is None:
            return
        dest = self._clone_grid(base)
        if source is not None:
            self._blank_rect(dest, source)
        draw.blit_region(dest, float_grid, fx, fy)
        self._commit_grid(dest, base, "move pixels" if moved else "paste pixels")

    def _clear_float(self) -> None:
        """Drop the float and marquee and their canvas overlays (no commit)."""
        self._float_grid = None
        self._float_base = None
        self._float_source_rect = None
        self._marquee = None
        if hasattr(self, "_canvas"):
            self._canvas.set_float(None)
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
    def _pixel_copy(self) -> None:
        rect = self._marquee
        if self._doc is None or rect is None:
            self.statusBar().showMessage("Select a pixel rectangle to copy.")
            return
        base = self._window_grid()
        if base is None:
            return
        region = draw.extract_region(
            base, rect.x(), rect.y(), rect.width(), rect.height()
        )
        self._put_pixel_clipboard(region)
        self.statusBar().showMessage(f"Copied {rect.width()}×{rect.height()} pixels.")

    def _pixel_cut(self) -> None:
        rect = self._marquee
        if self._doc is None or rect is None:
            return
        base = self._window_grid()
        if base is None:
            return
        region = draw.extract_region(
            base, rect.x(), rect.y(), rect.width(), rect.height()
        )
        self._put_pixel_clipboard(region)
        grid = self._clone_grid(base)
        self._blank_rect(grid, rect)
        self._clear_float()
        if self._commit_grid(grid, base, "cut pixels"):
            self.statusBar().showMessage(f"Cut {rect.width()}×{rect.height()} pixels.")

    def _pixel_clear(self) -> None:
        rect = self._marquee
        if self._doc is None or rect is None:
            return
        base = self._window_grid()
        if base is None:
            return
        grid = self._clone_grid(base)
        self._blank_rect(grid, rect)
        self._clear_float()
        if self._commit_grid(grid, base, "clear pixels"):
            self.statusBar().showMessage(
                f"Cleared {rect.width()}×{rect.height()} pixels."
            )

    def _pixel_paste(self) -> None:
        if self._doc is None:
            return
        region = self._take_pixel_clipboard()
        if region is None:
            self.statusBar().showMessage("Nothing on the clipboard to paste here.")
            return
        self._commit_float()  # stamp any live float first
        at = (self._marquee.x(), self._marquee.y()) if self._marquee else (0, 0)
        self._marquee = None
        self._canvas.set_marquee(None)
        self._float_grid = region
        self._float_pos = at
        self._float_base = self._window_grid()
        self._float_source_rect = None
        self._show_float()
        self._after_pixel_change()
        self.statusBar().showMessage(
            "Pasted a floating selection — drag it, then Esc or click away to drop."
        )

    def _pixel_select_all(self) -> None:
        if self._doc is None:
            return
        self._commit_float()
        grid = self._window_grid()
        if grid is None:
            return
        self._marquee = QRect(0, 0, grid.width, grid.height)
        self._canvas.set_marquee(self._marquee)
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

    # -- pixel-region transforms (Tile group in pixel mode) ----------------
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
