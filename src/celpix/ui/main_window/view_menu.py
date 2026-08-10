"""The View menu: how the pixels are drawn, and how much of them.

Everything under View, as opposed to Navigate: this menu changes the *reading* of
what is on screen, while Navigate moves the window through the file. Four
questions, in the order the menu asks them.

**The annotations laid over the art** - the grid and its style, and the cell
labels a tilemap can carry. Both are drawn in the grid's own colour and neither
is part of the picture, which is why the labels sit with the grid rather than
with the tilemap controls.

**How much, and how large** - View ▸ Entire File takes the height off the row
window so a file arrives in one piece, and the zoom rows are the keyboard route
to the zoom spin. They are grouped because they answer one question between them:
how much of this am I looking at, and at what size.

**The second readings** - the animation player and the text window, each a
``Qt.Tool`` holding another view of the entry on screen. Commands rather than
checkable toggles, because the window the user closes from its own frame already
holds that answer and a checkbox here would be a second one.

**The frame around the picture** - the app's light/dark theme, below the
separator because it is the one thing here that does not change how the pixels
are drawn. The canvas looks the same in both themes by design.

All of it is a **local preference**, stored in ``QSettings`` under the keys below
and shared by every project: how you want to look at pixels is a property of the
person looking, and carrying it in the ``.celpix`` would mean opening someone
else's project rearranged your view.

What is not here: the window arrangement, which is Panels' and
:mod:`celpix.ui.window_layout`'s; the interpretation bars' own controls, which
state what the bytes *are* rather than how they are shown
(:mod:`~celpix.ui.main_window.interpretation`); and the render itself
(:mod:`~celpix.ui.main_window.rendering`).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QAction,
    QKeySequence,
)

from celpix.core.capabilities import Capability
from celpix.core.document import GridMode
from celpix.ui.canvas import GridStyle
from celpix.ui.main_window.interpretation import (
    ROWS_LOCKED_TIP,
    ROWS_TIP,
    ROWS_WHOLE_TIP,
)
from celpix.ui.theme import THEME_KEY, Theme, apply_theme
from celpix.ui.widgets import (
    add_enum_action_group,
    load_bool_setting,
    load_enum_setting,
    make_action,
    save_bool_setting,
    save_enum_setting,
)

# QSettings keys for the grid. All four parts of it are **local preferences**,
# not project state: how you want to look at pixels is a property of the person
# looking, and carrying it in the .celpix would mean opening someone else's
# project rearranged your view.
GRID_STYLE_KEY = "view/grid_style"
GRID_SHOWN_KEY = "view/grid_shown"
GRID_SCALE_KEY = "view/grid_scale"
BLOCK_GRID_KEY = "view/block_grid"
# View ▸ Entire File, a local preference for the same reason: how much of a file
# you want in front of you belongs to the person looking, not to the project.
ENTIRE_FILE_KEY = "view/entire_file"
# View ▸ Show Tile IDs, likewise: an annotation you turn on to read a map and off
# again to look at it, which is about the reader and not about the map.
TILE_IDS_KEY = "view/tile_ids"


class ViewMenuMixin:
    """The View menu and the display preferences behind it.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _build_view_menu(self) -> None:
        """View ▸ display toggles that change how the pixels are drawn (as
        opposed to Navigate, which moves the window): the grid level, the
        app-wide grid style, how much of the file is shown and how big, and the
        app's own light/dark look."""
        menu = self.menuBar().addMenu("&View")
        self._build_grid_action(menu)
        self._build_grid_style_menu(menu)
        # With the grid because it is the same kind of thing: an annotation laid
        # over the art in the grid's own colour, not part of the picture.
        self._build_tile_ids_action(menu)
        menu.addSeparator()
        # Grouped with the zoom: both answer "how much of this am I looking at,
        # and how large" - Entire File sizes the window, zoom sizes the pixels.
        self._build_entire_file_action(menu)
        self._build_zoom_actions(menu)
        menu.addSeparator()
        self._build_animation_action(menu)
        self._build_text_action(menu)
        self._build_font_alphabet_action(menu)
        menu.addSeparator()
        self._build_theme_menu(menu)

    def _build_tile_ids_action(self, view_menu) -> None:  # noqa: ANN001 - QMenu
        """View ▸ Show Tile IDs — number each tilemap cell with the tile it names.

        The question a tilemap view cannot otherwise answer. A cell's picture is
        the tile's, so nothing on screen says *which* tile it is, and that number
        is what you carry to the Base tile spin, to a hex editor, or to a bank
        listing. Drawn in hex with the ``$`` the tilemap controls use.

        A toggle rather than always-on: a number over every cell of a 32x32 screen
        is a lot of ink for something wanted in bursts. Gated to tilemaps by
        ``CELL_LABELS`` — a pixel tile has no name to show, only a position, which
        the position bar already gives (``docs/design/tilemap-entry.md`` §8).

        No shortcut, for the reason Entire File has none: every bare letter near
        the view is already navigation.

        Mnemonic "D": "T" belongs to Theme and "G"/"B"/"S" to the three grid
        entries this sits with.
        """
        self._show_tile_ids_action = QAction("Show Tile I&Ds", self, checkable=True)
        self._show_tile_ids_action.setToolTip(
            "Number each cell with the tile it names, in hex\n"
            "The file's own number, before Base tile is applied"
        )
        self._show_tile_ids = load_bool_setting(TILE_IDS_KEY, False)
        self._show_tile_ids_action.setChecked(self._show_tile_ids)
        self._show_tile_ids_action.toggled.connect(self._on_show_tile_ids_change)
        view_menu.addAction(self._show_tile_ids_action)

    def _on_show_tile_ids_change(self, on: bool) -> None:
        save_bool_setting(TILE_IDS_KEY, on)
        self._show_tile_ids = on
        if self._doc is not None:
            self._refresh_view()

    def _build_animation_action(self, view_menu) -> None:  # noqa: ANN001 - QMenu
        """View ▸ Animation - open the player for this object's sequences.

        A command rather than a checkable toggle, and the one thing that makes it
        different from the docks in Panels: the window it opens is a `Qt.Tool`
        that the user closes from its own frame, so a checkbox here would be a
        second answer to a question the window itself already holds.

        Enabled only where there is something to play, which is sharper than the
        content kind (:meth:`~...animation.AnimationMixin._animation_available`):
        every sprite object has a table and most of its sequences are empty.

        Mnemonic "A": free among the View entries, and the word's own first
        letter.
        """
        self._animation_action = QAction("&Animation...", self)
        self._animation_action.setToolTip(
            "Play this sprite object's animation sequences\n"
            "Its own window, with its own zoom"
        )
        self._animation_action.triggered.connect(self._show_animation)
        self._animation_action.setEnabled(False)
        view_menu.addAction(self._animation_action)

    def _build_text_action(self, view_menu) -> None:  # noqa: ANN001 - QMenu
        """View ▸ Text - open the string this fontmap holds, as words.

        Beside Animation because it is the same kind of thing: a `Qt.Tool` window
        holding a second reading of the entry on screen, closed from its own
        frame rather than by a checkbox here.

        Enabled on the **declaration** and not on the alphabet, unlike its
        neighbour: a sprite object with no sequences has nothing to play, but a
        fontmap with no font bound has something to say - that its codes mean
        nothing yet, and where to fix it
        (:meth:`~...text.TextMixin._text_available`).

        Mnemonic "x": Theme has the word's own first letter and Entire File its
        second, so the third is what is left - and it is the letter of the word
        that stands out.
        """
        self._text_action = QAction("Te&xt...", self)
        self._text_action.setToolTip(
            "Read and edit this text run as words\n"
            "Its own window, typed through the font's alphabet"
        )
        self._text_action.triggered.connect(self._show_text)
        self._text_action.setEnabled(False)
        view_menu.addAction(self._text_action)

    def _build_font_alphabet_action(self, view_menu) -> None:  # noqa: ANN001 - QMenu
        """View ▸ Font Alphabet - say what this font's tiles spell.

        Directly after Text, because the two are one gesture split in half: the
        string is read through the alphabet, and the alphabet is only judged
        against the string. Both windows open on a fontmap and sit side by side.

        Enabled wherever there is a **font** to write to - a bound one under a
        fontmap, or a sheet ticked Use as Font - and not on there being a table
        already, since the empty table is the thing it exists to fill in
        (:meth:`~...font_alphabet.FontAlphabetMixin._font_alphabet_available`).
        """
        self._font_alphabet_action = QAction("&Font Alphabet...", self)
        self._font_alphabet_action.setToolTip(
            "Say which character each of this font's tiles draws\n"
            "Its own window, beside the text it is read against"
        )
        self._font_alphabet_action.triggered.connect(self._show_font_alphabet)
        self._font_alphabet_action.setEnabled(False)
        view_menu.addAction(self._font_alphabet_action)

    def _build_entire_file_action(self, view_menu) -> None:  # noqa: ANN001 - QMenu
        """View ▸ Entire File - drop the row window and show all of it at once.

        A file is normally viewed through a fixed window of Rows tile-rows that
        the offset pages through (``docs/design/overview.md`` §4). This takes the
        height off that setting: whenever Rows would cut the file short the window
        grows to every row the data fills, which leaves the offset nowhere to move
        and puts the file on screen in one piece. A file already shorter than Rows
        is unaffected - it was never being limited.

        Rows is locked while this is on (:meth:`_sync_entire_file`) rather than
        overwritten, so the number the user chose is still there, and still
        theirs, when the toggle goes off.

        No shortcut: it is a mode you settle into per file, not something worth a
        key, and every bare letter near the view is already navigation.
        """
        self._entire_file = QAction("&Entire File", self, checkable=True)
        self._entire_file.setToolTip(
            "Show the whole file at once, ignoring Rows\n"
            "Every row redecodes on each redraw - slow on big files"
        )
        self._entire_file.setChecked(load_bool_setting(ENTIRE_FILE_KEY, False))
        self._entire_file.toggled.connect(self._on_entire_file_change)
        view_menu.addAction(self._entire_file)

    def _on_entire_file_change(self) -> None:
        """Persist the toggle, lock/release Rows, and re-render at the new height.

        Leaving the mode takes the window back down to Rows, which would strand
        it at the file's start - so it re-anchors on whatever tile the user
        picked out of the full view (:meth:`_snap_offset_to_selection`).
        """
        save_bool_setting(ENTIRE_FILE_KEY, self._entire_file.isChecked())
        self._sync_entire_file()
        if not self._entire_file.isChecked():
            self._snap_offset_to_selection()
        self._on_view_change()

    def _sync_entire_file(self) -> None:
        """Lock the Rows control (and its caption) to match the toggle.

        Also called once during construction, because the menus are built before
        the toolbar: the action is restored from settings before the spin it
        governs exists, so the lock can only be applied afterwards.

        The caption goes with the spin - it is half the control's hover target
        (:func:`~celpix.ui.widgets.add_labelled`), so a live-looking label over a
        dead input is exactly where the "why can't I type here" lands.
        """
        # Two ways to have no row count to set. View > Entire File is the
        # temporary one; a tilemap is the permanent one — it is always shown
        # entire, so a window height is not a setting it has. Asking the
        # capability table keeps that second rule stated once
        # (``docs/design/tilemap-entry.md`` §4) instead of here.
        windowed = self._can(Capability.NAVIGATION)
        entire = self._entire_file.isChecked()
        usable = windowed and not entire
        self._rows.setEnabled(usable)
        self._rows_label.setEnabled(usable)
        tip = ROWS_TIP if usable else (ROWS_LOCKED_TIP if windowed else ROWS_WHOLE_TIP)
        self._rows.setToolTip(tip)
        self._rows_label.setToolTip(tip)

    def _build_theme_menu(self, view_menu) -> None:  # noqa: ANN001 - QMenu
        """View ▸ Theme - the app's light/dark appearance.

        Under View because it *is* a view setting, but below the separator with
        the rest: everything above changes how the pixels are drawn, this changes
        the frame around them. The canvas itself looks the same in both themes by
        design - its backing gray, the grid and the selection outline are fixed
        colors so the art reads identically whichever theme is on.

        A local preference like the grid's, applied to the running application
        the moment it is chosen (:func:`~celpix.ui.theme.apply_theme`).
        """
        submenu = view_menu.addMenu("&Theme")
        self._theme_group, _ = add_enum_action_group(
            self,
            submenu,
            ((Theme.LIGHT, "&Light", ""), (Theme.DARK, "&Dark", "")),
            load_enum_setting(THEME_KEY, Theme.LIGHT),
            self._on_theme_change,
        )

    def _on_theme_change(self, action: QAction) -> None:
        """Persist the chosen theme and put it on immediately.

        Reconstructed through ``Theme(...)`` because an action's data makes a
        round trip through QVariant, which hands a str-valued enum back as the
        bare string.
        """
        theme = Theme(action.data())
        save_enum_setting(THEME_KEY, theme)
        apply_theme(theme)
        # Qt re-polishes every widget against the new palette, which covers the
        # whole window bar two kinds of thing. The file-position rail is a
        # *stylesheet*, and its accent was written into that string as a literal
        # when the bar was built; only regenerating it re-reads the palette.
        self._tile_offset_bar.setStyleSheet(self._tile_offset_bar_style())
        # And the painted icons are pixmaps baked in the old text color - a
        # re-polish repaints the button around them, not the art inside.
        self._rebake_icons()

    def _rebake_icons(self) -> None:
        """Re-paint the window's own painted icons against the live palette.

        The two buttons whose art is a pixmap this window painted rather than a
        icon the style draws: the codec filter's funnel and the tilemap bar's
        jump. Called on a theme switch rather than from a ``changeEvent`` like the
        panels that own their own icons - Qt sends a burst of PaletteChange during
        construction, before these widgets exist, and a window-level handler would
        have to guard against its own half-built state.
        """
        self._bake_pixel_filter_icon()
        self._bake_binding_jump_icon()

    def _build_grid_action(self, view_menu) -> None:  # noqa: ANN001 - QMenu
        """View ▸ Grid - the on/off switch, over everything Grid Style configures.

        A plain checkable action: what the grid *is* — its scale, its structure,
        its line style — is one menu down, so this stays the single question
        worth a key. Display-only shortcut, like Palette ▸ Load from Selection:
        the bare "G" is routed by the app-wide event filter (_handle_nav_key),
        which yields to focused text inputs - a live shortcut here would steal it
        from them.
        """
        self._grid = make_action(
            self,
            "&Grid",
            self._on_grid_change,
            menu=view_menu,
            tip="Overlay a grid (zoom >= 2)",
            shortcut=QKeySequence("G"),
            context=Qt.ShortcutContext.WidgetShortcut,
            checkable=True,
            checked=load_bool_setting(GRID_SHOWN_KEY, False),
        )

    def _grid_mode(self) -> GridMode:
        """The checked grid scale.

        Through ``parse`` because an action's data makes a round trip through
        QVariant, which hands a str-valued enum back as the bare string.
        """
        checked = self._grid_mode_group.checkedAction()
        return GridMode.parse(checked.data() if checked else None, GridMode.TILE)

    def _on_grid_change(self) -> None:
        """Persist the menu's grid as a local preference, and redraw with it.

        The canvas is told directly rather than only through the view refresh,
        because the grid can be changed with no entry open at all - the refresh
        below does nothing then.
        """
        show, mode, block_grid = self._grid_settings()
        save_bool_setting(GRID_SHOWN_KEY, show)
        save_enum_setting(GRID_SCALE_KEY, mode)
        save_bool_setting(BLOCK_GRID_KEY, block_grid)
        self._canvas.set_grid(show, mode, block_grid)
        self._on_view_change()

    def _grid_settings(self) -> tuple[bool, GridMode, bool]:
        """The grid as the menu has it, in the order every canvas takes it."""
        return self._grid.isChecked(), self._grid_mode(), self._block_grid.isChecked()

    def _build_zoom_actions(self, view_menu) -> None:  # noqa: ANN001 - QMenu
        """View ▸ Zoom In / Zoom Out - the keyboard route to the zoom spin.

        Real shortcuts (not the event-filter kind the bare-key nav uses): the
        bare +/- are already the byte nudge, so zoom takes the platform's standard
        Ctrl combos, which nothing routes through the nav map. Ctrl+= joins Zoom
        In because the standard Ctrl++ needs Shift on most layouts.

        The wheel gesture is what the entries advertise, written into the label
        after a tab (the Navigate menu's idiom) because no QKeySequence can
        express a scroll direction. Qt renders that tab text *instead* of the
        registered shortcut, so the Ctrl combos still fire - they just aren't the
        thing in the shortcut column, and the tooltip names them so they stay
        discoverable.
        """
        zoom_in = QAction("Zoom &In\tCtrl + Scroll Up", self)
        sequences = QKeySequence.keyBindings(QKeySequence.StandardKey.ZoomIn)
        sequences.append(QKeySequence("Ctrl+="))
        zoom_in.setShortcuts(sequences)
        zoom_in.setToolTip("Zoom in (Ctrl++)")
        zoom_in.triggered.connect(lambda: self._zoom_steps(1))
        view_menu.addAction(zoom_in)
        zoom_out = QAction("Zoom &Out\tCtrl + Scroll Down", self)
        zoom_out.setShortcut(QKeySequence.StandardKey.ZoomOut)
        zoom_out.setToolTip("Zoom out (Ctrl+-)")
        zoom_out.triggered.connect(lambda: self._zoom_steps(-1))
        view_menu.addAction(zoom_out)

    def _build_grid_style_menu(self, view_menu) -> None:  # noqa: ANN001 - QMenu
        """View ▸ Grid Style - everything about the grid except whether it shows.

        Three sections, because they answer three different questions and two of
        them are radio groups that would otherwise run together: **Style** is the
        line itself (Point/Dot/Dash/Line), **Scale** is what the
        fine lines count, **Blocks** is what the strong ones do. Shift+G cycles
        the style, on the same event-filter routing as the bare G that switches
        the whole grid on.

        All of it is a local preference in QSettings, remembered across launches
        and shared by every project (see the keys above).
        """
        submenu = view_menu.addMenu("Grid &Style\tShift+G")

        submenu.addSection("Style")
        style = load_enum_setting(GRID_STYLE_KEY, GridStyle.LINE)
        self._apply_grid_style(style)
        self._grid_style_group, _ = add_enum_action_group(
            self,
            submenu,
            (
                (GridStyle.POINT, "&Point", ""),
                (GridStyle.DOT, "&Dot", ""),
                (GridStyle.DASH, "D&ash", ""),
                (GridStyle.LINE, "&Line", ""),
            ),
            style,
            self._on_grid_style_change,
        )

        submenu.addSection("Scale")
        self._grid_mode_group, self._grid_actions = add_enum_action_group(
            self,
            submenu,
            (
                (
                    GridMode.TILE,
                    "&Tile",
                    "Grey lines on every tile, blue every\n8 tiles",
                ),
                (
                    GridMode.PIXEL,
                    "Pi&xel",
                    "Grey lines on every pixel, blue on every tile\n"
                    "(needs a high zoom)",
                ),
            ),
            load_enum_setting(GRID_SCALE_KEY, GridMode.TILE),
            self._on_grid_change,
        )

        submenu.addSection("Blocks")
        # Not part of either group: it re-scales the strong level rather than
        # being a scale of its own, so it is on or off beside any of them.
        self._block_grid = QAction("&Block Grid", self, checkable=True)
        self._block_grid.setToolTip(
            "Put the blue lines on the arrangement's Block W×H\n"
            "instead of the default 8-tile square"
        )
        self._block_grid.setChecked(load_bool_setting(BLOCK_GRID_KEY, False))
        self._block_grid.toggled.connect(self._on_grid_change)
        submenu.addAction(self._block_grid)
        # Every part is on the canvas before the first render, since the menu is
        # built from the stored preferences rather than the canvas's defaults.
        self._canvas.set_grid(*self._grid_settings())

    def _on_grid_style_change(self, action: QAction) -> None:
        style = action.data()
        save_enum_setting(GRID_STYLE_KEY, style)
        self._apply_grid_style(style)

    def _apply_grid_style(self, style: GridStyle) -> None:
        """Show ``style`` on every canvas - the main one and the preview overlay.

        The style is app-wide, so the decompression preview has to follow it too:
        the whole point of that window is to look like the view would with the
        bytes unpacked.
        """
        self._canvas.set_grid_style(style)
        self._overlay.set_grid_style(style)

    def _cycle_grid_style(self) -> None:
        """Shift+G: step the app-wide grid style on, in the menu's own order."""
        actions = self._grid_style_group.actions()
        checked = self._grid_style_group.checkedAction()
        following = (
            actions[(actions.index(checked) + 1) % len(actions)]
            if checked
            else actions[0]
        )
        following.setChecked(True)
        self._on_grid_style_change(following)
