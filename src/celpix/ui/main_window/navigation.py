"""Where the view sits in the file, and how the user moves it.

The window shows a fixed **window** of tiles rather than scrolling freely, so
"position" is a tile ``_offset`` plus a sub-tile ``_nudge`` - both owned by the
window and clamped by the document. This module is everything that reads or
writes that pair: the navigation bar under the canvas, the step/page/home
actions behind the Navigate menu and the keyboard, and the address machinery
that renders an offset as flat hex or a ``bank:offset`` mapping.

Navigation keys are routed by an **application event filter** rather than
``QShortcut`` so they work wherever focus is - except inside a widget that uses
the arrow keys itself, which :meth:`_handle_nav_key` yields to. The window's
bare *letter* keys (G, S, E, R, T and their Shift forms) are filtered the same
way for the same reason, and are collected here for it: each one names the
control it presses (:class:`KeyControl`), which is what keeps a key from acting
while the thing it drives is switched off.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCursor,
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
from celpix.core.capabilities import Capability
from celpix.project.workspace import EntryKind
from celpix.ui.hex_view_panel import HexDumpView
from celpix.ui.palette_panel import PalettePanel
from celpix.ui.undo_commands import (
    OffsetMoveCommand,
)
from celpix.ui.widgets import (
    CommittingLineEdit,
    CompactComboBox,
    add_labelled,
    hex_spin,
    pan_scroll_area,
    signals_blocked,
    zoom_anchored,
    zoom_level_after,
)


class KeyControl(NamedTuple):
    """A bare-key shortcut, expressed as **the control it presses**.

    The bare letters are routed by the app-wide event filter rather than bound
    to their actions (see the module docstring), which means Qt does not disarm
    them the way it disarms a disabled action's real shortcut — so every one of
    them has to be refused by hand. Doing that per handler means each one carries
    its own copy of the conditions, which drifts from the copy that greys the
    control: that is what leaves ``S`` swapping a selection shape the picker has
    locked.

    So a key names its control rather than its effect, and
    :meth:`NavigationMixin._handle_nav_key` asks that one object. A key is dead
    exactly when clicking the control would do nothing — there is no second
    predicate to keep in step. The table of them is
    :meth:`NavigationMixin._build_key_controls`.

    ``control`` is whatever the user would otherwise click: usually the
    :class:`QAction` behind a menu row or a toolbar button, but a plain widget
    (the Selection Shape combo) or a :class:`QActionGroup` (the grid styles)
    where that is what the key drives. All three answer ``isEnabled()``.

    ``press`` is what the key does, defaulting to triggering the action — given
    explicitly where the control is not a single action, or where the effect is
    a step through it rather than a click on it.
    """

    control: QAction | QActionGroup | QWidget
    press: Callable[[], None] | None = None

    def fire(self) -> None:
        """Do what a click on the control would; nothing while it is switched off."""
        if not self.control.isEnabled():
            return
        # Only an *action* is asked whether it is visible. A hidden action means
        # "not a thing on this document" - the Edit Tiles mode off a tilemap - so
        # its key has to go with it. A QWidget answers False for as long as its
        # window is unshown (startup, and every offscreen test), which would
        # strand the key rather than gate it.
        if isinstance(self.control, QAction | QActionGroup):
            if not self.control.isVisible():
                return
        (self.press or self.control.trigger)()


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

        Back/Forward lead: they move between *entries* rather than within one, so
        they belong at the head of the menu and are the only actions in it with
        real shortcuts (see :meth:`_add_history_actions`).

        The last field of each row says whether it addresses the **view window** —
        moving it, or resizing it by rows. Those are what ``NAVIGATION`` gates, so
        they are collected into :attr:`_nav_window_actions` for the capability
        pass to switch off on a document that is always shown entire. The column
        rows are not among them: a map's cell width is a live setting of its own,
        which is why ``capabilities.py`` refuses a tilemap the row count and the
        position and says nothing about columns.
        """
        menu = self.menuBar().addMenu("&Navigate")
        self._add_history_actions(menu)
        groups: tuple[tuple[tuple[str, str, Callable[[], None], bool], ...], ...] = (
            (
                ("&First page", "Home", self._nav_home, True),
                ("&Last page", "End", self._nav_end, True),
            ),
            (
                ("&Previous byte", "- / Ctrl+Left", lambda: self._nav_bytes(-1), True),
                # "=" is advertised beside "+": on most layouts + is Shift+=, and
                # the bare key is what makes -/= a pair the fingers can hold.
                ("&Next byte", "+ = / Ctrl+Right", lambda: self._nav_bytes(1), True),
                ("&Zero byte offset", "0", self._clear_nudge, True),
                ("Previous &tile", "Left", lambda: self._nav_tiles(-1), True),
                ("Next til&e", "Right", lambda: self._nav_tiles(1), True),
                ("Row &up", "Up", lambda: self._nav_rows(-self._row_step()), True),
                ("Row &down", "Down", lambda: self._nav_rows(self._row_step()), True),
                ("Pa&ge up", "PgUp", lambda: self._nav_rows(-self._view_rows()), True),
                (
                    "Page do&wn",
                    "PgDown",
                    lambda: self._nav_rows(self._view_rows()),
                    True,
                ),
            ),
            (
                (
                    "Fewer &columns",
                    "Shift+Left",
                    lambda: self._adjust_columns(-1),
                    False,
                ),
                (
                    "&More columns",
                    "Shift+Right",
                    lambda: self._adjust_columns(1),
                    False,
                ),
                (
                    "Fewer &rows",
                    "Shift+Up",
                    lambda: self._adjust_spin(self._rows, -1),
                    True,
                ),
                (
                    "More r&ows",
                    "Shift+Down",
                    lambda: self._adjust_spin(self._rows, 1),
                    True,
                ),
            ),
        )
        window_actions: list[QAction] = []
        for i, group in enumerate(groups):
            if i:
                menu.addSeparator()
            for text, key, handler, addresses_window in group:
                # The key text goes in the label after a tab, which Qt renders in
                # the menu's shortcut column. No real shortcut is registered:
                # these keys are routed by the app-wide event filter
                # (_handle_nav_key), which yields to arrow-consuming inputs - a
                # live shortcut here would fire even then. Plain text also shows
                # alternate keys ("+ / Ctrl+Right"), which QKeySequence can't.
                action = QAction(f"{text}\t{key}", menu)
                action.triggered.connect(handler)
                menu.addAction(action)
                if addresses_window:
                    window_actions.append(action)
        self._nav_window_actions = tuple(window_actions)

    def _build_navbar(self) -> QWidget:
        """The strip under the canvas: the current position + tile/row step buttons.

        Two rows - the address row (offset box, format dropdown, bank settings)
        and below it the step-button row - so the bank settings don't push the
        buttons off-screen at narrow widths.

        Up/Down step one tile-row (``columns`` tiles); Left/Right step one tile;
        +B/−B nudge the grid one byte (sub-tile alignment) and 0B clears the
        nudge; Pg Up/Dn step a whole window - the same actions the keys drive
        (:meth:`_build_nav_keys`).
        First/last page are keyboard + Navigate menu only. The position box
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
        self._bank_size = hex_spin(0x1, 0x1000000, "Bank size in bytes", 0x8000)
        self._bank_addr = hex_spin(
            0x0, 0xFFFFFF, "Address of a bank's first byte", 0x8000
        )
        self._bank_first = hex_spin(0x0, 0xFF, "Bank of the file's first byte")
        # The bank anchor is the setting users actually retune (mirror
        # conventions), so give it room beyond its two-digit size hint.
        self._bank_first.setFixedWidth(int(self._bank_first.sizeHint().width() * 1.4))
        self._bank_spins = (self._bank_size, self._bank_addr, self._bank_first)
        for spin in self._bank_spins:
            spin.setEnabled(False)  # the default format (flat hex) has none of them
            spin.valueChanged.connect(self._on_bank_setting_change)

        # Kept on self: its tooltip names the live address format, so it is
        # re-set alongside the box's in _refresh_offset_display.
        self._offset_label = QLabel("Offset ")
        row.addWidget(self._offset_label)
        # Narrow closed button (the bank-layout names run to 36 characters),
        # full-width popup - the same compact treatment the format pickers get.
        # Below theirs because this one is a short list of fixed names rather
        # than a registry that grows.
        self._addr_format = CompactComboBox(140)
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
        self._address_edit = CommittingLineEdit(self._parse_address, self._offset_text)
        self._address_edit.setFixedWidth(104)
        self._address_edit.setToolTip(self._address_edit_tip())
        self._offset_label.setToolTip(self._address_edit.toolTip())
        self._offset_label.setBuddy(self._address_edit)
        self._address_edit.committed.connect(self._jump_to_address)
        row.addWidget(self._address_edit)
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
                lambda: self._nav_rows(self._view_rows()),
            ),
            (
                "",
                sp.SP_ArrowDown,
                "Down one row (Down)\nA whole block-row in a block pattern",
                lambda: self._nav_rows(self._row_step()),
            ),
            (
                "",
                sp.SP_ArrowUp,
                "Up one row (Up)\nA whole block-row in a block pattern",
                lambda: self._nav_rows(-self._row_step()),
            ),
            (
                "Pg Up",
                None,
                "Up one page (PgUp)",
                lambda: self._nav_rows(-self._view_rows()),
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

    def _tile_offset_bar_style(self) -> str:
        """Accent-colored QSS for the file-position bar.

        Derived from the app's Highlight color so it stays theme-appropriate; a
        rounded accent handle on a tinted rail with the step arrows hidden makes it
        read clearly as a file navigator, distinct from the canvas's own scrollbars.

        The accent is read off the **application** rather than off this window: a
        theme switch installs the new palette on the application immediately but
        propagates it to the widgets through the event loop, so the window's own
        palette is still the outgoing one at the moment the switch regenerates
        this string.
        """
        accent = QApplication.palette().color(QPalette.ColorRole.Highlight)
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
    # The palette panel is one: focused (clicked), its Up/Down step palette rows.
    # The files tree is another: its arrows walk the open-entries list (selection
    # is activation, so Up/Down switch the shown file/slice). The hex dump keeps
    # its arrows on its own byte cursor. These same panels also claim the canvas
    # editing shortcuts while focused (take_editing_shortcut),
    # so nav keys and editing keys alike stay theirs - not the canvas's.
    _ARROW_INPUT_TYPES = (
        QComboBox,
        QAbstractSpinBox,
        QLineEdit,
        QAbstractSlider,
        QTextEdit,
        HexDumpView,
        PalettePanel,
        QTreeWidget,
    )

    # The same yield for the **space bar**, which the pan gesture claims
    # window-wide - and a shorter list, because the two keys are not typed in the
    # same places. Space is a character in a text input and picks the current row
    # in a list, so those keep it; a spin box, a slider and the palette grid do
    # nothing with it, and yielding to them left the pan dead exactly where it is
    # most wanted - on the Zoom spin the user has just clicked before reaching for
    # the sheet they magnified.
    _SPACE_INPUT_TYPES = (
        QComboBox,
        QLineEdit,
        QTextEdit,
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
            (Qt.Key.Key_Up, *no_mod): lambda: self._nav_rows(-self._row_step()),
            (Qt.Key.Key_Down, *no_mod): lambda: self._nav_rows(self._row_step()),
            (Qt.Key.Key_Left, *no_mod): lambda: self._nav_tiles(-1),
            (Qt.Key.Key_Right, *no_mod): lambda: self._nav_tiles(1),
            (Qt.Key.Key_PageUp, *no_mod): lambda: self._nav_rows(-self._view_rows()),
            (Qt.Key.Key_PageDown, *no_mod): lambda: self._nav_rows(self._view_rows()),
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
            (Qt.Key.Key_Left, *shift): lambda: self._adjust_columns(-1),
            (Qt.Key.Key_Right, *shift): lambda: self._adjust_columns(1),
        }
        self._build_key_controls()

    def _build_key_controls(self) -> None:
        """The bare letter keys, each named by the control it presses.

        Not navigation, but the same routing need: they must yield to a focused
        text input, so they are filtered app-wide rather than bound as shortcuts
        (see :class:`KeyControl` for what that costs and what this table pays it
        with). Keyed like :meth:`_build_nav_keys`, by ``(key, shift, ctrl)``.

        Each row points at the thing on screen the key stands in for, and every
        new one has to: that is the whole guarantee, and it is why the middle
        column is a control rather than a handler. Where a mode has both a
        toolbar button and an Edit-menu row, the button is named — it is the one
        the user is looking at, and the row is greyed in step with it.

        The transform bar's flip/rotate letters are not here. They are handled
        ahead of this table (:meth:`~...transform.TransformMixin._transform_key`)
        because which button they press depends on the group it is showing, but
        they are refused on exactly these terms.
        """
        no_mod = (False, False)
        shift = (True, False)
        self._key_controls = {
            (Qt.Key.Key_P, *no_mod): KeyControl(self._palette_from_selection_action),
            (Qt.Key.Key_G, *no_mod): KeyControl(self._grid),
            # The style is a radio group rather than one control, and the key
            # steps through it rather than clicking any single row of it.
            (Qt.Key.Key_G, *shift): KeyControl(
                self._grid_style_group, self._cycle_grid_style
            ),
            # The picker itself, not the Edit ▸ row that documents the key: it is
            # a widget, so Qt already answers for the transform bar it sits on
            # (greyed with no document) as well as for the shapes being forced.
            (Qt.Key.Key_S, *no_mod): KeyControl(
                self._selection_shape, self._toggle_selection_mode
            ),
            (Qt.Key.Key_E, *no_mod): KeyControl(self._edit_mode_action),
            (Qt.Key.Key_R, *no_mod): KeyControl(self._rearrange_action),
            (Qt.Key.Key_R, *shift): KeyControl(self._show_rearranged_action),
            (Qt.Key.Key_T, *no_mod): KeyControl(self._stamp_action),
            (Qt.Key.Key_P, *shift): KeyControl(self._show_palette_regions_action),
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
        # The back/forward mouse buttons walk the visit trail from anywhere in the
        # window, for the same reason the nav keys are filtered rather than bound:
        # they are a gesture on the window, not on whatever happens to be under
        # the pointer (and no widget here wants those buttons for itself).
        if (
            et
            in (
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonDblClick,
                QEvent.Type.MouseButtonRelease,
            )
            and self.isActiveWindow()
            and self._handle_history_mouse(event)
        ):
            return True
        # A space hold that outlives this window's activation - alt-tab, or the
        # player window raised over it. The release lands wherever the focus went
        # and is never seen here, so both surfaces go down now; one left armed
        # keeps its open hand and eats the next press it gets, which is the first
        # click of whoever comes back to the window.
        if et == QEvent.Type.WindowDeactivate and obj is self:
            self._canvas.set_pan_mode(False)
            self._tile_source_panel.set_pan_mode(False)
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
        """Arm/disarm space-drag panning; True if the key is consumed.

        Yields to popups and focused text inputs (space types/activates there) and
        stays inert with no document. Auto-repeat from a held space is swallowed
        but re-arms nothing.

        **Which** surface pans is :meth:`_pan_surface`'s answer. Decided here
        rather than by each panel claiming the key, because the key is filtered
        app-wide — two claimants would arm both, and the one that is not being
        looked at would sit holding an open hand until space came up.
        """
        if QApplication.activePopupWidget() is not None:
            return False
        if isinstance(QApplication.focusWidget(), self._SPACE_INPUT_TYPES):
            return False
        if self._doc is None:
            return False
        if not event.isAutoRepeat():
            on = event.type() == QEvent.Type.KeyPress
            surface = self._pan_surface()
            # The other one goes down regardless: the pointer can move while
            # space is held, and a surface left armed keeps the hand cursor and
            # eats the next press it gets.
            other = (
                self._canvas
                if surface is self._tile_source_panel
                else self._tile_source_panel
            )
            other.set_pan_mode(False)
            surface.set_pan_mode(on)
        return True

    def _pan_surface(self):  # noqa: ANN201 — Canvas | TileSourcePanel
        """Which surface a space-drag pans: whichever one the **pointer** is over.

        The tile source sheet is scrollable and magnifiable in its own right, so
        space over it has to move it rather than the canvas behind it. The
        pointer decides rather than the focus ring, because the pointer is what
        does the dragging: a user reaches for the sheet with the mouse — often
        straight from the Zoom spin that made it too big for its dock, having
        never clicked a tile — and the surface that grows a hand cursor has to be
        the one under the hand. It is also the surface that will get the press:
        arming the other one would leave a hand hovering over a sheet that does
        not move.

        ``visibleRegion`` rather than the panel's rectangle, because that
        rectangle is the whole sheet — most of it scrolled out of the dock, and
        all of it while the Palette holds their shared tab. The region is what is
        actually on screen, so it answers "on the sheet" and "the sheet is even
        showing" in one test.
        """
        panel = self._tile_source_panel
        if panel.visibleRegion().contains(panel.mapFromGlobal(QCursor.pos())):
            return panel
        return self._canvas

    def _pan_view(self, dx: int, dy: int) -> None:
        """Shift the scroll view by a space-drag delta (device pixels)."""
        pan_scroll_area(self._scroll, dx, dy)

    def _on_zoom_requested(self, steps: int, pos) -> None:  # noqa: ANN001 — QPointF
        """Wheel-zoom the canvas, keeping the pixel under the cursor stationary.

        ``pos`` is the cursor in the canvas's device coordinates. One wheel notch
        is one *level*, not one multiplier: the levels are not evenly spaced (0.5
        sits under 1), so the list is what a step walks — the only thing this
        view's zoom does differently from the docks' and the player's.
        """
        zoom_anchored(
            self._scroll, self._zoom, zoom_level_after(self._zoom.value(), steps), pos
        )

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
        zoom = self._zoom.value()
        viewport = self._scroll.viewport()
        hbar = self._scroll.horizontalScrollBar()
        vbar = self._scroll.verticalScrollBar()
        cx = hbar.value() + min(viewport.width(), self._canvas.width()) / 2
        cy = vbar.value() + min(viewport.height(), self._canvas.height()) / 2
        return int(cx // zoom), int(cy // zoom)

    def _handle_nav_key(self, event) -> bool:
        """Run the handler for ``event``; return True if it was consumed.

        Yields (returns False) when an arrow-consuming input has focus, a popup
        (e.g. an open menu, which arrow keys navigate) is up, or the event carries
        Alt/Meta, so only bare / Shift-ed / Ctrl-ed navigation keys ever act (an
        unregistered Ctrl combo still falls through to the normal shortcuts).

        Two tables, in this order: the bare letters that press a control
        (:meth:`_build_key_controls`), then the navigation keys that move the view
        (:meth:`_build_nav_keys`). The letters are refused when their control is
        off; the navigation keys are refused where every position gesture is, at
        :meth:`_set_offset`.
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
        # A rearrange drag claims Escape first: whatever navigation would
        # otherwise do with it, it cannot put a tile stranded in the air back
        # down. Then the transform bar's flip/rotate letters, which act on
        # whichever group it is showing - that tool's pair included.
        if self._rearrange_key(event.key(), shift, ctrl):
            return True
        if self._transform_key(event.key(), shift, ctrl):
            return True
        # Pixel mode claims the bare number keys (tool select) and Escape (stamp
        # the float / drop the marquee) before the navigation map sees them.
        if self._pixel_key(event.key(), shift, ctrl):
            return True
        # A bare letter presses its control, and is swallowed either way: the
        # control being switched off is an answer, and letting the letter fall
        # through to something else would be a surprise (the reading
        # ``_transform_key`` takes for the same keys' neighbours).
        binding = self._key_controls.get((event.key(), shift, ctrl))
        if binding is not None:
            binding.fire()
            return True
        handler = self._nav_keys.get((event.key(), shift, ctrl))
        if handler is None:
            return False
        handler()
        return True

    def _adjust_columns(self, delta: int) -> None:
        """Widen or narrow whichever sheet the Cols keys are addressing.

        The view's own width normally — and the **tile source sheet's** while
        that panel holds the focus. The sheet is a second grid of tiles with a
        width of its own, laid out by a spin up in the dock's header; a user
        working in the sheet who reaches for Shift+arrow means that width, and
        moving the canvas's instead re-lays the picture they were not looking at.

        Focus, not the pointer that picks the surface for a space-drag
        (:meth:`_pan_surface`): a drag happens where the cursor is, a key press
        where the user is typing. The sheet takes focus on a click, so having
        worked in it is what says so — a click on the grey around it included,
        since that band is the sheet as far as a user is concerned
        (:meth:`~celpix.ui.widgets.PanZoomSurface.claim_background`).
        """
        panel = self._tile_source_panel
        spin = (
            self._tile_source_columns
            if QApplication.focusWidget() is panel
            else self._columns
        )
        self._adjust_spin(spin, delta)

    @staticmethod
    def _adjust_spin(spin: QSpinBox, delta: int) -> None:
        # setValue clamps to the spinbox range and fires valueChanged, which
        # re-renders (and re-clamps the offset) through _on_view_change.
        # A locked spin ignores the step: setValue works on a disabled widget, so
        # without this the keys would keep editing a control the user can't - Rows
        # under View > Entire File, or anything on a greyed-out toolbar.
        if not spin.isEnabled():
            return
        # In the spin's own step rather than in ones: a Cols that only moves in
        # whole stamps (:attr:`~celpix.core.document.Document.stamp_columns`) sets
        # a step of two, and a key that moved by one would floor straight back and
        # read as a dead shortcut.
        spin.setValue(spin.value() + delta * max(1, spin.singleStep()))

    # -- navigation --------------------------------------------------------
    def _row_step(self) -> int:
        """Tile-rows a single Up/Down step moves - the block height.

        With a block grouping taller than one tile, a one-row move would re-cut
        every block from a mid-block origin and scramble the image, so the unit
        of vertical movement is a whole block-row. Plain layouts keep the
        ordinary single row (block height 1).
        """
        return max(1, self._block_rows.value())

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

    def _on_tile_offset_bar_change(self, value: int) -> None:
        """Scrub the file-position bar in whole rows (block-rows in a block pattern).

        A drag can land the raw slider value on any tile, which would shift the
        image sideways by the sub-row remainder - and re-cut every block under
        a block grouping - so the value snaps to the nearest vertical step
        (:meth:`_row_step` rows). The bar's maximum is row-aligned
        (:meth:`Document.last_page_tile_offset` rounds up to a whole row), so a
        row snap never overshoots the end clamp; a block-row snap can, and
        :meth:`_set_offset` clamps it back to the last page. Tile-level moves
        stay available on the keys/buttons, and the byte nudge is untouched.
        """
        self._set_offset(self._snap_to_row(value))

    def _snap_to_row(self, tile: int) -> int:
        """``tile`` pulled onto the nearest whole (block-)row of the window.

        The vertical unit of movement: landing between rows would shift the image
        sideways by the sub-row remainder, and re-cut every block under a block
        grouping (:meth:`_row_step`). Rounds to the *nearest* row, so a position
        an eighth of a row past the boundary doesn't jump a whole row down.
        """
        unit = max(1, self._columns.value()) * self._row_step()
        return (tile + unit // 2) // unit * unit

    def _snap_offset_to_selection(self) -> None:
        """Re-anchor the window on the selected tile, snapped to its row.

        For leaving View ▸ Entire File: the window collapses back to Rows around
        offset 0 - the file's start, which is rarely where the user was reading.
        The selection is: it is the thing they picked out of the whole-file view.
        Snapped by the position bar's own rule above, and with no selection the
        offset is left where it is.

        Assigns rather than going through :meth:`_set_offset`: following a view
        change back to where the user was looking is not a navigation gesture to
        undo, and the caller's own refresh clamps this to the last page.
        """
        if self._doc is None or self._selected_tile is None:
            return
        self._offset = self._snap_to_row(self._selected_tile)

    def _nav_end(self) -> None:
        if self._doc is not None:
            self._set_offset(self._doc.tile_count)  # clamped to the last page

    def _set_offset(self, offset: int, nudge: int | None = None) -> None:
        """Clamp the origin to a valid page and, if it moved, re-render.

        Tile-based moves pass only ``offset`` and keep the current byte nudge -
        the nudge is alignment state, not position, so paging/rowing preserves
        it. Byte-based moves (:meth:`_set_byte_position`) supply both.

        Every deliberate reposition also ends a pixel-format switching run: the
        place the user just navigated to is the position now, so the next format
        switch anchors there instead of on the target a previous run captured.
        Focus alone can't decide this - the position bar never takes focus, so a
        drag would otherwise leave the stale target live.

        Inert on a document with no view window (a tilemap, which is always drawn
        entire - ``Capability.NAVIGATION``). The bar and the menu rows are already
        gated, but the nav **keys** are filtered app-wide rather than bound to
        those actions, so this is where they stop: without it an arrow key moved a
        position nothing renders from, and pushed an undo step that dirtied the
        project for a move the user could not see.
        """
        if self._doc is None or self._applying_undo:
            return
        if not self._can(Capability.NAVIGATION):
            return
        self._end_pixel_switch_run()
        if nudge is None:
            nudge = self._nudge
        offset = self._doc.clamp_tile_offset(
            offset, self._columns.value(), self._view_rows(), nudge
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
            pos, self._columns.value(), self._view_rows()
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

    def _address_edit_tip(self) -> str:
        return f"File position ({self._addr_format.currentText()})\nEnter to jump"

    def _refresh_offset_display(self) -> None:
        self._address_edit.setToolTip(self._address_edit_tip())
        self._offset_label.setToolTip(self._address_edit.toolTip())
        if self._doc is not None and not self._address_edit.hasFocus():
            self._address_edit.refresh()
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

    def _anchor_base(self) -> int:
        """The file byte the view's position 0 corresponds to - the coordinates an
        offset is *written down* in (slice offsets, Offset-mode palette addresses,
        jump-to-source), not the ones the address box shows.

        Raw sources (no decompressor, no reshape) anchor source-file-absolute:
        past whatever a container skipped for a whole file, the slice offset for a
        raw slice. A decompressed stream has no linear mapping back to file
        offsets, and a reshaped one is a byte permutation of its region, so under
        either the base is 0 and those offsets are positions in the reordered
        buffer.

        The base comes from what Read *recorded* rather than from the config's
        requested offset, because only the container knows where it actually began:
        a container works its start out from the format (past a copier header,
        past the iNES header and the PRG banks) and the host never asked for it.
        """
        assert self._doc is not None
        return self._doc.anchor_base

    def _addresses_are_view_relative(self) -> bool:
        """Whether the address surfaces count from the view's own first byte.

        True for a **slice**: it is a window the user drew on another entry, and
        what they want to read off it is a position *within it* - "which byte of
        this font", not where the font sits in the ROM. That parent address is
        already on the entry itself (its tooltip, and Jump to Source), and reading
        it off the box was actively misleading next to a reshaped sibling of the
        same region, which had to fall back to 0-based anyway.

        False for a whole file, whose addresses stay the file's, so a container's
        skipped header and any ROM bank format still name real cartridge bytes.
        """
        entry = self._workspace.current
        return entry is not None and entry.kind is EntryKind.SLICE

    def _address_base(self) -> int:
        """What the address surfaces (box, hex dump, status text) count from."""
        return 0 if self._addresses_are_view_relative() else self._anchor_base()

    def _tilemap_address_base(self) -> int:
        """:meth:`_address_base` for a tilemap document's own cells."""
        assert self._doc is not None
        if self._addresses_are_view_relative():
            return 0
        return self._doc.tilemap_anchor_base

    def _tile_anchor_offset(self, tile: int) -> int:
        """``tile``'s byte offset on the current (nudged) grid, in the coordinates
        an offset is written down in (:meth:`_anchor_base`)."""
        assert self._doc is not None
        return self._anchor_base() + self._nudge + tile * self._doc.bytes_per_tile

    def _tile_address(self, tile: int) -> int:
        """``tile``'s byte offset on the current (nudged) grid, as displayed."""
        assert self._doc is not None
        return self._address_base() + self._nudge + tile * self._doc.bytes_per_tile

    def _offset_text(self) -> str:
        """The current byte offset rendered in the chosen address format.

        Also the offset box's ``current_text`` provider - it re-renders from this on
        every commit, so it must be safe to call with no document loaded.
        """
        if self._doc is None:
            return ""
        return self._format_offset(self._tile_address(self._offset))

    def _jump_to_address(self, byte_off: int) -> None:
        """Jump to an address the box shows - its commit handler.

        Byte-exact: a sub-tile address sets the byte nudge, so typing any offset
        lands the grid on it. The box re-renders itself from :meth:`_offset_text`
        after this, so there's no text handling to do here; an out-of-range value
        is clamped by _set_byte_position.
        """
        if self._doc is None:
            return
        self._set_byte_position(byte_off - self._address_base())

    def _sync_nav(self) -> None:
        """Mirror the current position into the address box and the tile-offset
        bar, and re-scale the bar to the file."""
        has_doc = self._doc is not None
        self._address_edit.setEnabled(has_doc)
        self._tile_offset_bar.setEnabled(has_doc)
        if not has_doc:
            self._address_edit.clear()
            self._nudge_info.clear()
            return

        cols, rows = self._columns.value(), self._view_rows()
        # Don't overwrite what the user is mid-way through typing; a commit re-renders
        # the box itself (CommittingLineEdit.commit), so this guard is safe.
        if not self._address_edit.hasFocus():
            self._address_edit.refresh()
        self._nudge_info.setText(f"+{self._nudge} B" if self._nudge else "")

        # Scrollbar spans the whole file: value = offset, page = one window of tiles,
        # so the handle size reflects how much of the file is on screen.
        page = max(1, cols) * max(1, rows)
        max_off = self._doc.clamp_tile_offset(
            self._doc.tile_count, cols, rows, self._nudge
        )
        bar = self._tile_offset_bar
        with signals_blocked(bar):  # setValue here must not re-enter _set_offset
            bar.setEnabled(max_off > 0)
            bar.setRange(0, max_off)
            bar.setSingleStep(cols * self._row_step())  # one (block-)row
            bar.setPageStep(page)
            bar.setValue(self._offset)

    def _land_on_byte(self, file_offset: int) -> None:
        """Move the view origin to an absolute file byte, without pushing an undo.

        Takes an offset as it was *written down* (:meth:`_anchor_base`), not as
        the box shows it: its caller is Jump to Source, handing over a child's
        stored ``slice_offset``. Same byte→tile/nudge split as
        :meth:`_set_byte_position`, but it applies the position directly - a
        jump-to-source is navigation, not an edit.
        """
        if self._doc is None:
            return
        pos = file_offset - self._anchor_base()
        offset, nudge = self._doc.clamp_byte_position(
            pos, self._columns.value(), self._view_rows()
        )
        self._apply_offset(offset, nudge)
