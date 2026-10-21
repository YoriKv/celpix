"""Edit Subsprites: the Edit Tiles tool read on a sprite map's pieces.

The tool is not a sibling of Edit Tiles but the same one — the same action pair,
the same ``T``, the same ``_stamping`` flag — offered on a sprite object whose
codec declares what its records can hold (``docs/design/sprite-map.md``
§10.1). A sprite's pieces sit at signed pixel offsets and overlap, so no slot
names one; the canvas reports the stamp gestures in **pixels** there
(:meth:`~celpix.ui.canvas.Canvas.set_stamp_in_pixels`) and this mixin reads
them on pieces, resolved through :func:`~celpix.pipeline.pipeline.sprite_hit`
— the resolution the pick and the pen already share.

**The canvas moves; the menu and the keys hold the structure.** A left drag is
the rearrange drag on pieces: a press inside the pick lifts the whole pick, a
press on another piece picks and lifts it, release commits one step and keeps
the pick. The arrows nudge, a right click eyedrops a piece as the held record,
a right drag selects every piece its band touches, and a click on empty sheet
lays the held piece. The four clipboard verbs, Duplicate, the reorder rows and
Mirror About Origin are record ops (:mod:`celpix.core.sprite_edit`), dispatched
on the format's structure kind.

**Every edit is a cell edit.** Each gesture computes a new cell list — the
codec's inverse writes the offsets, the size and the drawn bit — and lands it
through :meth:`~...tilemap_edit.TilemapEditMixin._apply_cells`, which carries
the pick on the undo step. Nothing here writes ``doc.cells`` directly, so a
refused edit (a frame that would not build, an offset the field cannot hold)
is a status-line message and nothing on the stack.

**The geometry is frozen at the press.** Every frame is drawn in one shared
box, so a piece dragged past the edge would grow the box and shift every
frame's slot mid-drag. The drag therefore reads the pointer against the sheet
as it stood at the press, in object coordinates, and the picture re-renders
only when the release lands.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QKeySequence
from PySide6.QtWidgets import QApplication

from celpix.core.errors import PipelineError
from celpix.core.sprite import Subsprite
from celpix.core.sprite_edit import (
    SpriteEditError,
    add_subsprite,
    fit_group_delta,
    move_subsprite,
    record_positions,
    reflect_group,
    remove_subsprites,
    reorder_subsprite,
)
from celpix.core.tilemap import BLANK, Cell, CellGrid, CellOp
from celpix.pipeline import pipeline
from celpix.ui.widgets import counted, make_action

if TYPE_CHECKING:
    # For the checker only: a mixin is a slice of the window, so its
    # ``self`` is the window, widgets, state and all (see the package
    # docstring). At runtime it is a plain mixin.
    from celpix.pipeline.pipeline import SpriteSheet
    from celpix.ui.main_window.window import MainWindow as _Host
else:
    _Host = object

_CTRL = Qt.KeyboardModifier.ControlModifier
_SHIFT = Qt.KeyboardModifier.ShiftModifier

# The arrows' unit step, (dx, dy); Shift scales it to one tile.
_NUDGE_KEYS = {
    Qt.Key.Key_Left: (-1, 0),
    Qt.Key.Key_Right: (1, 0),
    Qt.Key.Key_Up: (0, -1),
    Qt.Key.Key_Down: (0, 1),
}

_DRAG_HINT = "Shift locks an axis, Ctrl snaps to tiles"
_NOTHING_PICKED = "No subsprite picked - click a piece, or right-drag over several."
# Kept to one piece because a several-piece move between frames renumbers the
# records of every piece after each splice, and a refused half-move would leave
# a frame holding pieces the user never dropped there.
_ONE_ACROSS = "Subsprites move between frames one at a time - pick one piece"

# Every op here can be refused by the format, and each refusal is worded for the
# status line already: a record op's own (SpriteEditError), the codec's inverse
# naming the range (ValueError), and a frame list that would not build.
_REFUSALS = (SpriteEditError, ValueError, PipelineError)


@dataclass
class SubspriteDrag:
    """A left gesture on a sprite's pieces, from the press to the release.

    ``sheet`` is the layout at the press, which every pointer position is read
    against until the release (the module docstring says why). ``grab`` is the
    record the press landed on and ``frame`` the slot it was in. Nothing is
    lifted until the pointer leaves the press pixel — a press and release in
    place is a **click**, which cycles the pick rather than moving anything —
    so ``records``/``pieces`` are empty until then.
    """

    press: tuple[int, int]
    sheet: SpriteSheet
    frame: int
    grab: int
    modifiers: Qt.KeyboardModifier
    lifted: bool = False
    records: tuple[int, ...] = ()
    pieces: tuple[Subsprite, ...] = ()
    homes: tuple[int, ...] = ()  # each piece's own frame
    image: QImage | None = None
    image_at: tuple[int, int] = (0, 0)  # object coords of the float's corner
    delta: tuple[int, int] = (0, 0)
    target: int = 0
    valid: bool = True
    # A move into another frame, tried once per frame the pointer visits: the
    # cells and the piece's new record, or the refusal's text.
    trials: dict[int, tuple[list[Cell], int] | str] = field(default_factory=dict)


class SpriteEditMixin(_Host):
    """The sprite reading of the Edit Tiles tool: gestures, keys and record ops.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object. See the module docstring for what it owns.
    """

    # -- state & construction ----------------------------------------------
    def _init_sprite_edit(self) -> None:
        """Seed the tool's sprite state; called from the window's ``__init__``
        before the menus, which build the Edit rows."""
        self._subsprite_drag: SubspriteDrag | None = None
        # Which run of arrow nudges the next one belongs to, so a held key is
        # one undo step (``TilemapCellsCommand``'s ``run``). Bumped by every
        # press, tool change and other key — the text window's rule.
        self._nudge_run = 0
        # The sheet pixel under the pointer while armed (None off the canvas):
        # where the ghost sits and which frame a paste with no pick lands in.
        self._subsprite_hover: tuple[int, int] | None = None
        # The offsets of the pieces the last subsprite copy put in the cell
        # buffer, beside the buffer itself: a paste lands at them only while
        # the buffer is still that copy, not one a grid map's copy replaced.
        self._subsprite_clip: tuple[CellGrid, tuple[tuple[int, int], ...]] | None = None
        # The ghost's last landing and what it rendered, so a hover that stays
        # on one tile does not re-run the add it previews.
        self._subsprite_ghost: tuple[tuple, tuple[QImage, tuple[int, int]] | None] = (
            (),
            None,
        )
        self._subsprite_actions: tuple = ()

    def _connect_sprite_edit_canvas(self) -> None:
        """Wire the canvas's pixel stamp gestures (called once it exists)."""
        canvas = self._canvas
        canvas.stamp_pixel_pressed.connect(self._on_subsprite_pressed)
        canvas.stamp_pixel_moved.connect(self._on_subsprite_moved)
        canvas.stamp_pixel_released.connect(self._on_subsprite_released)
        canvas.stamp_pixel_area_picked.connect(self._on_subsprite_area_picked)
        canvas.stamp_pixel_hovered.connect(self._on_subsprite_hovered)
        canvas.stamp_pixel_cancelled.connect(self._on_subsprite_cancelled)

    def _build_subsprite_actions(self, menu) -> None:  # noqa: ANN001 — a QMenu
        """The Edit rows the tool grows on a sprite map.

        Their keys are **display-only** (a widget-context shortcut that never
        fires): Alt+Up/Down and Ctrl+D are the Files pane's own keys there, and
        a window-wide binding would take them from that list while it has focus.
        So the working binding is the app-wide key filter
        (:meth:`_subsprite_key`), which already yields to a focused list, and the
        rows are where the menu and the F1 guide read the keys from.
        """
        widget = Qt.ShortcutContext.WidgetShortcut
        specs = (
            (
                "_subsprite_forward_action",
                "Bring Subsprite Forward",
                "Alt+Up",
                lambda: self._reorder_subsprite("forward"),
                "Draw the picked piece one step further forward\n"
                "among its frame's pieces",
            ),
            (
                "_subsprite_backward_action",
                "Send Subsprite Backward",
                "Alt+Down",
                lambda: self._reorder_subsprite("backward"),
                "Draw the picked piece one step further back\namong its frame's pieces",
            ),
            (
                "_subsprite_front_action",
                "Bring Subsprite to Front",
                "Alt+Shift+Up",
                lambda: self._reorder_subsprite("front"),
                "Draw the picked piece in front of its frame's others",
            ),
            (
                "_subsprite_back_action",
                "Send Subsprite to Back",
                "Alt+Shift+Down",
                lambda: self._reorder_subsprite("back"),
                "Draw the picked piece behind its frame's others",
            ),
            (
                "_subsprite_duplicate_action",
                "Duplicate Subsprites",
                "Ctrl+D",
                self._duplicate_subsprites,
                "Copy the picked pieces one tile along,\nand pick the copies",
            ),
            (
                "_subsprite_mirror_h_action",
                "Mirror Frame About Origin Horizontally",
                None,
                lambda: self._mirror_about_origin(horizontal=True),
                "Flip the picked pieces and reflect their offsets\n"
                "about the frame's origin: x' = -x - w",
            ),
            (
                "_subsprite_mirror_v_action",
                "Mirror Frame About Origin Vertically",
                None,
                lambda: self._mirror_about_origin(horizontal=False),
                "Flip the picked pieces and reflect their offsets\n"
                "about the frame's origin: y' = -y - h",
            ),
        )
        menu.addSeparator()
        actions = []
        for name, text, key, slot, tip in specs:
            action = make_action(
                self,
                text,
                slot,
                menu=menu,
                tip=tip,
                shortcut=None if key is None else QKeySequence(key),
                context=widget if key is not None else None,
                enabled=False,
            )
            action.setVisible(False)
            setattr(self, name, action)
            actions.append(action)
        self._subsprite_actions = tuple(actions)
        # The pick moves without a refresh, so the rows are re-decided when the
        # menu opens as well as on the refresh path (_sync_stamp_actions).
        menu.aboutToShow.connect(self._sync_subsprite_actions)

    def _sync_subsprite_actions(self) -> None:
        """Show the rows on a sprite map; enable them armed, with a pick."""
        doc = self._doc
        on_sprite = doc is not None and doc.is_sprite
        live = on_sprite and self._stamping and bool(self._picked_subsprites())
        for action in self._subsprite_actions:
            action.setVisible(on_sprite)
            action.setEnabled(live)

    # -- asking the format ---------------------------------------------------
    def _sprite_tool_live(self) -> bool:
        doc = self._doc
        return self._stamping and doc is not None and doc.is_sprite

    def _sprite_ops(self):  # noqa: ANN202 — (limits, place, assign) or None
        """Everything a record op needs, or None with the reason said.

        The codec's inverse as the property row binds it
        (:meth:`~...tilemap_edit.TilemapEditMixin._place_subsprite`,
        :meth:`~...tilemap_edit.TilemapEditMixin._assign_frames`), so a drag
        and a typed offset write the same bits the same way.
        """
        limits = self._subsprite_limits()
        if limits is None:
            self._refuse_view_only()
            return None
        return limits, self._place_subsprite, self._assign_frames

    def _frame_cells(self, cells: list[Cell]) -> list:
        """The frames ``cells`` would build, for an op chained on another.

        Encoded over a context copy first, the order ``_apply_cells`` trials an
        edit in: a counted format's encode is what states the sizes the frames
        are cut at.
        """
        doc = self._doc
        assert doc is not None and doc.tilemap_config is not None
        trial = doc.tilemap_ctx.copy()
        preset = doc.tilemap_config.interpret_preset_id
        pipeline.encode_cells(cells, preset, self._registry, trial)
        return pipeline.sprite_frames_of(cells, preset, self._registry, trial)

    def _add_pieces(
        self, specs: Sequence[tuple[int, Cell, int, int]]
    ) -> tuple[list[Cell], list[int]]:
        """``specs`` — ``(frame, template, x, y)`` each — added in order.

        Returns the cells and every new piece's record. Raises what the op or
        the codec refuses. Each add after the first re-frames the cells, since
        the next free slot and the run each frame owns are read off the frames;
        and under the inserting kinds an insert shifts every record at or after
        it, the pieces already added included.
        """
        ops = self._sprite_ops()
        if ops is None:
            raise SpriteEditError("This sprite map is view-only")
        limits, place, assign = ops
        doc = self._doc
        cells = list(doc.cells)
        frames = doc.sprite_frames or []
        added: list[int] = []
        for count, (frame, template, x, y) in enumerate(specs):
            if count:
                frames = self._frame_cells(cells)
            cells, at = add_subsprite(
                cells,
                frames,
                limits,
                frame,
                template,
                x=x,
                y=y,
                size=None,
                place=place,
                assign=assign,
            )
            if limits.structure != "slots":
                added = [r + 1 if r >= at else r for r in added]
            added.append(at)
        return cells, added

    # -- geometry ------------------------------------------------------------
    @staticmethod
    def _frame_slot_at(sheet: SpriteSheet, x: int, y: int) -> int | None:
        """The frame slot the sheet pixel is in, or None off every frame."""
        _left, _top, width, height = sheet.box
        if not (0 <= x < sheet.across * width and 0 <= y < sheet.down * height):
            return None
        at = (y // height) * sheet.across + x // width
        return at if at < sheet.frames else None

    @staticmethod
    def _object_at(sheet: SpriteSheet, x: int, y: int) -> tuple[int, int]:
        """The sheet pixel in its frame's object coordinates."""
        left, top, width, height = sheet.box
        return x % width + left, y % height + top

    @staticmethod
    def _slot_origin(sheet: SpriteSheet, at: int) -> tuple[int, int]:
        """Where object (0, 0) of frame slot ``at`` sits on the sheet."""
        left, top, width, height = sheet.box
        return (at % sheet.across) * width - left, (at // sheet.across) * height - top

    def _sprite_tile_size(self) -> tuple[int, int]:
        doc = self._doc
        return max(1, doc.tile_width), max(1, doc.tile_height)

    def _render_pieces(
        self, pieces: Sequence[Subsprite]
    ) -> tuple[QImage, tuple[int, int]] | None:
        """``pieces`` drawn as the sheet draws them, cropped to their own box.

        Through :func:`~celpix.pipeline.pipeline.sprite_image` itself over a
        shallow copy of the document holding just these pieces as its one
        frame — the strip's own walk, flips, row bias and transparency, so the
        float and the ghost cannot look different from what lands. The copy
        shares the tile bank's cache, so nothing is decoded again. Index 0 is
        painted transparent: a piece is carried over the sheet, and its holes
        must show what is under it. Returns the image and the object
        coordinates of its top-left.
        """
        doc = self._doc
        if doc is None or not pieces:
            return None
        trial = copy.copy(doc)
        trial.sprite_frames = [tuple(pieces)]
        try:
            grid, sheet = pipeline.sprite_image(trial, self._registry, 1)
        except (KeyError, PipelineError):
            return None
        image = self._paint_grid(
            grid, pinned=doc.folds_palette_rows, transparent_zero=True
        )
        tile_w, tile_h = self._sprite_tile_size()
        spans = [(sub.x, sub.y, *sub.pixels(tile_w, tile_h)) for sub in pieces]
        x0 = min(x for x, _, _, _ in spans)
        y0 = min(y for _, y, _, _ in spans)
        x1 = max(x + w for x, _, w, _ in spans)
        y1 = max(y + h for _, y, _, h in spans)
        left, top = sheet.box[:2]
        return image.copy(QRect(x0 - left, y0 - top, x1 - x0, y1 - y0)), (x0, y0)

    # -- the left button -----------------------------------------------------
    def _on_subsprite_pressed(
        self,
        x: int,
        y: int,
        button: object,
        modifiers: Qt.KeyboardModifier | None = None,
    ) -> None:
        """A press: lift what is under it, eyedrop it, add, or clear the pick.

        The piece a press is *on* is the first of the stack under the pointer
        that is already picked, or the front one: a press over a stack whose
        second piece is picked means that piece, or a click there could never
        cycle past it — the press would re-pick the front one every time.
        """
        if not self._sprite_tool_live():
            return
        self._end_nudge_run()
        if button == Qt.MouseButton.RightButton:
            self._eyedrop_subsprite(x, y)
            return
        if button != Qt.MouseButton.LeftButton:
            return
        mods = QApplication.keyboardModifiers() if modifiers is None else modifiers
        doc = self._doc
        stack = [
            hit
            for hit in pipeline.sprite_hits(
                doc, self._registry, self._tilemap_columns(), x, y
            )
            if hit.piece.record is not None
        ]
        if not stack:
            if self._held_tile_id() is not None:
                self._add_held_subsprites(x, y, bool(mods & _CTRL))
            elif not mods & _CTRL:
                # A toggle that misses changes nothing, as in a list.
                self._set_picked_records([])
            return
        picked = set(self._picked_records)
        hit = next((h for h in stack if h.piece.record in picked), None)
        if hit is None:
            hit = stack[0]
            if not mods & (_CTRL | _SHIFT):
                # A fresh pick, so a stale cycle anchor must not make the
                # click that follows step past the piece just taken.
                self._pick_tile, self._pick_depth = None, 0
                self._set_picked_records([hit.piece.record])
        sheet = self._sprite_sheet()
        if sheet is None:
            return
        self._subsprite_drag = SubspriteDrag(
            press=(x, y),
            sheet=sheet,
            frame=hit.frame,
            grab=hit.piece.record,
            modifiers=mods,
            target=hit.frame,
        )
        self._sync_subsprite_preview()

    def _lift_subsprites(self, drag: SubspriteDrag) -> bool:
        """Take the pick into the air: the pieces, their homes and the float.

        A press with Ctrl or Shift left the pick alone (it might have been a
        toggle); a drag out of it means the pressed piece, so it is picked
        plainly here. The float holds the pieces of the pressed frame only —
        one image in one frame's coordinates — while the drop boxes outline
        every piece, wherever its frame is.
        """
        if drag.grab not in self._picked_records:
            self._pick_tile, self._pick_depth = None, 0
            self._set_picked_records([drag.grab])
        drawn = self._picked_subsprites()
        if not drawn:
            return False
        positions = self._record_positions()
        drag.records = tuple(record for record, _ in drawn)
        drag.pieces = tuple(sub for _, sub in drawn)
        drag.homes = tuple(positions[record][0] for record in drag.records)
        rendered = self._render_pieces(
            [
                sub
                for sub, home in zip(drag.pieces, drag.homes, strict=True)
                if home == drag.frame
            ]
        )
        if rendered is not None:
            drag.image, drag.image_at = rendered
        drag.lifted = True
        return True

    def _on_subsprite_moved(
        self, x: int, y: int, modifiers: Qt.KeyboardModifier | None = None
    ) -> None:
        drag = self._subsprite_drag
        if drag is None or not self._sprite_tool_live():
            return
        if not drag.lifted:
            if (x, y) == drag.press or not self._lift_subsprites(drag):
                return
        mods = QApplication.keyboardModifiers() if modifiers is None else modifiers
        self._update_subsprite_drag(drag, x, y, mods)

    def _update_subsprite_drag(
        self, drag: SubspriteDrag, x: int, y: int, mods: Qt.KeyboardModifier
    ) -> None:
        """Re-aim the float and the drop boxes at the pointer, and say where.

        The delta is in **object** coordinates against the press-time sheet,
        so a pointer in another frame's slot reads as the same offset there —
        the "same object coordinates plus the delta" a cross-frame move lands
        at — and inside the pressed frame it is simply the pointer's delta.
        """
        limits = self._subsprite_limits()
        if limits is None:
            return
        sheet = drag.sheet
        target = self._frame_slot_at(sheet, x, y)
        if target is None:
            # Off every frame (a partial last row): stay in the press's frame
            # and read the pointer as a plain pixel delta.
            target = drag.frame
            dx, dy = x - drag.press[0], y - drag.press[1]
        else:
            ox, oy = self._object_at(sheet, x, y)
            px, py = self._object_at(sheet, *drag.press)
            dx, dy = ox - px, oy - py
        if mods & _SHIFT:
            if abs(dx) >= abs(dy):
                dy = 0
            else:
                dx = 0
        if mods & _CTRL:
            tile_w, tile_h = self._sprite_tile_size()
            dx, dy = tile_w * round(dx / tile_w), tile_h * round(dy / tile_h)
        fx, fy = fit_group_delta(drag.pieces, dx, dy, limits)
        drag.delta, drag.target = (fx, fy), target
        refusal = None
        if target != drag.frame:
            refusal = self._cross_frame_refusal(drag, target)
        drag.valid = refusal is None
        self._show_subsprite_drag(drag)
        clamped = None
        if (fx, fy) != (dx, dy) and limits is not None:
            clamped = limits.x_range if fx != dx else limits.y_range
        self.statusBar().showMessage(
            self._drag_status(drag, clamped=clamped, refusal=refusal)
        )

    def _cross_frame_refusal(self, drag: SubspriteDrag, target: int) -> str | None:
        """Why the pick cannot move into ``target``, or None where it can.

        Tried by running the move itself once per frame the pointer visits, so
        the red outline and the release cannot disagree about a full frame.
        """
        if len(drag.records) != 1:
            return _ONE_ACROSS
        if target not in drag.trials:
            ops = self._sprite_ops()
            doc = self._doc
            if ops is None or doc is None:
                return "This sprite map is view-only"
            limits, place, assign = ops
            try:
                drag.trials[target] = move_subsprite(
                    list(doc.cells),
                    doc.sprite_frames or [],
                    limits,
                    drag.records[0],
                    target,
                    place=place,
                    assign=assign,
                )
            except _REFUSALS as exc:
                drag.trials[target] = str(exc)
        tried = drag.trials[target]
        return tried if isinstance(tried, str) else None

    def _show_subsprite_drag(self, drag: SubspriteDrag) -> None:
        fx, fy = drag.delta
        sheet = drag.sheet
        tile_w, tile_h = self._sprite_tile_size()
        rects = []
        for sub, home in zip(drag.pieces, drag.homes, strict=True):
            # A piece moving into another frame lands in that frame's slot;
            # the rest of a several-frame pick moves within its own.
            at = drag.target if home == drag.frame else home
            ox, oy = self._slot_origin(sheet, at)
            wide, tall = sub.pixels(tile_w, tile_h)
            rects.append(QRect(ox + sub.x + fx, oy + sub.y + fy, wide, tall))
        self._canvas.set_drop_rects(rects, valid=drag.valid)
        if drag.image is not None:
            ox, oy = self._slot_origin(sheet, drag.target)
            self._canvas.set_float(
                drag.image, ox + drag.image_at[0] + fx, oy + drag.image_at[1] + fy
            )

    def _drag_status(
        self, drag: SubspriteDrag, *, clamped: range | None, refusal: str | None
    ) -> str:
        fx, fy = drag.delta
        if refusal is not None:
            return f"{refusal} - release puts it back"
        if len(drag.pieces) == 1:
            sub = drag.pieces[0]
            position = self._record_positions().get(drag.records[0], (0, 0))[1]
            where = f" in frame {drag.target}" if drag.target != drag.frame else ""
            said = (
                f"Moving subsprite {position}{where}: ({sub.x:+d}, {sub.y:+d}) -> "
                f"({sub.x + fx:+d}, {sub.y + fy:+d})"
            )
        else:
            said = f"Moving {len(drag.pieces)} subsprites by ({fx:+d}, {fy:+d})"
        if clamped is not None:
            said += f" - stopped at the edge ({_range_text(clamped)})"
        return f"{said} - {_DRAG_HINT}"

    def _on_subsprite_released(
        self, x: int, y: int, modifiers: Qt.KeyboardModifier | None = None
    ) -> None:
        """Commit the move as one step — or, for a click, cycle the pick."""
        drag = self._subsprite_drag
        if drag is None or not self._sprite_tool_live():
            self._abort_subsprite_drag()
            return
        if not drag.lifted:
            self._subsprite_drag = None
            # A click: the pick mixin's own rule, cycling down the stack on a
            # plain one and toggling or spanning with the modifiers the press
            # carried.
            self._on_pixel_picked(*drag.press, drag.modifiers)
            return
        mods = QApplication.keyboardModifiers() if modifiers is None else modifiers
        self._update_subsprite_drag(drag, x, y, mods)
        self._abort_subsprite_drag()
        if not drag.valid:
            self._announce_subsprite()
            return
        if drag.target != drag.frame:
            self._commit_cross_frame(drag)
            return
        fx, fy = drag.delta
        if (fx, fy) == (0, 0):
            self._announce_subsprite()
            return
        ops = self._sprite_ops()
        if ops is None:
            return
        _limits, place, _assign = ops
        cells = list(self._doc.cells)
        picked = list(self._picked_records)
        try:
            for record, sub in zip(drag.records, drag.pieces, strict=True):
                cells[record] = place(cells[record], x=sub.x + fx, y=sub.y + fy)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc))
            return
        if self._apply_cells(cells, "move subsprites", pick=(picked, picked)):
            self.statusBar().showMessage(
                f"Moved {counted(len(drag.records), 'subsprite')} by ({fx:+d}, {fy:+d})"
            )

    def _commit_cross_frame(self, drag: SubspriteDrag) -> None:
        tried = drag.trials.get(drag.target)
        ops = self._sprite_ops()
        if ops is None or tried is None or isinstance(tried, str):
            return
        _limits, place, _assign = ops
        cells, record = list(tried[0]), tried[1]
        sub = drag.pieces[0]
        fx, fy = drag.delta
        try:
            cells[record] = place(cells[record], x=sub.x + fx, y=sub.y + fy)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc))
            return
        before = list(self._picked_records)
        if self._apply_cells(cells, "move subsprite", pick=(before, [record])):
            self.statusBar().showMessage(
                f"Moved subsprite to frame {drag.target} at "
                f"({sub.x + fx:+d}, {sub.y + fy:+d})"
            )

    def _on_subsprite_cancelled(self) -> None:
        if self._abort_subsprite_drag():
            self._announce_subsprite()

    def _abort_subsprite_drag(self) -> bool:
        """Put a carried pick back down, pushing nothing; True if one was live.

        Nothing is previewed on the document — the float and the drop boxes
        are canvas overlays — so abandoning the drag is taking those down
        (:meth:`~...pixel_edit.PixelEditMixin._abort_live_gestures` calls
        this ahead of any apply).
        """
        drag = self._subsprite_drag
        if drag is None:
            return False
        self._subsprite_drag = None
        if drag.lifted:
            self._canvas.set_float(None)
            self._canvas.set_drop_rects(None)
        self._sync_subsprite_preview()
        return True

    # -- the right button ----------------------------------------------------
    def _eyedrop_subsprite(self, x: int, y: int) -> None:
        """A right click: hold the piece under it as the record a click adds.

        The whole record, opaque bits and all, as the stamp's eyedropper takes
        a cell (:meth:`~...stamp_tool.StampToolMixin._pick_tile_at`) — and its
        palette row into Palette Row, which is the row the held piece is shown
        and laid in.
        """
        doc = self._doc
        hit = pipeline.sprite_hit(doc, self._registry, self._tilemap_columns(), x, y)
        record = None if hit is None else hit.piece.record
        if record is None or not 0 <= record < len(doc.cells or ()):
            self.statusBar().showMessage("No subsprite here to pick.")
            return
        cell = doc.cells[record]
        span = pipeline.tile_source_span(
            doc, self._cell_index_limit(), self._cell_index_runs()
        )
        if cell.index not in span:
            self._clear_source_tile()
            self.statusBar().showMessage(
                f"Tile ${cell.index:X} is not in the tile source - nothing picked."
            )
            return
        self._set_source_tile(cell.index, cell)
        row = None
        if doc.cells_carry_palette_rows:
            row = self._drawn_palette_row(cell.palette_row)
            self._palette_row.setValue(row)
        tail = "" if row is None else f", palette row {row}"
        self.statusBar().showMessage(
            f"Holding subsprite {hit.subsprite} of frame {hit.frame} "
            f"(tile ${cell.index:X}{tail}) - click empty sheet to add it."
        )

    def _on_subsprite_area_picked(self, x0: int, y0: int, x1: int, y1: int) -> None:
        """A right drag: pick every piece whose box the band **touches**.

        Touches rather than encloses, stated because editors split evenly
        between the two (``sprite-map.md`` §10.2) — and touching is the one
        that reaches a piece hidden under another without sweeping its whole
        box. Replaces the pick, in frame and draw order.
        """
        if not self._sprite_tool_live():
            return
        self._end_nudge_run()
        doc, sheet = self._doc, self._sprite_sheet()
        if sheet is None:
            return
        band = QRect(x0, y0, x1 - x0 + 1, y1 - y0 + 1)
        tile_w, tile_h = self._sprite_tile_size()
        found = []
        for at, frame in enumerate(doc.shown_frames):
            ox, oy = self._slot_origin(sheet, at)
            for sub in frame:
                if sub.record is None:
                    continue
                wide, tall = sub.pixels(tile_w, tile_h)
                if band.intersects(QRect(ox + sub.x, oy + sub.y, wide, tall)):
                    found.append(sub.record)
        self._pick_tile, self._pick_depth = None, 0
        self._set_picked_records(found)
        if not found:
            self.statusBar().showMessage("No subsprite under the band.")

    # -- adding --------------------------------------------------------------
    def _subsprite_landing(
        self, x: int, y: int, pixel: bool
    ) -> tuple[int, int, int] | None:
        """``(frame, x, y)`` a click at the sheet pixel adds at, or None.

        Snapped to the tile grid in object coordinates unless ``pixel`` (Ctrl):
        pieces start on the grid and are nudged off it.
        """
        sheet = self._sprite_sheet()
        if sheet is None:
            return None
        frame = self._frame_slot_at(sheet, x, y)
        if frame is None:
            return None
        ox, oy = self._object_at(sheet, x, y)
        if not pixel:
            tile_w, tile_h = self._sprite_tile_size()
            ox, oy = ox - ox % tile_w, oy - oy % tile_h
        return frame, ox, oy

    def _held_specs(
        self, frame: int, x: int, y: int
    ) -> list[tuple[int, Cell, int, int]] | None:
        """What a click lays at ``(x, y)`` in ``frame``, as add specs.

        The template is the cell the stamp itself would lay
        (:meth:`~...stamp_tool.StampToolMixin._stamp_cell` over a blank): an
        eyedropped record whole, or a sheet pick's index with the property
        row's settings. A brush swept off the tile source sheet lays **one
        small piece per square**, stepped by the tile size.
        """
        held = self._held_tile_id()
        if held is None:
            return None
        brush = self._stamp_brush
        if brush is not None and len(brush) > 1 and self._source_cell is None:
            tile_w, tile_h = self._sprite_tile_size()
            return [
                (
                    frame,
                    self._stamp_cell(brush.get(bx, by).index, BLANK),
                    x + bx * tile_w,
                    y + by * tile_h,
                )
                for by in range(brush.height)
                for bx in range(brush.width)
            ]
        return [(frame, self._stamp_cell(held, BLANK), x, y)]

    def _add_held_subsprites(self, x: int, y: int, pixel: bool) -> None:
        landing = self._subsprite_landing(x, y, pixel)
        if landing is None:
            self.statusBar().showMessage("No frame here to add a subsprite to.")
            return
        frame, ox, oy = landing
        specs = self._held_specs(frame, ox, oy)
        if not specs:
            self._refuse_stamp()
            return
        before = list(self._picked_records)
        try:
            cells, added = self._add_pieces(specs)
        except _REFUSALS as exc:
            self.statusBar().showMessage(str(exc))
            return
        if self._apply_cells(cells, "add subsprite", pick=(before, added)):
            what = "subsprite" if len(added) == 1 else counted(len(added), "subsprite")
            self.statusBar().showMessage(
                f"Added {what} to frame {frame} at ({ox:+d}, {oy:+d})."
            )

    def _on_subsprite_hovered(self, pixel: object) -> None:
        self._subsprite_hover = pixel  # type: ignore[assignment]
        if self._sprite_tool_live():
            self._sync_subsprite_preview()

    def _sync_subsprite_preview(self) -> None:
        """The ghost of what a click would add, at the snapped landing.

        Rendered by **the landing computation itself** — the add run on a copy
        of the cells and the new pieces read back off the frames it builds —
        so the ghost shows the size the codec resolves, the row the stamp
        settles and the slot a full frame refuses, and cannot differ from the
        press. None with nothing held, over a piece (a press there lifts it,
        it does not add), or while a drag is carrying the pick.
        """
        canvas = self._canvas
        doc = self._doc
        hover = self._subsprite_hover
        if (
            not self._sprite_tool_live()
            or hover is None
            or self._subsprite_drag is not None
        ):
            canvas.set_stamp_preview(None)
            return
        ctrl = bool(QApplication.keyboardModifiers() & _CTRL)
        landing = self._subsprite_landing(*hover, ctrl)
        specs = None if landing is None else self._held_specs(*landing)
        if not specs or pipeline.sprite_hit(
            doc, self._registry, self._tilemap_columns(), *hover
        ):
            canvas.set_stamp_preview(None)
            return
        key = (landing, tuple(specs), id(doc), id(doc.cells))
        cached_key, shown = self._subsprite_ghost
        if cached_key != key:
            shown = self._render_ghost(specs)
            self._subsprite_ghost = (key, shown)
        if shown is None:
            canvas.set_stamp_preview(None)
            return
        image, (bx, by) = shown
        ox, oy = self._slot_origin(self._sprite_sheet(), landing[0])
        canvas.set_stamp_preview(image, at=(ox + bx, oy + by))

    def _render_ghost(
        self, specs: Sequence[tuple[int, Cell, int, int]]
    ) -> tuple[QImage, tuple[int, int]] | None:
        try:
            cells, added = self._add_pieces(specs)
            frames = self._frame_cells(cells)
        except _REFUSALS:
            return None
        positions = record_positions(frames)
        pieces = [
            frames[place[0]][place[1]]
            for record in added
            if (place := positions.get(record)) is not None
        ]
        return self._render_pieces(pieces)

    # -- the keys ------------------------------------------------------------
    def _end_nudge_run(self) -> None:
        """Close the current nudge run: the next arrow starts a new undo step."""
        self._nudge_run += 1

    def _subsprite_key(
        self, key, shift: bool, ctrl: bool, *, alt: bool = False
    ) -> bool:  # noqa: ANN001
        """The tool's keys while armed on a sprite; True if consumed.

        Routed from the nav event filter ahead of the transform letters and
        the navigation map — and ahead of its Alt refusal, since Alt+Up/Down
        are the reorder rows' keys here. The arrows nudge only with a pick, so
        with nothing picked they still navigate. Any other key ends a nudge
        run.
        """
        if not self._sprite_tool_live():
            return False
        if key == Qt.Key.Key_Escape and not (shift or ctrl or alt):
            self._end_nudge_run()
            if self._abort_subsprite_drag():
                self._canvas.cancel_drag()
                self._announce_subsprite()
                return True
            return False
        if alt and not ctrl and key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._end_nudge_run()
            self._sync_subsprite_actions()
            up = key == Qt.Key.Key_Up
            if shift:
                action = (
                    self._subsprite_front_action if up else self._subsprite_back_action
                )
            else:
                action = (
                    self._subsprite_forward_action
                    if up
                    else self._subsprite_backward_action
                )
            if action.isEnabled():
                action.trigger()
            else:
                self.statusBar().showMessage(_NOTHING_PICKED)
            return True
        if ctrl and not (shift or alt) and key == Qt.Key.Key_D:
            self._end_nudge_run()
            self._duplicate_subsprites()
            return True
        step = _NUDGE_KEYS.get(key)
        # The tile source sheet keeps its own arrows while it has focus - its
        # pick on the bare ones, its Cols on Shift+Left/Right (_adjust_columns).
        sheet_focused = QApplication.focusWidget() is self._tile_source_panel
        if (
            step is None
            or alt
            or ctrl
            or sheet_focused
            or not self._picked_subsprites()
        ):
            self._end_nudge_run()
            return False
        tile_w, tile_h = self._sprite_tile_size() if shift else (1, 1)
        self._nudge_subsprites(step[0] * tile_w, step[1] * tile_h)
        return True

    def _nudge_subsprites(self, dx: int, dy: int) -> None:
        """Move the pick by ``(dx, dy)`` as one step of the current run.

        Refused **whole** where any piece would leave the range — a nudge is
        one exact step, and a shrunk one would land somewhere the key never
        said (the drag clamps instead, being a gesture toward a point).
        """
        drawn = self._picked_subsprites()
        ops = self._sprite_ops()
        if not drawn or ops is None:
            return
        limits, place, _assign = ops
        pieces = [sub for _, sub in drawn]
        if fit_group_delta(pieces, dx, dy, limits) != (dx, dy):
            reach = limits.x_range if dx else limits.y_range
            self.statusBar().showMessage(f"{_range_text(reach)} - stopped at the edge")
            return
        cells = list(self._doc.cells)
        try:
            for record, sub in drawn:
                cells[record] = place(cells[record], x=sub.x + dx, y=sub.y + dy)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc))
            return
        picked = list(self._picked_records)
        if self._apply_cells(
            cells, "nudge subsprites", run=self._nudge_run, pick=(picked, picked)
        ):
            self.statusBar().showMessage(
                f"Nudged {counted(len(drawn), 'subsprite')} by ({dx:+d}, {dy:+d})"
            )

    # -- the record ops ------------------------------------------------------
    def _picked_or_refuse(self) -> list[tuple[int, Subsprite]] | None:
        drawn = self._picked_subsprites()
        if not drawn:
            self.statusBar().showMessage(_NOTHING_PICKED)
            return None
        return drawn

    def _copy_subsprites(self) -> bool:
        """The picked records into the cell buffer, their offsets beside it.

        A one-row :class:`CellGrid`, so the cell buffer's readers — Paste's
        enabled state among them — work unchanged; the offsets ride in
        :attr:`_subsprite_clip`, tied to that very buffer.
        """
        drawn = self._picked_or_refuse()
        if drawn is None:
            return False
        doc = self._doc
        buffer = CellGrid.from_cells(
            len(drawn), 1, [doc.cells[record] for record, _ in drawn]
        )
        self._cell_clipboard = buffer
        self._subsprite_clip = (buffer, tuple((sub.x, sub.y) for _, sub in drawn))
        self._sync_edit_actions()
        self.statusBar().showMessage(f"Copied {counted(len(drawn), 'subsprite')}.")
        return True

    def _cut_subsprites(self) -> None:
        if self._copy_subsprites():
            self._clear_subsprites("cut subsprites")

    def _clear_subsprites(self, text: str = "clear subsprites") -> None:
        """Remove the picked pieces, on the format's terms (§8.4).

        Under ``slots`` that is the drawn bit off and the pick **stays** on
        the records, so the property row's Drawn box can put them back; under
        the inserting kinds the cells go and every later record renumbers, so
        the pick goes with them.
        """
        drawn = self._picked_or_refuse()
        ops = self._sprite_ops() if drawn is not None else None
        if drawn is None or ops is None:
            return
        limits, place, assign = ops
        doc = self._doc
        records = [record for record, _ in drawn]
        before = list(self._picked_records)
        try:
            cells = remove_subsprites(
                list(doc.cells),
                doc.sprite_frames or [],
                limits,
                records,
                place=place,
                assign=assign,
            )
        except _REFUSALS as exc:
            self.statusBar().showMessage(str(exc))
            return
        slots = limits.structure == "slots"
        if self._apply_cells(cells, text, pick=(before, before if slots else [])):
            verb = "Cut" if text.startswith("cut") else "Removed"
            self.statusBar().showMessage(
                f"{verb} {counted(len(records), 'subsprite')}."
            )

    def _paste_subsprites(self) -> None:
        """Add the buffered records, at their copied offsets, as new pieces.

        Into the primary picked piece's frame — the selection's frame, as a
        grid paste lands at the selection — else the frame under the pointer,
        else frame 0. A buffer some other copy filled carries no offsets, so
        its records are laid along a row from the origin.
        """
        copied = self._cell_clipboard
        doc = self._doc
        if copied is None or not len(copied) or doc is None:
            return
        records = [
            copied.get(x, y) for y in range(copied.height) for x in range(copied.width)
        ]
        clip = self._subsprite_clip
        if clip is not None and clip[0] is copied:
            offsets = list(clip[1])
        else:
            tile_w, _ = self._sprite_tile_size()
            offsets = [(at * tile_w, 0) for at in range(len(records))]
        frame = self._paste_frame()
        specs = [
            (frame, cell, x, y) for cell, (x, y) in zip(records, offsets, strict=True)
        ]
        before = list(self._picked_records)
        try:
            cells, added = self._add_pieces(specs)
        except _REFUSALS as exc:
            self.statusBar().showMessage(str(exc))
            return
        if self._apply_cells(cells, "paste subsprites", pick=(before, added)):
            self.statusBar().showMessage(
                f"Pasted {counted(len(added), 'subsprite')} into frame {frame}."
            )

    def _paste_frame(self) -> int:
        pick = self._picked_subsprite
        if pick is not None:
            return pick[0]
        sheet, hover = self._sprite_sheet(), self._subsprite_hover
        if sheet is not None and hover is not None:
            at = self._frame_slot_at(sheet, *hover)
            if at is not None:
                return at
        return 0

    def _duplicate_subsprites(self) -> None:
        """Ctrl+D: the pick copied one tile along each way, the copies picked.

        The offset is shrunk as a group where the pick sits at the range's
        edge, the drag's rule, so a duplicate is never refused for where the
        originals are — at worst the copies land on them.
        """
        if not self._sprite_tool_live():
            return
        drawn = self._picked_or_refuse()
        limits = self._subsprite_limits()
        if drawn is None or limits is None:
            return
        tile_w, tile_h = self._sprite_tile_size()
        dx, dy = fit_group_delta([sub for _, sub in drawn], tile_w, tile_h, limits)
        positions = self._record_positions()
        doc = self._doc
        specs = [
            (positions[record][0], doc.cells[record], sub.x + dx, sub.y + dy)
            for record, sub in drawn
        ]
        before = list(self._picked_records)
        try:
            cells, added = self._add_pieces(specs)
        except _REFUSALS as exc:
            self.statusBar().showMessage(str(exc))
            return
        if self._apply_cells(cells, "duplicate subsprites", pick=(before, added)):
            self.statusBar().showMessage(
                f"Duplicated {counted(len(added), 'subsprite')} at ({dx:+d}, {dy:+d})."
            )

    def _reorder_subsprite(self, where: str) -> None:
        """Move the primary picked piece forward, back, to the front or back.

        Position 0 is the front — draw order is file order — so *forward* is a
        step toward 0. The pick follows the piece to its new record, which
        :func:`~celpix.core.sprite_edit.reorder_subsprite` makes the ``to``-th
        lowest of the frame's records; the rest of a several-piece pick is
        dropped, since the permutation renumbers their records too.
        """
        if not self._sprite_tool_live():
            return
        drawn = self._picked_or_refuse()
        ops = self._sprite_ops() if drawn is not None else None
        if drawn is None or ops is None:
            return
        limits, _place, assign = ops
        doc = self._doc
        primary = self._picked_records[-1]
        frames = doc.sprite_frames or []
        place = record_positions(frames).get(primary)
        if place is None:
            self.statusBar().showMessage(_NOTHING_PICKED)
            return
        number, position = place
        order = sorted(sub.record for sub in frames[number] if sub.record is not None)
        to = {
            "forward": position - 1,
            "backward": position + 1,
            "front": 0,
            "back": len(order) - 1,
        }[where]
        to = max(0, min(to, len(order) - 1))
        if to == position:
            end = "front" if to == 0 else "back"
            self.statusBar().showMessage(
                f"Subsprite {position} is already at the {end} of frame {number}."
            )
            return
        before = list(self._picked_records)
        try:
            cells = reorder_subsprite(
                list(doc.cells), frames, limits, primary, to, assign=assign
            )
        except _REFUSALS as exc:
            self.statusBar().showMessage(str(exc))
            return
        if self._apply_cells(cells, "reorder subsprites", pick=(before, [order[to]])):
            self.statusBar().showMessage(
                f"Subsprite {position} of frame {number} is now {to} of {len(order)}"
                f"{' (front)' if to == 0 else ''}."
            )

    def _mirror_about_origin(self, *, horizontal: bool) -> None:
        """Turn the picked pieces around about the frame's origin.

        Each piece's flip bit toggled through the codec and its position
        reflected as one picture, ``x' = -x - w`` with ``w`` the piece's own
        width (:func:`~celpix.core.sprite_edit.reflect_group` with pivot 0) —
        a different pivot from the Block flip's, which mirrors the pick about
        its own box, and so a different action.
        """
        if not self._sprite_tool_live():
            return
        drawn = self._picked_or_refuse()
        ops = self._sprite_ops() if drawn is not None else None
        found = self._tilemap_engine()
        if drawn is None or ops is None or found is None:
            return
        limits, place, _assign = ops
        engine, preset = found
        op = CellOp.FLIP_H if horizontal else CellOp.FLIP_V
        flip = getattr(engine, "transform_cell", None)
        if flip is None or flip(Cell(), op, preset.params) is None:
            self.statusBar().showMessage(
                f"This format's pieces have no {op.label} bit."
            )
            return
        tile_w, tile_h = self._sprite_tile_size()
        moved = reflect_group(
            [sub for _, sub in drawn], tile_w, tile_h, horizontal=horizontal, pivot=0
        )
        reach = limits.x_range if horizontal else limits.y_range
        axis = 0 if horizontal else 1
        if any(spot[axis] not in reach for spot in moved):
            self.statusBar().showMessage(
                f"{_range_text(reach)} - a mirrored piece would leave it"
            )
            return
        cells = list(self._doc.cells)
        try:
            for (record, _sub), (x, y) in zip(drawn, moved, strict=True):
                cells[record] = place(flip(cells[record], op, preset.params), x=x, y=y)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc))
            return
        picked = list(self._picked_records)
        if self._apply_cells(cells, "mirror subsprites", pick=(picked, picked)):
            rule = "x' = -x - w" if horizontal else "y' = -y - h"
            way = "horizontally" if horizontal else "vertically"
            self.statusBar().showMessage(
                f"Mirrored {counted(len(drawn), 'subsprite')} {way} about the "
                f"origin ({rule})."
            )


def _range_text(reach: range) -> str:
    return f"Offsets here run {reach.start}..{reach.stop - 1}"
