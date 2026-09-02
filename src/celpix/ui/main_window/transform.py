"""The canvas transform toolbar: flip and rotate, per tile or per block.

Geometric transforms are **destructive, undoable edits** — they rewrite the
interpreted pixels and round-trip through the active codec, unlike the byte-nudge
(``−B / +B / 0B``), which only realigns where tiles start and touches no data.
The realignment need the reference tools cover with a 1px *shift* is already the
byte-nudge's job, so only flip/rotate live here.

The bar splits **Tile** from **Block**, because "flip the selection" means two
different things:

- **Tile** — transform each selected tile **in place**; tile positions never
  change. Works on any selection (linear or rectangle, one tile or many). This is
  the "mirror every tile" operation.
- **Block** — transform the selected **rectangle as one picture**: flip/rotate
  each tile *and* permute the tiles' positions within the block. Needs a 2D block:
  a rectangle selection, or a **single** selected tile — in any selection shape —
  which expands to the arrangement block (Block W×H) it sits in, so one click turns
  a whole 16×16 unit. Only a linear *multi*-tile run has no block.

Which groups are on the bar follows the current interaction
(:meth:`_sync_transform_bar_mode`): Tile + Block while editing tiles, a dedicated
**Pixel** group in pixel mode, and — while the rearrange tool is armed — a
display-only Tile/Block pair in their place. Those last ones are *not* edits: they
transform by storing an orientation in the rearrangement rather than by rewriting
pixels, so both halves of a block transform (the tiles' orientation and their
position permutation) live in the rearrangement and the file is untouched. They
keep the
Tile/Block captions rather than announcing themselves, since the split means
exactly what it does elsewhere; the tooltips carry the difference. One
:class:`TransformOp` table drives both paths — ``tile_orient`` is each op expressed
as display-orientation bits, so the destructive and the display route cannot
disagree about what "rotate right" means.

**H / V / C / X** press the flip and rotate buttons from the keyboard, and
**Shift** picks the Block group — so four letters cover whichever pair is on the
bar, and a key never means one thing in tile mode and another in pixel mode
(:meth:`_transform_key`). The letters and the button order come from
:data:`~celpix.ui.tools.TRANSFORM_SPECS`, which the F1 guide reads too. While
**Edit Tiles** is armed the Tile and Block buttons keep their meanings but act
on the **held stamp** — the tool cleared the selection they otherwise read —
so the letters follow them there for free (``stamp_tool.py``).

Each button decodes the selection's enclosing run, transforms it, and re-encodes
through :meth:`~celpix.ui.main_window.tile_bytes.TileBytesMixin._apply_tile_edit`,
which pushes one :class:`~celpix.ui.undo_commands.PixelEditCommand` — so a
transform is a single Ctrl+Z step, exactly like a paste.

Rotation swaps a tile's width and height, so it needs **square tiles** in every
group, the rearrange ones included; a block group additionally needs a **square
block** (``cols == rows``), since a non-square block would swap the block's own
dimensions. Flips have no such constraint.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QIcon, QPalette
from PySide6.QtWidgets import QLabel, QSizePolicy, QToolBar, QWidget

from celpix.core import transform
from celpix.core.tilemap import CellOp
from celpix.core.tilerearrangement import (
    TILE_FLIP_H,
    TILE_FLIP_V,
    TILE_ROTATE_CCW,
    TILE_ROTATE_CW,
)
from celpix.ui.icon_font import glyph_pixmap
from celpix.ui.main_window.capability_sync import Gesture
from celpix.ui.main_window.selection import SELECTION_SHAPE_KEY, SelectionShape
from celpix.ui.tools import TRANSFORM_SPECS, EditMode, TransformSpec
from celpix.ui.widgets import (
    CompactComboBox,
    add_labelled,
    counted,
    load_enum_setting,
    select_combo_data,
)


@dataclass(frozen=True)
class TransformOp:
    """One transform direction: how it moves pixels and, for a block, cells.

    ``pixel_fn`` transforms a single decoded tile (from :mod:`celpix.core.transform`).
    ``cell_src`` answers, for a destination cell ``(dx, dy)`` in a ``cols×rows``
    block, which source cell's (transformed) tile lands there — the block-level
    half of the permutation, unused by the in-place tile group. ``verb``/``past``
    feed the undo label and the status line.

    ``tile_orient`` is the same operation expressed as **display-orientation**
    bits (:mod:`celpix.core.tilerearrangement`), for the rearrange tool: it
    transforms a tile
    by storing an orientation rather than by rewriting pixels, so the two paths
    share this one table instead of a parallel mapping that could drift from it.

    ``cell_op`` is the third expression of it, for a tilemap: the *name* of the
    operation, handed to the format's codec to answer in whatever bits it has —
    or to refuse, since which transforms a cell supports is a property of the
    format (:class:`~celpix.core.tilemap.CellOp`).
    """

    verb: str
    past: str
    pixel_fn: Callable[[object], object]
    cell_src: Callable[[int, int, int, int], tuple[int, int]]
    tile_orient: int
    cell_op: CellOp


# The four directions. The cell maps invert the pixel transform at tile
# granularity: a horizontal flip reverses the column axis, a CW rotation
# transposes (block rotation is only ever applied to a square block, cols == rows).
OP_FLIP_H = TransformOp(
    "flip",
    "Flipped",
    transform.flip_horizontal,
    lambda dx, dy, cols, rows: (cols - 1 - dx, dy),
    TILE_FLIP_H,
    CellOp.FLIP_H,
)
OP_FLIP_V = TransformOp(
    "flip",
    "Flipped",
    transform.flip_vertical,
    lambda dx, dy, cols, rows: (dx, rows - 1 - dy),
    TILE_FLIP_V,
    CellOp.FLIP_V,
)
OP_ROTATE_CCW = TransformOp(
    "rotate",
    "Rotated",
    transform.rotate_ccw,
    lambda dx, dy, cols, rows: (cols - 1 - dy, dx),
    TILE_ROTATE_CCW,
    CellOp.ROTATE_CCW,
)
OP_ROTATE_CW = TransformOp(
    "rotate",
    "Rotated",
    transform.rotate_cw,
    lambda dx, dy, cols, rows: (dy, rows - 1 - dx),
    TILE_ROTATE_CW,
    CellOp.ROTATE_CW,
)

# Which op each button in :data:`~celpix.ui.tools.TRANSFORM_SPECS` performs. The
# specs carry the presentation (icon, key, name) and this the behaviour, so the
# toolbar, the keys and the shortcut guide all read one button order.
OP_BY_FIELD: dict[str, TransformOp] = {
    "flip_h": OP_FLIP_H,
    "flip_v": OP_FLIP_V,
    "rotate_cw": OP_ROTATE_CW,
    "rotate_ccw": OP_ROTATE_CCW,
}


def _qt_key(spec: TransformSpec) -> Qt.Key:
    """The letter a spec advertises, as the key the event filter compares against.

    The specs carry the letter as text because that is what the tooltips and the
    F1 guide print; this is the one place it is turned back into a ``Qt.Key``.
    """
    return getattr(Qt.Key, f"Key_{spec.key}")


# Appended to every tooltip in the rearrange tool's groups. Its buttons carry the
# same icons as the destructive ones, so the one thing a tooltip there has to say
# is that nothing is being rewritten. Its own line, within the ~60-column wrap a
# plain-text tooltip needs (docs/py-qt-reference/pyside6-pitfalls.md).
DISPLAY_ONLY_TIP = "\nDisplay only; the file is not changed"

# The transform buttons' icon box, in logical pixels. Sized to the text arrows it
# replaced rather than to a toolbar's default, so the bar keeps the height it had
# over the canvas.
_TRANSFORM_ICON = 16

# The transform keys as Qt keys, in button order. Bare letters, so they are routed
# by the app-wide event filter (:meth:`TransformMixin._transform_key`) rather than
# registered: a live shortcut on a letter would steal it from a focused text input.
KEY_FIELDS: dict[Qt.Key, str] = {_qt_key(spec): spec.field for spec in TRANSFORM_SPECS}


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

    @property
    def actions(self) -> tuple[QAction, QAction, QAction, QAction]:
        return (self.flip_h, self.flip_v, self.rotate_cw, self.rotate_ccw)

    @property
    def by_field(self) -> dict[str, QAction]:
        """The four actions under the field names :data:`OP_BY_FIELD` uses, so a
        per-operation rule can reach the button that performs it."""
        return {
            "flip_h": self.flip_h,
            "flip_v": self.flip_v,
            "rotate_cw": self.rotate_cw,
            "rotate_ccw": self.rotate_ccw,
        }


class TransformMixin:
    """Flip/rotate the selection — per tile or per block — from a canvas toolbar.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`; it reads the
    window's live selection state and its single ``_doc``, and reuses the
    selection mixin's decode/encode helpers. See the module docstring for the
    tile-vs-block semantics.
    """

    def _build_transform_toolbar(self) -> QToolBar:
        """The canvas-top transform bar (a plain widget, not ``addToolBar``).

        The last of the four bars stacked over the canvas, directly on top of it
        and below the Codecs/Arrangement/View rows, so it reads as belonging to
        the editing surface. It carries three labelled groups but shows only the
        ones for the current edit mode: Tile + Block in tile editing, Pixel in
        pixel editing (:meth:`_sync_transform_bar_mode`). Each starts disabled;
        :meth:`_sync_transform_actions` (driven from the selection convergence)
        turns buttons on for what the selection supports.
        """
        bar = QToolBar("Transform")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        # The bar's own icon size, not the platform's: this is the fourth row
        # stacked over the canvas, and a toolbar left to its default (24px, and
        # larger again on some desktops) would take a visible bite out of the
        # editing surface for four arrows.
        bar.setIconSize(QSize(_TRANSFORM_ICON, _TRANSFORM_ICON))
        # Every group built below registers here, so a theme switch re-bakes all
        # five without this list having to be spelled out twice.
        self._transform_groups: list[_TransformGroup] = []
        self._build_selection_shape_combo(bar)
        bar.addSeparator()
        # Tile + Block: the tile-mode transforms. Each group's label and leading
        # separator are captured alongside its buttons so the whole group hides as
        # a unit when pixel mode swaps in the Pixel group.
        tile_label = self._group_caption(
            bar, " Tile: ", "Flip / rotate each tile in place"
        )
        self._tile_group = self._add_transform_group(
            bar, self._transform_tiles, "each tile in place", ""
        )
        block_sep = bar.addSeparator()
        block_label = self._group_caption(
            bar, " Block: ", "Flip / rotate the block, tiles and positions"
        )
        self._block_group = self._add_transform_group(
            bar, self._transform_block, "the block, tiles and positions", "Shift+"
        )
        # Pixel: shown only in pixel mode, flips/rotates the pixel selection (or the
        # whole window when nothing is lifted) rather than tiles.
        pixel_label = self._group_caption(
            bar, " Pixel: ", "Flip / rotate the pixel selection"
        )
        self._pixel_group = self._add_transform_group(
            bar,
            self._transform_pixel_region,
            "the pixel selection",
            "",
        )
        # Rearrange: shown only while that tool is armed, over *either* mode. Its
        # transforms are display state stored in the rearrangement, not edits —
        # the same icons and the same Tile/Block captions, deliberately, because
        # the gesture and the split read the same; the tooltips carry the
        # difference.
        rearrange_label = self._group_caption(
            bar,
            " Tile: ",
            "Flip / rotate each tile in place\nDisplay only; the file is not changed",
        )
        self._rearrange_group = self._add_transform_group(
            bar,
            self._orient_rearranged_tiles,
            "each tile in place",
            "",
            DISPLAY_ONLY_TIP,
        )
        rearrange_block_sep = bar.addSeparator()
        rearrange_block_label = self._group_caption(
            bar,
            " Block: ",
            "Flip / rotate the block, tiles and positions\n"
            "Display only; the file is not changed",
        )
        self._rearrange_block_group = self._add_transform_group(
            bar,
            self._orient_rearranged_block,
            "the block, tiles and positions",
            "Shift+",
            DISPLAY_ONLY_TIP,
        )
        self._tile_mode_bar_actions = [
            tile_label,
            *self._tile_group.actions,
            block_sep,
            block_label,
            *self._block_group.actions,
        ]
        # Palette row: the pin gestures the Palette and canvas menus also hold,
        # as buttons. Added after every mode group so it lands to the right of
        # whichever one is showing (a hidden group takes no width), and left
        # visible in all three: pinning reads a tile selection, which pixel mode
        # and the rearrange tool both still have. One enabled state for all three
        # homes - _sync_pin_actions.
        bar.addSeparator()
        self._group_caption(
            bar,
            " Palette Row: ",
            "Pin the selected tiles to the Palette Row on screen",
        )
        bar.addAction(self._pin_palette_action)
        bar.addAction(self._unpin_palette_action)

        self._pixel_mode_bar_actions = [pixel_label, *self._pixel_group.actions]
        self._rearrange_bar_actions = [
            rearrange_label,
            *self._rearrange_group.actions,
            rearrange_block_sep,
            rearrange_block_label,
            *self._rearrange_block_group.actions,
        ]
        self._build_edit_mode_toggle(bar)
        self._sync_transform_bar_mode()
        # Nothing is open yet, and the bar acts on a document; _set_document_ui_enabled
        # arms it when one is shown.
        bar.setEnabled(False)
        return bar

    def _sync_transform_bar_mode(self) -> None:
        """Show the transform groups that match the current interaction.

        Tile + Block for tile editing, Pixel for pixel editing — and while the
        rearrange tool is armed, its own group instead of either, since its flips
        do something different from the destructive ones (display state, not
        pixels)."""
        rearranging = self._rearranging
        pixel = self._edit_mode is EditMode.PIXEL
        for action in self._tile_mode_bar_actions:
            action.setVisible(not pixel and not rearranging)
        for action in self._pixel_mode_bar_actions:
            action.setVisible(pixel and not rearranging)
        for action in self._rearrange_bar_actions:
            action.setVisible(rearranging)

    def _build_edit_mode_toggle(self, bar: QToolBar) -> None:
        """The whole-surface switches, pinned to the toolbar's right edge.

        An expanding spacer pushes them hard right, away from the transform
        groups, so they read as switches for the editing surface rather than more
        transform buttons. The rearrange tool's button leads, Edit Tiles sits
        beside it, and Pixel Mode ends the bar — exclusive modes over the same
        canvas, so arming one leaves whichever was armed (``rearrange.py``). Only
        the tool's *button* is here: its other half is an Edit menu row, and the
        view toggle is menu-only. The rearrange tool is the one that changes what
        the transform groups beside it mean.
        """
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        self._build_rearrange_actions(bar)
        # Beside the rearrange pair rather than beyond the separator: it is the
        # fourth of the exclusive modes over this canvas, and the one a tilemap
        # actually has — where those two are hidden or greyed, this is the tool
        # in that place (``stamp_tool.py``).
        self._build_stamp_actions(bar)
        bar.addSeparator()
        self._edit_mode_action = QAction("Pixel Mode", self)
        self._edit_mode_action.setCheckable(True)
        self._edit_mode_action.setChecked(self._edit_mode is EditMode.PIXEL)
        self._edit_mode_action.setToolTip("Draw pixels instead of selecting tiles (E)")
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
        self._selection_shape = CompactComboBox(100)
        # Which shape is currently being imposed rather than chosen, or None where
        # the preference rules - what ``SelectionMixin._sync_selection_shape``
        # watches for a crossing.
        self._selection_shape_forced = None
        for shape, label in (
            (SelectionShape.LINEAR, "Linear"),
            (SelectionShape.RECT, "Rectangle"),
        ):
            self._selection_shape.addItem(label, shape)
        self._selection_shape.setToolTip(
            "What a canvas drag selects (S swaps):\n"
            "• Linear - the run of tiles in storage order\n"
            "• Rectangle - the block of tiles on screen"
        )
        select_combo_data(
            self._selection_shape,
            load_enum_setting(SELECTION_SHAPE_KEY, SelectionShape.LINEAR),
        )
        self._selection_shape.currentIndexChanged.connect(
            self._on_selection_shape_change
        )
        add_labelled(
            bar, "Selection: ", self._selection_shape, self._selection_shape.toolTip()
        )

    @staticmethod
    def _group_caption(bar: QToolBar, text: str, tip: str):
        """A transform group's caption, tooltipped with what the group acts on.

        The caption is as likely a hover target as the icon buttons beside it,
        and on its own " Tile: " says nothing about the distinction from Block -
        so it answers the same question its buttons' tooltips do.
        """
        label = QLabel(text)
        label.setToolTip(tip)
        return bar.addWidget(label)

    def _add_transform_group(
        self,
        bar: QToolBar,
        handler: Callable[[TransformOp], None],
        scope: str,
        modifier: str,
        suffix: str = "",
    ) -> _TransformGroup:
        """Build one flip/rotate group on ``bar``, wired to ``handler``.

        ``scope`` completes each tooltip so the groups' otherwise-identical glyphs
        read unambiguously, ``suffix`` adds the line the rearrange groups need to
        disown the destructive reading, and ``modifier`` is the key prefix this
        group answers to (Shift for Block) — named in the tooltip, not registered;
        see :data:`KEY_FIELDS`.
        """
        actions = {}
        for spec in TRANSFORM_SPECS:  # the table is the left-to-right order
            action = QAction(self)
            # The label is set as well as the tooltip, even though the bar draws
            # icons only: it is what a screen reader announces, and what Qt puts
            # in the toolbar's own overflow menu when the bar is too narrow.
            action.setText(spec.label)
            action.setToolTip(f"{spec.label} ({modifier}{spec.key}) — {scope}{suffix}")
            action.setEnabled(False)
            op = OP_BY_FIELD[spec.field]
            action.triggered.connect(lambda _=False, op=op: handler(op))
            bar.addAction(action)
            actions[spec.field] = action
        group = _TransformGroup(**actions)
        # Every group's icons are baked pixmaps in the palette's colour, so they
        # are re-baked together on a theme or DPI change; registering the group
        # here is what keeps that list from drifting as groups are added.
        self._transform_groups.append(group)
        self._bake_transform_group(group)
        return group

    def _bake_transform_group(self, group: _TransformGroup) -> None:
        """Paint one group's four icons in the theme's button-text colour.

        Disabled is left to Qt: unlike the tools rail, these buttons spend most
        of their life *enabled* and the automatic fade is only asked to mark a
        transform the current selection can't take — a passing state, not the
        resting one.
        """
        color = self.palette().color(QPalette.ColorRole.ButtonText)
        ratio = self.devicePixelRatioF()
        box = QSize(_TRANSFORM_ICON, _TRANSFORM_ICON)
        for spec, action in zip(TRANSFORM_SPECS, group.actions, strict=True):
            action.setIcon(QIcon(glyph_pixmap(spec.icon, color, box, ratio)))

    def _bake_transform_icons(self) -> None:
        """Re-bake every transform group's icons — see ``_rebake_icons``."""
        for group in self._transform_groups:
            self._bake_transform_group(group)

    # -- the keys ----------------------------------------------------------
    def _transform_key(self, key, shift: bool, ctrl: bool) -> bool:  # noqa: ANN001
        """Press the transform button ``key`` names; True if it was consumed.

        Routed from the nav event filter. The bar already swaps groups with the
        interaction, so one key table serves all of them: a letter always means
        "what the visible group's button does" and Shift always means the Block
        half of whichever pair is showing — never a different operation in a
        different mode.

        A disabled button still swallows the key: the selection simply doesn't
        support that transform — non-square tiles have no rotation, whichever
        group is showing — and letting the letter fall through to something else
        would be a surprise. Edit Tiles needs no branch here: while it is armed
        the Tile/Block buttons themselves act on the held stamp
        (:meth:`~...stamp_tool.StampToolMixin._stamp_transform_tiles`), so the
        letters press them exactly as they do over a selection.
        """
        if ctrl or self._doc is None:
            return False
        action = self._transform_key_actions(shift).get(key)
        if action is None:
            return False
        if action.isEnabled():
            action.trigger()
        return True

    def _transform_key_actions(self, shift: bool) -> dict:
        """The keyed buttons of the group currently on the bar, by Qt key."""
        if self._rearranging:
            group = self._rearrange_block_group if shift else self._rearrange_group
        elif self._edit_mode is EditMode.PIXEL:
            if shift:
                return {}  # one group, so there is no Block half to shift into
            group = self._pixel_group
        else:
            group = self._block_group if shift else self._tile_group
        return {key: getattr(group, field) for key, field in KEY_FIELDS.items()}

    def _sync_transform_actions(self) -> None:
        """Enable each group for what the current selection supports.

        Tile transforms take any selection (rotation needs square tiles); block
        transforms need a 2D block, which :meth:`_block_geometry` resolves (a
        rectangle selection, or a single tile's arrangement block in any shape;
        rotation additionally needs that block square). Called from the selection
        convergence *and* from the render cycle, since on a tilemap the answer
        also depends on the document and its format, which a codec change moves
        without touching the selection.
        """
        if self._edit_mode is EditMode.PIXEL:
            self._sync_pixel_transform_actions()
            return
        if self._stamping:
            # Edit Tiles cleared the selection this pass reads, so while it is
            # armed the groups arm for the held stamp instead — the same
            # retargeting the property row makes
            # (:meth:`~...stamp_tool.StampToolMixin._sync_stamp_transform_actions`).
            self._sync_stamp_transform_actions()
            return
        has = self._doc is not None and self._selected_tile is not None
        # Squareness is about the *tile*; whether the cells can be turned at all
        # is about the *format*, which ``allows`` asks per operation — a
        # tilemap's tiles may be perfectly square and its cells still carry
        # mirror bits and no transpose bit, so a rotate stays disabled there.
        square_tiles = has and self._square_tiles()
        allows = self._transform_allowed
        self._arm_transform_group(self._tile_group, has, square_tiles, allows)
        geom = self._block_geometry()
        square_block = geom is not None and geom[0] == geom[1]
        self._arm_transform_group(
            self._block_group, geom is not None, square_block and square_tiles, allows
        )

    def _transform_allowed(self, op: TransformOp) -> bool:
        """Whether this document can perform ``op`` at all, format included.

        The third gate, and the one only a tilemap needs. ``ContentKind`` says a
        tilemap flips; the **format** says whether its cells have a bit that
        means it, and that varies inside the kind — a console BG map mirrors, a
        Game Boy map's cell is a bare index (``docs/design/tilemap-entry.md`` §4).
        Asking the codec here rather than only on the click is what puts that
        answer where every other unavailable control's already is: a disabled
        button, not a message after the fact. The probe is a blank cell through
        :meth:`~...tilemap_edit.TilemapEditMixin._cell_transform`, so it costs
        nothing and touches no data.

        A view-only map answers False to everything for the separate reason that
        its cells are not what is on screen
        (:attr:`~celpix.core.document.Document.cells_editable`) — the same rule
        the clipboard actions follow.
        """
        doc = self._doc
        if doc is None or not doc.is_tilemap:
            return True
        return doc.cells_editable and self._cell_transform(op) is not None

    @staticmethod
    def _arm_transform_group(group, has: bool, square: bool, allows=None) -> None:  # noqa: ANN001
        """Enable one transform group: flips need a target, rotates a square one.

        A quarter turn swaps width and height, so it is only offered where the
        result still fits its footprint. Shared by every group — destructive,
        block and rearrange — so the three cannot disagree about that rule.

        ``allows`` adds a per-operation veto on top, for the groups whose
        document answers one direction differently from another
        (:meth:`_transform_allowed`). Omitted where the four buttons stand or
        fall together, which is every pixel-side group.
        """
        for field, action in group.by_field.items():
            enabled = has and (square or field.startswith("flip"))
            if enabled and allows is not None:
                enabled = allows(OP_BY_FIELD[field])
            action.setEnabled(enabled)

    def _square_tiles(self) -> bool:
        """Whether this codec's tile can be turned at all.

        A rotation swaps a tile's width and height, so on a tile that isn't square
        it could neither be shown in the cell it came from nor written back to the
        bytes it came from. One rule for every group on the bar, destructive and
        display-only alike (the rearrange tool's, in
        :meth:`~celpix.ui.main_window.rearrange.RearrangeMixin._sync_rearrange_actions`).
        """
        return self._doc is not None and self._doc.tile_width == self._doc.tile_height

    def _block_geometry(self) -> tuple[int, int, int, int] | None:
        """The block a block-transform acts on: ``(cols, rows, x0, y0)`` in canvas
        **slots**, which are tiles.

        Slots and not tilemap cells, and the distinction matters one call further
        on: a tilemap's own rectangle is this divided down by
        :attr:`~celpix.core.document.Document.cell_tiles`
        (:meth:`~...tilemap_edit.TilemapEditMixin._cell_rect`).

        A selection of a **single unit** expands to the block it sits in, snapped
        to the ``bc×br`` grid of slots the layout lays down (see
        :class:`~celpix.core.arrangement.BlockLayout`) — so one click turns a whole
        unit, an arrangement block in the pixel view and a metatile cell on a
        tilemap, in **any** selection shape. A larger Rectangle selection *is*
        the block (its own extent in slots, anchored at its top-left slot). A
        linear multi-unit run has no 2D block, so it returns ``None``.

        The block comes off :meth:`~...selection.SelectionMixin._view_layout`
        rather than off the Block W×H spins, because on a tilemap the block is the
        map's own cell and the spins belong to the pixel view — one click on a
        16x16 cell has to turn that cell, not the 8x8 quadrant under the cursor.
        The unit is the same one the selection snaps to
        (:meth:`~...selection.SelectionMixin._selection_unit`), which is the tile
        on a pixel document — and on a stamped chain it outgrows the layout's
        block, which stays the single cell the render places, so the snapped
        unit wins where it is the larger: one click turns the whole stamp it
        selected.
        """
        if self._doc is None or self._selected_tile is None:
            return None
        layout = self._view_layout()
        cx, cy = layout.slot_to_pos(self._selected_tile - self._offset)
        across, down = self._selection_unit()
        if len(self._selection_tiles()) <= across * down:
            # Match BlockLayout's block sizing (columns clamps block width).
            bc = max(1, min(max(layout.block_columns, across), layout.columns))
            br = max(1, max(layout.block_rows, down))
            return bc, br, (cx // bc) * bc, (cy // br) * br
        if self._rect_size is not None:
            cols, rows = self._rect_size
            return cols, rows, cx, cy
        return None  # a linear multi-tile run has no 2D block

    def _transform_tiles(self, op: TransformOp) -> None:
        """Transform every selected tile in place — positions unchanged.

        Each selected tile passes through the op's pixel transform;
        :meth:`~celpix.ui.main_window.selection.SelectionMixin._map_selected_tiles`
        handles the run bookkeeping (a rectangle's gap tiles ride along unchanged).
        Tile-mode only — the Tile group is hidden in pixel mode, where the Pixel
        group drives :meth:`_transform_pixel_region` instead.
        """
        if self._doc is None:
            return
        if self._stamping:
            # No selection to act on while Edit Tiles is armed: each unit of
            # the held stamp is transformed in place instead.
            self._stamp_transform_tiles(op)
            return
        if self._selected_tile is None:
            return
        if (transform_cells := self._kind_handler(Gesture.TRANSFORM_TILES)) is not None:
            # Same button, different document: a cell's mirror is an attribute
            # bit, not a rewrite of anyone's pixels.
            transform_cells(op)
            return
        moved = len(self._selection_tiles())
        if self._map_selected_tiles(op.pixel_fn, f"{op.verb} tiles"):
            self.statusBar().showMessage(f"{op.past} {counted(moved, 'tile')}.")

    def _transform_block(self, op: TransformOp) -> None:
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
        if self._stamping:
            # The block over a held stamp is the swept brush, mirrored whole —
            # positions permuted and each record transformed.
            self._stamp_transform_block(op)
            return
        if (transform_block := self._kind_handler(Gesture.TRANSFORM_BLOCK)) is not None:
            transform_block(op)
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
                placements.append((dest_tile, layout.pos_to_slot(x0 + sx, y0 + sy)))
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
        """Enable the Pixel group for what the pixel selection supports.

        The Pixel group flips/rotates the lifted selection; a rectangle needs to
        be square to rotate,
        matching the tile-mode rule. The Tile/Block groups are hidden in pixel mode
        (see :meth:`_sync_transform_bar_mode`), so only this group is touched.
        """
        region = self._pixel_transform_source()
        has = self._doc is not None and region is not None
        square = has and region.width() == region.height()
        self._arm_transform_group(self._pixel_group, has, square)
