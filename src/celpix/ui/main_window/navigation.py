"""Where the view sits in the file, and how the user moves it.

The window shows a fixed **window** of tiles rather than scrolling freely, so
"position" is a tile ``_offset`` plus a sub-tile ``_nudge`` - both owned by the
window and clamped by the document. This module is everything that reads or
writes that pair: the navigation bar under the canvas, the step/page/home
actions behind the Navigate menu and the keyboard, and the address machinery
that renders an offset as flat hex or a ``bank:offset`` mapping.

Navigation keys are routed by an **application event filter** rather than
``QShortcut`` so they work wherever focus is - except inside a widget that uses
the arrow keys itself, which :meth:`_handle_nav_key` yields to.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import (
    QAction,
    QPalette,
)
from PySide6.QtWidgets import (
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStyle,
    QTextEdit,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from celpix.core.address import (
    BANK_PRESETS,
    BankLayout,
    BankPreset,
    SplitBankLayout,
    format_hex,
    parse_hex,
)
from celpix.plugins.base import NO_DECOMPRESS
from celpix.ui.palette_panel import PalettePanel
from celpix.ui.undo_commands import (
    OffsetMoveCommand,
)
from celpix.ui.widgets import (
    CommittingLineEdit,
    CompactComboBox,
    add_labelled,
    signals_blocked,
)


class NavigationMixin:
    """The view window's position in the file, and every way of moving it.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _build_navigate_menu(self) -> None:
        """Navigate ▸ the navigation actions - the menu home for every nav key.

        Some of these also have navbar buttons; the rest (first/last page, page
        steps, window resizing) live only here and on the keyboard, so the menu
        doubles as the discoverable list of navigation shortcuts.
        """
        menu = self.menuBar().addMenu("Navigate")
        groups: tuple[tuple[tuple[str, str, Callable[[], None]], ...], ...] = (
            (
                ("First page", "Home", self._nav_home),
                ("Last page", "End", self._nav_end),
            ),
            (
                ("Previous byte", "- / Ctrl+Left", lambda: self._nav_bytes(-1)),
                ("Next byte", "+ / Ctrl+Right", lambda: self._nav_bytes(1)),
                ("Zero byte offset", "0", self._clear_nudge),
                ("Previous tile", "Left", lambda: self._nav_tiles(-1)),
                ("Next tile", "Right", lambda: self._nav_tiles(1)),
                ("Row up", "Up", lambda: self._nav_rows(-1)),
                ("Row down", "Down", lambda: self._nav_rows(1)),
                ("Page up", "PgUp", lambda: self._nav_rows(-self._rows.value())),
                ("Page down", "PgDown", lambda: self._nav_rows(self._rows.value())),
            ),
            (
                (
                    "Fewer columns",
                    "Shift+Left",
                    lambda: self._adjust_spin(self._columns, -1),
                ),
                (
                    "More columns",
                    "Shift+Right",
                    lambda: self._adjust_spin(self._columns, 1),
                ),
                ("Fewer rows", "Shift+Up", lambda: self._adjust_spin(self._rows, -1)),
                ("More rows", "Shift+Down", lambda: self._adjust_spin(self._rows, 1)),
            ),
        )
        for i, group in enumerate(groups):
            if i:
                menu.addSeparator()
            for text, key, handler in group:
                # The key text goes in the label after a tab, which Qt renders in
                # the menu's shortcut column. No real shortcut is registered:
                # these keys are routed by the app-wide event filter
                # (_handle_nav_key), which yields to arrow-consuming inputs - a
                # live shortcut here would fire even then. Plain text also shows
                # alternate keys ("+ / Ctrl+Right"), which QKeySequence can't.
                action = QAction(f"{text}\t{key}", menu)
                action.triggered.connect(handler)
                menu.addAction(action)

    def _build_navbar(self) -> QWidget:
        """The strip under the canvas: the current position + tile/row step buttons.

        Two rows - the address row (offset box, format dropdown, bank settings)
        and below it the step-button row - so the bank settings don't push the
        buttons off-screen at narrow widths.

        Up/Down step one tile-row (``columns`` tiles); Left/Right step one tile;
        +B/−B nudge the grid one byte (sub-tile alignment) and 0B clears the
        nudge; Pg Up/Dn step a whole window - the same actions the keys drive
        (:meth:`_build_nav_keys`).
        First/last page are keyboard + View menu only. The position box
        reads/writes addresses in the format the
        dropdown next to it selects: flat hex, or a ``bank:offset`` mapping
        parameterized by the three bank-setting spins (a preset fills them; a
        hand-edit flips the dropdown to Custom; the piecewise ExHiROM/ExLoROM
        presets hide them instead).
        """
        bar = QWidget()
        rows = QVBoxLayout(bar)
        rows.setContentsMargins(6, 2, 6, 2)
        rows.setSpacing(2)
        row = QHBoxLayout()  # the address row
        step_row = QHBoxLayout()
        rows.addLayout(row)
        rows.addLayout(step_row)

        # Bank settings - created before the dropdown whose handler fills them.
        # Hex spin boxes (not line edits) so they clamp and step like the rest of
        # the toolbar; disabled while the flat-hex format needs none of them.
        self._bank_size = self._hex_spin(0x1, 0x1000000, 0x8000, "Bank size in bytes")
        self._bank_addr = self._hex_spin(
            0x0, 0xFFFFFF, 0x8000, "Address of a bank's first byte"
        )
        self._bank_first = self._hex_spin(
            0x0, 0xFF, 0x00, "Bank of the file's first byte"
        )
        # The bank anchor is the setting users actually retune (mirror
        # conventions), so give it room beyond its two-digit size hint.
        self._bank_first.setFixedWidth(int(self._bank_first.sizeHint().width() * 1.4))
        self._bank_spins = (self._bank_size, self._bank_addr, self._bank_first)

        # Kept on self: its tooltip names the live address format, so it is
        # re-set alongside the box's in _refresh_offset_display.
        self._offset_label = QLabel("Offset ")
        row.addWidget(self._offset_label)
        # Half-width closed button (the format names are long), full-width popup -
        # the same compact treatment the pixel/palette pickers get.
        self._addr_format = CompactComboBox(0.5)
        self._addr_format.addItem("Hex", "hex")
        for preset in BANK_PRESETS:
            self._addr_format.addItem(preset.name, preset)
        self._addr_format.addItem("Custom", "custom")
        self._addr_format.setToolTip("Address format")
        self._addr_format.currentIndexChanged.connect(self._on_addr_format_change)
        row.addWidget(self._addr_format)

        # Editable file offset. A CommittingLineEdit commits on Enter / focus-out
        # (not per keystroke) and always re-renders on commit, so an invalid entry
        # reverts and a valid one shows its canonical form (byte-exact: a sub-tile
        # address becomes the grid's byte nudge); it keeps
        # its own arrow/Home keys, so the navigation shortcuts don't fire while
        # focused.
        self._offset_edit = CommittingLineEdit(self._parse_address, self._offset_text)
        self._offset_edit.setFixedWidth(104)
        self._offset_edit.setToolTip(self._offset_edit_tip())
        self._offset_label.setToolTip(self._offset_edit.toolTip())
        self._offset_label.setBuddy(self._offset_edit)
        self._offset_edit.committed.connect(self._jump_to_offset)
        row.addWidget(self._offset_edit)
        row.addSpacing(12)

        # The settings live in one container so the piecewise presets
        # (ExHiROM/ExLoROM), which the three-number model can't express, can
        # hide them wholesale instead of showing misleading values.
        self._bank_settings = QWidget()
        bank_row = QHBoxLayout(self._bank_settings)
        bank_row.setContentsMargins(0, 0, 0, 0)
        for label, spin in (
            ("Size", self._bank_size),
            ("Addr", self._bank_addr),
            ("Bank", self._bank_first),
        ):
            # The spin already carries the explanatory tip; the caption repeats it
            # so hovering either half of the pair answers the same question.
            add_labelled(bank_row, f" {label} ", spin, spin.toolTip())
        row.addWidget(self._bank_settings)
        row.addStretch(1)

        # Arrow steps use the style's standard icons rather than triangle glyphs:
        # the left/right triangles are emoji-capable codepoints, so font fallback
        # can render them in a different style from the up/down pair.
        sp = QStyle.StandardPixmap
        for text, icon, tip, handler in (
            (
                "Pg Dn",
                None,
                "Down one page (PgDown)",
                lambda: self._nav_rows(self._rows.value()),
            ),
            ("", sp.SP_ArrowDown, "Down one row (Down)", lambda: self._nav_rows(1)),
            ("", sp.SP_ArrowUp, "Up one row (Up)", lambda: self._nav_rows(-1)),
            (
                "Pg Up",
                None,
                "Up one page (PgUp)",
                lambda: self._nav_rows(-self._rows.value()),
            ),
            ("", sp.SP_ArrowLeft, "Back one tile (Left)", lambda: self._nav_tiles(-1)),
            (
                "",
                sp.SP_ArrowRight,
                "Forward one tile (Right)",
                lambda: self._nav_tiles(1),
            ),
            (
                "−B",
                None,
                "Nudge back one byte (- or Ctrl+Left)",
                lambda: self._nav_bytes(-1),
            ),
            (
                "+B",
                None,
                "Nudge forward one byte (+, = or Ctrl+Right)",
                lambda: self._nav_bytes(1),
            ),
            (
                "0B",
                None,
                "Clear the byte nudge (0)",
                self._clear_nudge,
            ),
        ):
            btn = QPushButton(text)
            if icon is not None:
                btn.setIcon(self.style().standardIcon(icon))
            btn.setToolTip(tip)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep arrow keys global
            btn.setFixedWidth(40)
            btn.clicked.connect(handler)
            step_row.addWidget(btn)

        # Surface the byte nudge when active - the tile grid looks ordinary, so
        # without this the sub-tile shift would be invisible state. Sits next to
        # the −B/+B buttons that change it.
        self._nudge_info = QLabel()
        step_row.addSpacing(8)
        step_row.addWidget(self._nudge_info)
        step_row.addStretch(1)
        return bar

    def _hex_spin(self, low: int, high: int, value: int, tip: str) -> QSpinBox:
        """A bank-setting spin box: hex display, commit-on-finish, $-prefixed."""
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setValue(value)
        spin.setDisplayIntegerBase(16)
        spin.setPrefix("$")
        spin.setKeyboardTracking(False)
        spin.setEnabled(False)  # the default format (flat hex) has no bank settings
        spin.setToolTip(f"{tip} (hex)")
        spin.valueChanged.connect(self._on_bank_setting_change)
        return spin

    def _offset_bar_style(self) -> str:
        """Accent-colored QSS for the file-position bar.

        Derived from the app's Highlight color so it stays theme-appropriate; a
        rounded accent handle on a tinted rail with the step arrows hidden makes it
        read clearly as a file navigator, distinct from the canvas's own scrollbars.
        """
        accent = self.palette().color(QPalette.ColorRole.Highlight)
        r, g, b = accent.red(), accent.green(), accent.blue()
        handle = accent.name()
        handle_hover = accent.lighter(120).name()
        return f"""
            QScrollBar:vertical {{
                width: 16px;
                background: rgba({r}, {g}, {b}, 38);
                border: none;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {handle};
                min-height: 28px;
                border-radius: 5px;
                margin: 3px 2px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {handle_hover}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; background: none; border: none;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """

    # Input widgets that use the arrow keys themselves; while one of these has focus
    # the navigation keys are left alone so it can cycle options / move the cursor.
    # The palette panel is one: focused (clicked), its Up/Down step subpalettes.
    # The files tree is another: its arrows walk the open-entries list (selection
    # is activation, so Up/Down switch the shown file/slice). The hex dump's text
    # area (a QTextEdit) keeps its arrows on the text cursor. These same panels
    # also claim the canvas editing shortcuts while focused (take_editing_shortcut),
    # so nav keys and editing keys alike stay theirs - not the canvas's.
    _ARROW_INPUT_TYPES = (
        QComboBox,
        QAbstractSpinBox,
        QLineEdit,
        QAbstractSlider,
        QTextEdit,
        PalettePanel,
        QTreeWidget,
    )

    def _build_nav_keys(self) -> None:
        """Map navigation keys to handlers, applied window-wide by :meth:`eventFilter`.

        Arrow / Home / End / PageUp-Down drive the view window (scroll is locked to the
        tile offset; PageUp/Down step a whole window of rows). Shift+arrows resize the
        window instead of moving it (↕ rows, ↔ cols); Ctrl+arrows nudge bytes. Keyed
        by ``(key, shift_held, ctrl_held)``.
        """
        no_mod = (False, False)
        shift = (True, False)
        ctrl = (False, True)
        self._nav_keys = {
            (Qt.Key.Key_Up, *no_mod): lambda: self._nav_rows(-1),
            (Qt.Key.Key_Down, *no_mod): lambda: self._nav_rows(1),
            (Qt.Key.Key_Left, *no_mod): lambda: self._nav_tiles(-1),
            (Qt.Key.Key_Right, *no_mod): lambda: self._nav_tiles(1),
            (Qt.Key.Key_PageUp, *no_mod): lambda: self._nav_rows(-self._rows.value()),
            (Qt.Key.Key_PageDown, *no_mod): lambda: self._nav_rows(self._rows.value()),
            (Qt.Key.Key_Home, *no_mod): self._nav_home,
            (Qt.Key.Key_End, *no_mod): self._nav_end,
            # Byte nudge. Plus is registered under both shift states: on many
            # layouts it is Shift+= (shift held), on the keypad it is bare. Bare
            # = also steps forward, so -/= work as a shiftless pair; Ctrl+arrows
            # mirror the pair for one-handed use, and 0 clears the nudge.
            (Qt.Key.Key_Minus, *no_mod): lambda: self._nav_bytes(-1),
            (Qt.Key.Key_Plus, *no_mod): lambda: self._nav_bytes(1),
            (Qt.Key.Key_Plus, *shift): lambda: self._nav_bytes(1),
            (Qt.Key.Key_Equal, *no_mod): lambda: self._nav_bytes(1),
            (Qt.Key.Key_Left, *ctrl): lambda: self._nav_bytes(-1),
            (Qt.Key.Key_Right, *ctrl): lambda: self._nav_bytes(1),
            (Qt.Key.Key_0, *no_mod): self._clear_nudge,
            (Qt.Key.Key_Up, *shift): lambda: self._adjust_spin(self._rows, -1),
            (Qt.Key.Key_Down, *shift): lambda: self._adjust_spin(self._rows, 1),
            (Qt.Key.Key_Left, *shift): lambda: self._adjust_spin(self._columns, -1),
            (Qt.Key.Key_Right, *shift): lambda: self._adjust_spin(self._columns, 1),
            # Not navigation, but the same routing need: bare letter keys that
            # must yield to focused text inputs (Palette ▸ Load from Selection,
            # View ▸ Grid).
            (Qt.Key.Key_P, *no_mod): self._load_palette_from_selection,
            (Qt.Key.Key_G, *no_mod): self._grid.toggle,
            (Qt.Key.Key_S, *no_mod): self._toggle_selection_mode,
            (Qt.Key.Key_E, *no_mod): self._toggle_edit_mode,
        }

    def eventFilter(self, obj, event) -> bool:
        # Installed on the QApplication so navigation keys act wherever focus is -
        # unlike a QShortcut, which a focused dropdown would pre-empt. Only while this
        # window is active, and _handle_nav_key defers to arrow-consuming inputs.
        et = event.type()
        # A press on the surround *around* the canvas deselects. The position has
        # to be checked, not just the receiving object: the canvas leaves its own
        # presses unaccepted (so ClickFocus still works), and Qt then propagates
        # them up to the viewport - which would otherwise clear the marquee the
        # press had just anchored, killing every drag-selection. Not consumed, so
        # the scroll area still does its normal thing.
        if (
            et == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
            and obj is self._scroll.viewport()
            and self._scroll.viewport().childAt(event.position().toPoint()) is None
        ):
            self._clear_selection_on_background()
        if (
            et in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
            and self.isActiveWindow()
            and event.key() == Qt.Key.Key_Space
            and self._handle_space_pan(event)
        ):
            return True
        if (
            et == QEvent.Type.KeyPress
            and self.isActiveWindow()
            and self._handle_nav_key(event)
        ):
            return True
        return super().eventFilter(obj, event)

    def _handle_space_pan(self, event) -> bool:
        """Arm/disarm the canvas's space-drag panning; True if the key is consumed.

        Yields to popups and focused text inputs (space types/activates there) and
        stays inert with no document. Auto-repeat from a held space is swallowed
        but re-arms nothing.
        """
        if QApplication.activePopupWidget() is not None:
            return False
        if isinstance(QApplication.focusWidget(), self._ARROW_INPUT_TYPES):
            return False
        if self._doc is None:
            return False
        if not event.isAutoRepeat():
            self._canvas.set_pan_mode(event.type() == QEvent.Type.KeyPress)
        return True

    def _pan_view(self, dx: int, dy: int) -> None:
        """Shift the scroll view by a space-drag delta (device pixels).

        The scroll bars clamp to the content, so a pan can never push the image off
        screen; when the view already fits the viewport their range is empty and
        this is a no-op.
        """
        hbar = self._scroll.horizontalScrollBar()
        vbar = self._scroll.verticalScrollBar()
        hbar.setValue(hbar.value() - dx)
        vbar.setValue(vbar.value() - dy)

    def _on_zoom_requested(self, steps: int, pos) -> None:
        """Wheel-zoom the canvas, keeping the pixel under the cursor stationary.

        Drives the zoom spin (so the change persists per entry and re-renders
        through the normal view path), then shifts the scroll bars so the image
        pixel that was under the cursor lands back under it — otherwise a zoom
        would appear to slide the art out from beneath the pointer. ``pos`` is the
        cursor in the canvas's device coordinates.
        """
        old = self._zoom.value()
        new = max(self._zoom.minimum(), min(self._zoom.maximum(), old + steps))
        if new == old:
            return
        hbar = self._scroll.horizontalScrollBar()
        vbar = self._scroll.verticalScrollBar()
        # The cursor's spot in the viewport, and the image pixel it sits on now.
        view_x = pos.x() - hbar.value()
        view_y = pos.y() - vbar.value()
        img_x, img_y = pos.x() / old, pos.y() / old
        self._zoom.setValue(new)  # re-renders and resizes the canvas synchronously
        # Put that same pixel back under the cursor; the scroll bars clamp so the
        # image can't be pushed off screen (a no-op when the view already fits).
        hbar.setValue(round(img_x * new - view_x))
        vbar.setValue(round(img_y * new - view_y))

    def _zoom_steps(self, steps: int) -> None:
        """Zoom from the View menu or its shortcut, anchored on the viewport centre.

        The wheel has a cursor to keep the art still under; a menu item or key
        press doesn't, so the middle of what's on screen is the natural fixed
        point. Reuses the wheel's anchoring by handing it that centre in the
        canvas's own device coordinates.
        """
        hbar = self._scroll.horizontalScrollBar()
        vbar = self._scroll.verticalScrollBar()
        viewport = self._scroll.viewport()
        self._on_zoom_requested(
            steps,
            QPointF(
                hbar.value() + viewport.width() / 2,
                vbar.value() + viewport.height() / 2,
            ),
        )

    def _viewport_centre_pixel(self) -> tuple[int, int]:
        """The image pixel at the middle of what is on screen.

        The scroll offsets alone don't give it: when the canvas is smaller than
        the viewport it sits centred inside it and the bars are empty, so the
        visible extent is the smaller of the two. What a paste centres on.
        """
        zoom = max(1, self._zoom.value())
        viewport = self._scroll.viewport()
        hbar = self._scroll.horizontalScrollBar()
        vbar = self._scroll.verticalScrollBar()
        cx = hbar.value() + min(viewport.width(), self._canvas.width()) / 2
        cy = vbar.value() + min(viewport.height(), self._canvas.height()) / 2
        return int(cx // zoom), int(cy // zoom)

    def _handle_nav_key(self, event) -> bool:
        """Run the navigation handler for ``event``; return True if it was consumed.

        Yields (returns False) when an arrow-consuming input has focus, a popup
        (e.g. an open menu, which arrow keys navigate) is up, or the event carries
        Alt/Meta, so only bare / Shift-ed / Ctrl-ed navigation keys ever act (an
        unregistered Ctrl combo still falls through to the normal shortcuts).
        """
        if self._scanning:
            return True  # a running scan owns the view position; swallow keys
        if QApplication.activePopupWidget() is not None:
            return False
        if isinstance(QApplication.focusWidget(), self._ARROW_INPUT_TYPES):
            return False
        mods = event.modifiers()
        blocked = Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier
        if mods & blocked:
            return False
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        # Pixel mode claims the bare number keys (tool select) and Escape (stamp
        # the float / drop the marquee) before the navigation map sees them.
        if self._pixel_key(event.key(), shift, ctrl):
            return True
        handler = self._nav_keys.get((event.key(), shift, ctrl))
        if handler is None:
            return False
        handler()
        return True

    @staticmethod
    def _adjust_spin(spin: QSpinBox, delta: int) -> None:
        # setValue clamps to the spinbox range and fires valueChanged, which
        # re-renders (and re-clamps the offset) through _on_view_change.
        spin.setValue(spin.value() + delta)

    # -- navigation --------------------------------------------------------
    def _nav_rows(self, delta_rows: int) -> None:
        """Move the window ``delta_rows`` tile-rows (± ``columns`` tiles each)."""
        self._set_offset(self._offset + delta_rows * self._columns.value())

    def _nav_tiles(self, delta_tiles: int) -> None:
        """Move the window ``delta_tiles`` single tiles."""
        self._set_offset(self._offset + delta_tiles)

    def _nav_bytes(self, delta: int) -> None:
        """Nudge the view origin ``delta`` bytes - sub-tile realignment.

        Works in byte space and carries across tile boundaries: nudging past
        ``bytes_per_tile`` rolls into the next tile with the nudge wrapped, so
        repeated +B/−B walks the file one byte at a time.
        """
        if self._doc is None or not self._doc.bytes_per_tile:
            return
        self._set_byte_position(self._byte_position() + delta)

    def _clear_nudge(self) -> None:
        """Snap the grid back to tile alignment, keeping the tile offset."""
        self._set_offset(self._offset, nudge=0)

    def _nav_home(self) -> None:
        self._set_offset(0)

    def _nav_end(self) -> None:
        if self._doc is not None:
            self._set_offset(self._doc.tile_count)  # clamped to the last page

    def _set_offset(self, offset: int, nudge: int | None = None) -> None:
        """Clamp the origin to a valid page and, if it moved, re-render.

        Tile-based moves pass only ``offset`` and keep the current byte nudge -
        the nudge is alignment state, not position, so paging/rowing preserves
        it. Byte-based moves (:meth:`_set_byte_position`) supply both.
        """
        if self._doc is None or self._applying_undo:
            return
        if nudge is None:
            nudge = self._nudge
        offset = self._doc.clamp_offset(
            offset, self._columns.value(), self._rows.value(), nudge
        )
        if (offset, nudge) == (self._offset, self._nudge):
            # No move (e.g. a scrollbar drag past the end clamped to here) - still
            # snap the scrollbar/box back onto the clamped position.
            self._sync_nav()
            return
        # Floating pixels are positioned against the window they were dropped
        # over, so they come down before it slides out from under them.
        self._commit_float()
        entry = self._workspace.current
        assert entry is not None  # a document implies a current entry
        self._push_command(
            OffsetMoveCommand(
                self,
                entry,
                before=(self._offset, self._nudge),
                after=(offset, nudge),
            )
        )

    def _apply_offset(self, offset: int, nudge: int) -> None:
        """Land the view on an already-clamped position (commands only -
        gestures go through :meth:`_set_offset`, which clamps and pushes)."""
        self._offset, self._nudge = offset, nudge
        self._refresh_view()  # re-clamps defensively if cols/rows changed since

    def _byte_position(self) -> int:
        """The view origin as a byte position on the tile grid (0 = file start)."""
        assert self._doc is not None
        return self._offset * self._doc.bytes_per_tile + self._nudge

    def _set_byte_position(self, pos: int) -> None:
        """Move the view origin to byte ``pos`` of the tile grid (0 = file start).

        The model clamps the position in byte space and splits it into a tile
        offset plus a sub-tile nudge (:meth:`Document.clamp_byte_position`).
        """
        assert self._doc is not None
        offset, nudge = self._doc.clamp_byte_position(
            pos, self._columns.value(), self._rows.value()
        )
        self._set_offset(offset, nudge=nudge)

    def _bank_layout(self) -> BankLayout | SplitBankLayout | None:
        """The bank mapping in effect, or None when the format is flat hex.

        A preset supplies its own layout object - it may fold a mirror anchor
        or be a piecewise split, neither of which the spins can express. Custom
        builds a plain three-number layout from the spins (which any hand-edit
        of a preset's values flips to, so the spins stay the truth there).
        """
        data = self._addr_format.currentData()
        if data == "hex":
            return None
        if isinstance(data, BankPreset):
            return data.layout
        return BankLayout(
            bank_size=self._bank_size.value(),
            addr_base=self._bank_addr.value(),
            bank_base=self._bank_first.value(),
        )

    def _format_offset(self, byte_off: int) -> str:
        """Render a byte offset in the active address format (box + status text)."""
        layout = self._bank_layout()
        return format_hex(byte_off) if layout is None else layout.format(byte_off)

    def _parse_address(self, text: str) -> int | None:
        """Parse the offset box's text as a file byte offset, or None if invalid."""
        layout = self._bank_layout()
        return parse_hex(text) if layout is None else layout.parse(text)

    def _offset_edit_tip(self) -> str:
        return f"File position ({self._addr_format.currentText()}) - Enter to jump"

    def _refresh_offset_display(self) -> None:
        self._offset_edit.setToolTip(self._offset_edit_tip())
        self._offset_label.setToolTip(self._offset_edit.toolTip())
        if self._doc is not None and not self._offset_edit.hasFocus():
            self._offset_edit.refresh()
        # The palette offset field shares the address conventions, so a format
        # or bank-setting change must re-render it too (its provider returns ""
        # when Offset mode isn't active, so this is safe at any time).
        if not self._palette_offset_edit.hasFocus():
            self._palette_offset_edit.refresh()
        # The hex dump's address column follows the same format.
        self._refresh_hex()

    def _on_addr_format_change(self) -> None:
        """Apply a newly chosen format: fill settings from a preset, re-render."""
        data = self._addr_format.currentData()
        layout = data.layout if isinstance(data, BankPreset) else None
        if isinstance(layout, BankLayout):
            # Block the spins' signals: this programmatic fill is the preset
            # itself, not a divergence, so it must not flip the box to Custom.
            with signals_blocked(*self._bank_spins):
                self._bank_size.setValue(layout.bank_size)
                self._bank_addr.setValue(layout.addr_base)
                self._bank_first.setValue(layout.bank_base)
        # Piecewise (split) layouts have no three-number equivalent - hide the
        # settings rather than display values that don't describe the mapping.
        self._bank_settings.setVisible(not isinstance(layout, SplitBankLayout))
        for spin in self._bank_spins:
            spin.setEnabled(data != "hex")
        self._refresh_offset_display()

    def _on_bank_setting_change(self) -> None:
        """A hand-edited bank setting means the selected preset no longer holds."""
        if isinstance(self._addr_format.currentData(), BankPreset):
            # Fires _on_addr_format_change, which re-renders the offset box.
            self._addr_format.setCurrentIndex(self._addr_format.findData("custom"))
        else:
            self._refresh_offset_display()

    def _display_base(self) -> int:
        """The file byte the view's position 0 corresponds to - display policy.

        Raw sources (no decompressor) show source-file-absolute addresses: the
        header skip for a whole file, the slice offset for a raw slice - so ROM
        bank addresses stay meaningful wherever the bytes came from. A
        decompressed stream has no linear mapping back to file offsets, so it
        shows its own 0-based positions instead of lying with file addresses.
        """
        assert self._doc is not None
        cfg = self._doc.pixel_config
        return cfg.source.offset if cfg.decompress_id == NO_DECOMPRESS else 0

    def _tile_byte_offset(self, tile: int) -> int:
        """The displayed byte offset of ``tile`` on the current (nudged) grid."""
        assert self._doc is not None
        return self._display_base() + self._nudge + tile * self._doc.bytes_per_tile

    def _offset_text(self) -> str:
        """The current byte offset rendered in the chosen address format.

        Also the offset box's ``current_text`` provider - it re-renders from this on
        every commit, so it must be safe to call with no document loaded.
        """
        if self._doc is None:
            return ""
        return self._format_offset(self._tile_byte_offset(self._offset))

    def _jump_to_offset(self, byte_off: int) -> None:
        """Jump to a file byte offset - the offset box's commit handler.

        Byte-exact: a sub-tile address sets the byte nudge, so typing any offset
        lands the grid on it. The box re-renders itself from :meth:`_offset_text`
        after this, so there's no text handling to do here; an out-of-range value
        is clamped by _set_byte_position.
        """
        if self._doc is None:
            return
        self._set_byte_position(byte_off - self._display_base())

    def _sync_nav(self) -> None:
        """Mirror the current offset into the hex box and the position bar."""
        has_doc = self._doc is not None
        self._offset_edit.setEnabled(has_doc)
        self._offset_bar.setEnabled(has_doc)
        if not has_doc:
            self._offset_edit.clear()
            self._nudge_info.clear()
            return

        cols, rows = self._columns.value(), self._rows.value()
        # Don't overwrite what the user is mid-way through typing; a commit re-renders
        # the box itself (CommittingLineEdit.commit), so this guard is safe.
        if not self._offset_edit.hasFocus():
            self._offset_edit.refresh()
        self._nudge_info.setText(f"+{self._nudge} B" if self._nudge else "")

        # Scrollbar spans the whole file: value = offset, page = one window of tiles,
        # so the handle size reflects how much of the file is on screen.
        page = max(1, cols) * max(1, rows)
        max_off = self._doc.clamp_offset(self._doc.tile_count, cols, rows, self._nudge)
        bar = self._offset_bar
        with signals_blocked(bar):  # setValue here must not re-enter _set_offset
            bar.setEnabled(max_off > 0)
            bar.setRange(0, max_off)
            bar.setSingleStep(cols)
            bar.setPageStep(page)
            bar.setValue(self._offset)

    def _land_on_byte(self, file_offset: int) -> None:
        """Move the view origin to an absolute file byte, without pushing an undo.

        Same byte→tile/nudge split as :meth:`_set_byte_position`, but it applies
        the position directly: a jump-to-source is navigation, not an edit.
        """
        if self._doc is None:
            return
        pos = file_offset - self._display_base()
        offset, nudge = self._doc.clamp_byte_position(
            pos, self._columns.value(), self._rows.value()
        )
        self._apply_offset(offset, nudge)
