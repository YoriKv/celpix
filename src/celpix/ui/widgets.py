"""Small reusable UI widgets and shared painting idioms.

Qt lives here (this is the ``ui`` layer); the model stays Qt-free.

Three families that grew here have modules of their own — the pan/zoom surface
and its scroll-area wiring (:mod:`celpix.ui.panzoom`), the preference store
(:mod:`celpix.ui.settings`) and the toolbar overflow button
(:mod:`celpix.ui.toolbar_overflow`) — and are re-exported from this module, so
an import of one of them from here keeps working.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QRect,
    QSize,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QDesktopServices,
    QKeySequence,
    QPainter,
    QPen,
    QValidator,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from celpix.core import ceil_div
from celpix.core.address import format_hex, parse_hex
from celpix.plugins.base import format_size
from celpix.ui.icon_font import icon_cache_key
from celpix.ui.panzoom import (  # noqa: F401 — re-exported, see the module docs
    ZOOM_LEVELS,
    PanOnlyMouse,
    PanZoomSurface,
    SpacePanFilter,
    ZoomSpinBox,
    mount_surface,
    pan_scroll_area,
    wheel_zoom,
    zoom_anchored,
    zoom_level_after,
    zoom_spin,
)
from celpix.ui.settings import (  # noqa: F401 — re-exported, see the module docs
    MAX_RECENT_PROJECTS,
    RECENT_PROJECTS_KEY,
    _recent_path,
    clear_recent_projects,
    forget_recent_project,
    load_bool_setting,
    load_enum_setting,
    load_float_setting,
    load_recent_projects,
    load_setting,
    remember_recent_project,
    save_bool_setting,
    save_enum_setting,
    save_float_setting,
    settings,
)
from celpix.ui.theme import ERROR_INK, GRID_STRUCTURE_COLOR, WARNING_INK, set_ink
from celpix.ui.toolbar_overflow import (  # noqa: F401 — re-exported, see the module docs
    ToolBarOverflow,
)

_SpinT = TypeVar("_SpinT", bound=QSpinBox)
_DialogT = TypeVar("_DialogT", bound=QDialog)
_ResultT = TypeVar("_ResultT")

# The canvas editing shortcuts (Cut/Copy/Paste/Select All/Delete). The main
# window binds these window-wide (see ``SelectionMixin``), so they otherwise fire
# wherever focus is - acting on the *canvas* selection even when a side panel has
# focus. A panel that wants to own its keys claims them with
# :func:`take_editing_shortcut`. Matched by key sequence, so platform bindings
# track without hard-coded literals.
_EDITING_SHORTCUTS = (
    QKeySequence.StandardKey.Cut,
    QKeySequence.StandardKey.Copy,
    QKeySequence.StandardKey.Paste,
    QKeySequence.StandardKey.SelectAll,
    QKeySequence.StandardKey.Delete,
)


def take_shortcuts(event: QEvent, keys: Iterable[QKeySequence.StandardKey]) -> bool:
    """Claim ``keys`` off the window-wide shortcuts; call from ``event()``.

    Returns True (having *accepted* ``event``) when it is a ``ShortcutOverride``
    for one of ``keys``. Accepting the override says the focused widget wants
    the key as an ordinary keystroke, so no shortcut runs anywhere and the press
    arrives at the widget's ``keyPressEvent`` to be answered once. That matters
    twice over: a panel shadowed by the main window's editing keys, and a
    ``Qt.Tool`` window — Qt keeps its parent's window shortcuts alive while it is
    active, so a key both bind is a tie, and Qt's answer to a tie is to fire
    *neither*. Matched by key sequence, so platform bindings track without
    hard-coded literals.
    """
    if event.type() == QEvent.Type.ShortcutOverride and any(
        event.matches(key) for key in keys
    ):
        event.accept()
        return True
    return False


def take_editing_shortcut(event: QEvent) -> bool:
    """Claim a canvas editing shortcut for a focused panel; call from ``event()``.

    :func:`take_shortcuts` over :data:`_EDITING_SHORTCUTS`. Accepting the
    override routes the key to the focused widget as a normal press instead of
    letting the canvas's window-wide shortcut consume (or, for Delete,
    ambiguously drop) it - so a
    panel that has its own key handling isn't shadowed by the editing surface
    behind it. The widget then handles the resulting key press however it likes
    (or ignores it, so the key simply does nothing there). Mirrors how the
    app-wide arrow-key filter yields to these same panels, and how text inputs
    already claim their editing keys natively.

    Usually reached through :class:`ShortcutIsland` rather than called directly;
    it stays public for a widget that has its own ``event()`` to weave it into.
    """
    return take_shortcuts(event, _EDITING_SHORTCUTS)


class ShortcutIsland:
    """Mix in front of a widget to make it own the editing keys while focused.

    The Cut/Copy/Paste/Select All/Delete shortcuts are bound window-wide for the
    canvas, so without this every side panel that wants its own Copy is shadowed
    by the editing surface behind it. Each panel decides for itself what the keys
    then *do* — the palette grid copies a colour, the hex dump copies text, the
    files list deletes an entry — and some are deliberately inert; claiming the
    key is the only part that is the same everywhere, so it is the only part here.

    Mixed in **before** the Qt base (``class Panel(ShortcutIsland, QListWidget)``)
    so this ``event`` is reached first and ``super()`` continues to the widget's
    own.
    """

    def event(self, event: QEvent) -> bool:  # noqa: D102 — Qt override
        if take_editing_shortcut(event):
            return True
        return super().event(event)


def wrap_lines(text: str, width: int = 60) -> str:
    """``text`` hard-wrapped for a tooltip, each of its lines at ``width``.

    Qt never wraps a plain-text tooltip, so a long line runs off the screen
    (``docs/py-qt-reference/pyside6-pitfalls.md``). Text we write is wrapped by
    hand where it is written; this is for text we only *relay* — a stage's
    message, an exception's — which nothing wrapped. Line by line, so the
    paragraph breaks the author put in survive.
    """
    return "\n".join(
        textwrap.fill(line, width) if line.strip() else "" for line in text.split("\n")
    )


def counted(count: int, noun: str) -> str:
    """``count`` with ``noun`` pluralised — ``"1 tile"``, ``"1,024 tiles"``.

    Every "Copied N tiles." / "Cleared N cells." the status bar says goes through
    here, so a count is never grouped in one message and not the next. Naive
    plurals (an ``s``), which is all these nouns need; a noun that pluralises any
    other way would have to be spelled out at the call site.
    """
    return f"{count:,} {noun}" + ("" if count == 1 else "s")


def size_text(count: int) -> str:
    """A byte count in both forms — ``"8 KiB (8,192 bytes)"`` — where they differ.

    The round form is what a size is quoted in and the exact one is what a
    reader checks an offset against, so a payload gets both. A count that is
    not a whole binary multiple already *is* its exact form, and saying it twice
    reads as a bug. Grouped, as every count the app shows is (:func:`counted`).
    """
    exact = counted(count, "byte")
    pretty = format_size(count)
    return exact if pretty.endswith(" bytes") else f"{pretty} ({exact})"


class SyncGuard:
    """A "this change is ours, not the user's" flag that is always put back.

    A widget that writes into its own inputs — restoring a caret, refilling a
    table — has to tell its change handlers to stand down while it does, and
    the hand-written ``flag = True; try: … finally: flag = False`` gets that
    wrong the moment two such writes nest: the inner one clears the flag while
    the outer is still writing. Counted rather than boolean, so nesting holds::

        with self._syncing:
            self._edit.setPlainText(body)
        ...
        if self._syncing:
            return

    Where the thing to silence is a widget's *signals* rather than the owner's
    own handlers, :func:`signals_blocked` is the tool instead.
    """

    def __init__(self) -> None:
        self._depth = 0

    def __enter__(self) -> SyncGuard:
        self._depth += 1
        return self

    def __exit__(self, *_exc: object) -> None:
        self._depth -= 1

    def __bool__(self) -> bool:
        return self._depth > 0


@contextmanager
def signals_blocked(*widgets: QObject) -> Iterator[None]:
    """Set widget state without the handlers firing back.

    The recurring need behind it: restoring a session, applying a preset, or
    correcting a clamped value pushes several widgets at once, and each one's
    ``valueChanged``/``toggled`` would otherwise trigger its own re-render — so
    what should be one coherent swap becomes a cascade of partial reloads (and,
    where a handler writes back, a re-entrant one). The caller re-renders once
    afterwards instead.

    Each widget's *previous* blocked state is restored rather than assumed
    ``False``, so nesting this inside an outer block doesn't unblock early.
    """
    previous = [widget.blockSignals(True) for widget in widgets]
    try:
        yield
    finally:
        for widget, was_blocked in zip(widgets, previous, strict=True):
            widget.blockSignals(was_blocked)


def select_combo_data(combo: QComboBox, data: object) -> None:
    """Select the item carrying ``data``, signals blocked, no-op if absent.

    The one signal-safe combo snap used everywhere a selection is set
    programmatically — session restore, the undo apply-helpers, and every
    load-failed revert. Leaving the selection unchanged when nothing matches is
    deliberate: a plugin refresh can drop a preset out from under a stored id,
    and a bare ``setCurrentIndex(-1)`` would blank the box instead.
    """
    with signals_blocked(combo):
        index = combo.findData(data)
        if index >= 0:
            combo.setCurrentIndex(index)


def select_only(view: QAbstractItemView, item: object | None) -> None:
    """Make ``item`` the current row **and** the whole selection; ``None`` clears both.

    Every programmatic pick goes through here rather than through the view's
    one-argument ``setCurrentItem``, which derives its selection command from
    the **live keyboard modifiers**. Under a held Ctrl that is *Toggle* — and
    the program sets a row from inside key presses that hold one: an undo's
    Ctrl+Z switches the entry back and re-points the Files pane, which would
    add the row beside the old one, or take the row already picked off. The
    explicit command makes the answer the same whatever the user is holding.

    ``item`` is any item-view item the view hands out (tree, table or list); a
    table cell selects its whole row.
    """
    model = view.selectionModel()
    if item is None:
        model.setCurrentIndex(QModelIndex(), QItemSelectionModel.SelectionFlag.Clear)
        return
    model.setCurrentIndex(
        view.indexFromItem(item),
        QItemSelectionModel.SelectionFlag.ClearAndSelect
        | QItemSelectionModel.SelectionFlag.Rows,
    )


def ask_save_path(
    parent: QWidget, title: str, default: str, file_filter: str, suffix: str
) -> str | None:
    """A Save-As dialog whose answer always carries ``suffix``.

    ``None`` when the user cancels. The suffix is appended rather than assumed,
    because a typed name without one is the common case and every export here
    writes exactly one format — so the extension is not the user's decision to
    forget. One helper so no export path silently omits it.

    **An appended name gets its own overwrite question.** The dialog confirmed
    the name the user typed; Qt's own dialog and the Linux ones append nothing,
    so ``mygame`` passed its check as a new file while ``mygame.celpix`` is
    the one about to be replaced. No sends the user back to the dialog, as the
    dialog's own question does.
    """
    while True:
        path, _ = QFileDialog.getSaveFileName(parent, title, default, file_filter)
        if not path:
            return None
        if path.lower().endswith(suffix.lower()):
            return path
        path += suffix
        if not os.path.exists(path):
            return path
        answer = QMessageBox.question(
            parent,
            title,
            f"{Path(path).name} already exists.\nDo you want to replace it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            return path
        if answer != QMessageBox.StandardButton.No:
            return None  # the box closed some other way: nothing was chosen
        default = path


def show_in_file_manager(path: str) -> bool:
    """Reveal ``path`` in the desktop's file manager; False if that failed.

    Windows Explorer and macOS Finder can both *select* the file, which is what
    the gesture is really for - "where did this come from" answered without
    reading a path out of a tooltip. Elsewhere there is no portable select (it
    needs the ``org.freedesktop.FileManager1`` D-Bus service, which plenty of
    sessions don't run), so the containing folder is opened instead. Same
    fallback when the launch itself fails - a stripped container may have no
    ``explorer.exe`` on PATH, and under WSL the Windows binaries may be off.
    """
    target = Path(path)
    folder = target.parent
    if target.exists():
        # Explorer parses its own command line rather than argv, so the whole
        # command goes as one string: "/select,<path>" must stay a single token
        # with the quotes around the path alone. An argv list would quote the
        # token as a unit the moment the path has a space, and explorer then
        # ignores the switch and opens Documents instead - successfully, so the
        # folder fallback below never gets a chance. Native separators too - it
        # opens the user's home rather than the folder for a forward-slash path.
        if sys.platform == "win32" and _spawn(
            f'explorer /select,"{os.path.normpath(target)}"'
        ):
            return True
        if sys.platform == "darwin" and _spawn(["open", "-R", str(target)]):
            return True
    if not folder.is_dir():
        return False
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


def _spawn(command: list[str] | str) -> bool:
    """Start ``command`` detached, False if the program isn't there.

    Detached because the file manager outlives us, and we never read back from
    it: a ``Popen`` we don't wait on would otherwise leave a zombie. A string
    is a pre-quoted Windows command line handed straight to ``CreateProcess``
    (never a shell), for programs that parse their own arguments.
    """
    try:
        subprocess.Popen(  # noqa: S603 - fixed program, no shell
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return False
    return True


def add_labelled(
    layout, text: str, widget: QWidget, tooltip: str, buddy: QWidget | None = None
) -> QLabel:
    """Add ``text`` then ``widget`` to ``layout``, tooltipping *both*.

    A caption is half the hover target of the pair it names and reads as part of
    the same control, so a tooltip that only answers over the input itself is
    missed exactly where people point first. Routing every labelled control
    through here is what keeps that from drifting back apart, so prefer it over
    adding a bare ``QLabel``. The label is also set as the widget's buddy, which
    is what makes a caption mnemonic focus the input.

    ``buddy`` overrides which widget the mnemonic focuses, for the case
    ``widget`` is a *container* holding a tight run of controls rather than one
    input — a container takes no focus, so the mnemonic would land nowhere. Give
    it the input the caption names, and give that input the same ``tooltip``, or
    the caption and its buddy answer differently to a hover a pixel apart.

    Returns the label, for the few callers that later show/hide or restyle it.
    """
    widget.setToolTip(tooltip)
    label = QLabel(text)
    label.setToolTip(tooltip)
    label.setBuddy(buddy if buddy is not None else widget)
    layout.addWidget(label)
    layout.addWidget(widget)
    return label


def add_form_row(
    form: QFormLayout,
    caption: str,
    field: QWidget,
    tooltip: str | None = None,
    buddy: QWidget | None = None,
) -> QLabel:
    """:func:`add_labelled` for a form: one row, its caption tooltipped too.

    ``QFormLayout.addRow(str, …)`` builds the caption itself and gives it no
    tooltip, so the half of the row people point at first answers nothing —
    which every dialog here used to patch up afterwards by walking its fields.
    ``tooltip`` is set on ``field`` when given; otherwise the field's own is
    copied, for a field built with one. ``buddy`` is the mnemonic's target for a
    ``field`` that is a holder of several controls, as in :func:`add_labelled`.
    """
    if tooltip is not None:
        field.setToolTip(tooltip)
    label = QLabel(caption)
    label.setToolTip(field.toolTip())
    label.setBuddy(buddy if buddy is not None else field)
    form.addRow(label, field)
    return label


class IconBaker:
    """Mix in front of a widget that bakes its own icons, to re-bake on time.

    Baked icons hold the palette's ink and the screen's device scale in their
    pixels, so a theme switch or a move to a differently scaled display has to
    re-render them — and both arrive as a ``changeEvent`` storm (a burst of
    PaletteChange on startup alone). Every such widget guards the re-bake on
    :func:`icon_cache_key` the same way, so the guard is here once: any change
    event, and :meth:`_bake_icons` runs only when the key actually moved.

    Mixed in **before** the Qt base (``class Panel(IconBaker, QWidget)``), and
    after any other mixin that supplies :meth:`_bake_icons`, or this one's
    placeholder is the one found. A
    widget calls :meth:`_bake_if_stale` once at the end of its constructor, and
    change events are ignored until it has: one can arrive while the widget is
    still building what :meth:`_bake_icons` paints onto.
    """

    _icon_key: tuple[int, float] | None = None

    def changeEvent(self, event: QEvent) -> None:  # noqa: D102 — Qt override
        super().changeEvent(event)
        if self._icon_key is not None:
            self._bake_if_stale()

    def _bake_if_stale(self) -> bool:
        """Re-bake when the palette or the device scale moved; True if it did."""
        key = icon_cache_key(self)
        if key == self._icon_key:
            return False
        self._icon_key = key
        self._bake_icons()
        return True

    def _bake_icons(self) -> None:
        """Render every icon against the current palette and device scale."""
        raise NotImplementedError


def paint_selection_outline(
    painter: QPainter,
    rect: QRect,
    alpha: int = 255,
    color: QColor | None = None,
    width: int = 1,
) -> None:
    """The app's shared selection outline: a white ring over a black one.

    One outline language for every "this is the active thing" highlight (the
    canvas's tile selection, the palette panel's active palette row). Two 1px
    layers rather than one line: whichever color the art under the edge happens
    to be, the other layer still shows, so the outline never disappears into it.
    Both are fixed colors — the highlight stays put whatever the theme is and
    wherever focus is, because the selection is the state, not the focus;
    ``alpha`` softens both layers together where the ring sits over small art.

    ``color`` replaces the outer layer for a ring that is **not** a selection —
    the tile source panel's mark for what the canvas is pointing at, which shares
    a grid with that panel's own pick and would otherwise read as a second one of
    them. The dark under-layer stays whatever it is, since that is what keeps the
    ring off the art rather than on it.

    The outer layer sits flush on the selected area's boundary and the black one
    just inside it, so the whole band lands *within* ``rect``: an aliased
    ``drawRect`` renders one pixel past its path, hence the -1 insets.
    ``width`` is each layer's, in whatever pixels the painter draws in — a
    surface drawing in physical pixels on a scaled screen passes the ring's
    weight in those (:meth:`~celpix.ui.panzoom.PanZoomSurface._device_width`).
    """
    painter.setBrush(Qt.BrushStyle.NoBrush)
    outer = QColor(color) if color is not None else QColor(255, 255, 255)
    outer.setAlpha(alpha)
    for layer, ink in enumerate((outer, QColor(0, 0, 0, alpha))):
        painter.setPen(QPen(ink, 1))
        for step in range(layer * width, (layer + 1) * width):
            painter.drawRect(rect.adjusted(step, step, -1 - step, -1 - step))


# The width every **format picker** takes: pixel, palette, tilemap, compression
# and arrangement. One number rather than five, because they are one kind of
# control and a toolbar of them reads as a row — which is exactly what deriving
# the width from the content could not give, since the registry decides how long
# the longest preset name is and a plugin can make it longer at any time.
PRESET_COMBO_WIDTH = 160

# The width a **fixed-choice** dropdown takes: a handful of phrases the app wrote
# itself, not a list the registry can grow — spare room, content kind, literal vs
# from-bytes, byte order. Narrower than a format picker because nothing can make
# its longest item longer, and one number so a form of them lines up.
SHORT_COMBO_WIDTH = 120


class CompactComboBox(QComboBox):
    """A combo box whose closed button is a stated width in pixels.

    A stock combo reserves the full width of its longest item, which long preset
    and entry names turn into a lot of dead toolbar space. Asking for the width
    outright is what keeps a row of them **predictable**: measured against the
    live registry, deriving it from the content instead gave five sibling format
    pickers five different widths — 166 to 220 px — decided by whichever preset
    happened to have the longest name, and a picker that is filled per entry
    changed width as the user moved between files.

    Only the *hints* are set, so a layout may still stretch one past its number;
    the popup list is given back the full content width, so entries stay readable
    while choosing. The height stays Qt's own.

    The width is in device-independent pixels and does **not** follow the font:
    a system font much larger than the one these numbers were measured against
    will elide the longest item down to the arrow. That is the trade for
    predictability, and it is what the popup re-widening is there to soften.
    """

    # Emitted when the box loses focus for real — i.e. the user moved on to
    # another widget, not merely opened this box's own popup (which also fires a
    # focus-out, with PopupFocusReason). Lets a screen hold scratch state alive
    # across consecutive selections and drop it the moment focus leaves.
    focus_lost = Signal()

    def __init__(self, width: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._width = width
        # No AdjustToContents: it exists to re-query the hint when the model
        # changes, and the hint no longer depends on the model.

    def focusOutEvent(self, event) -> None:  # Qt override
        super().focusOutEvent(event)
        if self._is_real_focus_loss(event):
            self.focus_lost.emit()

    def _is_real_focus_loss(self, event) -> bool:
        """Whether this focus-out means the user moved on, not "a popup opened".

        A hook rather than a bare check inside :meth:`focusOutEvent`, because a
        subclass with a popup of its own has to widen the exception without
        re-deciding what the rest of the handler does
        (:class:`~celpix.ui.searchable_combo.SearchableComboBox`).
        """
        return event.reason() != Qt.FocusReason.PopupFocusReason

    def _fixed(self, hint: QSize) -> QSize:
        hint.setWidth(self._width)
        return hint

    def sizeHint(self) -> QSize:  # Qt override
        return self._fixed(super().sizeHint())

    def minimumSizeHint(self) -> QSize:  # Qt override
        return self._fixed(super().minimumSizeHint())

    def showPopup(self) -> None:  # Qt override
        # The popup would inherit the narrowed button width; re-widen it to the
        # longest item (plus scrollbar room) so no entry is elided.
        view = self.view()
        scrollbar = view.verticalScrollBar()
        width = view.sizeHintForColumn(0) + scrollbar.sizeHint().width()
        view.setMinimumWidth(max(self.width(), width))
        super().showPopup()


class CommittingLineEdit(QLineEdit):
    """A free-text field that commits on edit-finish and self-normalises.

    Free-text fields that parse into a value (a hex offset, a dec/hex number, a
    palette index) all share one subtle correctness requirement, and one Qt
    gotcha that makes it easy to get wrong:

    - **Commit, don't stream.** The value should apply when the user finishes
      editing (Enter / focus-out), not on every keystroke — otherwise a
      half-typed value fires repeatedly.
    - **Always re-render on commit — even while focused.** An invalid entry must
      revert to the current value, and a valid one must show its *canonical* form
      (e.g. a tile-snapped, ``0x``-prefixed offset). The trap: ``editingFinished``
      fires on Enter *and* on focus-out, but Qt won't fire it again on a
      focus-out whose text is unchanged since the Enter. So if you skip the
      re-render while the field has focus (the usual guard against clobbering
      mid-typing), an invalid value committed with Enter lingers — the later
      focus-out never corrects it. Re-rendering unconditionally here closes that.

    Wiring it up: pass ``parse`` (text → value, or ``None`` when invalid) and
    ``current_text`` (a callable returning the canonical display string for the
    *current* committed state). On a valid commit the widget emits
    :attr:`committed` with the parsed value — the owner applies it (which may
    clamp/transform the underlying state) — and then the widget re-renders from
    ``current_text``, so the box always reflects the true post-commit state.
    """

    committed = Signal(object)  # the parsed value, on a valid commit

    def __init__(
        self,
        parse: Callable[[str], object | None],
        current_text: Callable[[], str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._parse = parse
        self._current_text = current_text
        self.editingFinished.connect(self.commit)

    def refresh(self) -> None:
        """Set the displayed text to the canonical current value."""
        self.setText(self._current_text())

    def commit(self) -> None:
        """Parse the text; emit :attr:`committed` if valid, then always re-render.

        Unconditional re-render is the point — see the class docstring: it reverts
        invalid input and normalises valid input regardless of focus.
        """
        value = self._parse(self.text())
        if value is not None:
            self.committed.emit(value)
        self.refresh()


@dataclass(frozen=True)
class Badge:
    """A tool window's status-bar annotation: the state its picture cannot show.

    Both windows that carry one have the same problem — a picture that looks
    equally plausible whether or not something is wrong with it, so the something
    has to be said in words. A decompression that stopped early looks like a
    decompression that finished; an animation whose steps name frames the file
    does not have plays as one that does not.

    ``text`` is the few words shown; ``detail`` the tooltip's fuller explanation
    (hard-wrapped by the caller — see the tooltip rule in
    ``docs/py-qt-reference/pyside6-pitfalls.md``); ``warning`` picks amber over
    the standard text colour. That last is the difference between a **problem**
    and a **fact**: a scheme with an end marker that did not reach one *was* cut
    short, while a stream-based one was only ever going to decode as far as it was
    fed, and neither reading applies to the other.
    """

    text: str
    detail: str
    warning: bool = False


def apply_badge(label: QLabel, badge: Badge | None) -> None:
    """Put ``badge`` on ``label``, or empty and hide it when there is none.

    The three writes every badge needs kept in one place, since a window that set
    the text and forgot the ink would carry the last badge's colour into this
    one's words.
    """
    label.setText(badge.text if badge else "")
    label.setToolTip(badge.detail if badge else "")
    set_ink(label, WARNING_INK if badge and badge.warning else None)
    label.setVisible(badge is not None)


def confirm_destructive(
    parent: QWidget,
    title: str,
    text: str,
    safe_label: str,
    proceed_label: str,
    safe: Callable[[], bool],
    *,
    default_safe: bool = False,
) -> bool:
    """Ask before discarding unsaved work; True when the caller may go ahead.

    Every "this throws away edits" gate in the app asks the same three-way
    question, and the middle answer is why it is not a Yes/No: deal with the work
    first (``safe_label`` — Write, Save Project), go ahead without dealing with it
    (``proceed_label``), or call the whole action off. Only the wording differs
    between them, because what is lost differs — a project save keeps edited
    bytes in memory, quitting drops them.

    ``safe`` performs the safe action **and reports whether it actually resolved
    the work**. That return is the part a hand-rolled copy of this gets wrong: a
    write that failed, or a save whose own nested gate was cancelled, leaves the
    work exactly as unsaved as before, so the caller must not proceed past it
    either. ``default_safe`` puts the focus ring on that button, for the paths
    where Enter hit blind should take the least-lossy answer.
    """
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    accept = box.addButton(safe_label, QMessageBox.ButtonRole.AcceptRole)
    box.addButton(proceed_label, QMessageBox.ButtonRole.DestructiveRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    if default_safe:
        box.setDefaultButton(accept)
    box.exec()
    if box.clickedButton() is cancel:
        return False
    return safe() if box.clickedButton() is accept else True


def dialog_buttons(
    dialog: QDialog,
    layout=None,  # noqa: ANN001 — QFormLayout | QBoxLayout | None
    on_accept: Callable[[], None] | None = None,
    *,
    close_only: bool = False,
) -> QDialogButtonBox:
    """A dialog's button row — OK/Cancel, or Close — wired and placed.

    ``on_accept`` is what OK runs: a validating dialog's check, which calls
    ``accept()`` itself once the fields hold up; left out, OK accepts outright.
    ``close_only`` is the single Close button of a dialog that only shows
    something. A Close button carries the *reject* role, so only ``rejected``
    is connected there — an ``accepted`` connection on it could never fire.

    Added to ``layout`` as a whole row of a form, or as the next widget of a
    box; ``None`` leaves the placing to a caller that builds its layout later.
    """
    if close_only:
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    else:
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(on_accept if on_accept is not None else dialog.accept)
    buttons.rejected.connect(dialog.reject)
    if isinstance(layout, QFormLayout):
        layout.addRow(buttons)
    elif layout is not None:
        layout.addWidget(buttons)
    return buttons


class ErrorLabel(QLabel):
    """The line a validating dialog says "why OK did nothing" on.

    Hidden until :meth:`fail`, in the error ink. Connect the edits of the fields
    it can complain about to :meth:`dismiss`, so a message about a value the
    user has since fixed does not stay up beside the fixed value — the dialog
    stays open on a failed OK, so a stale line would read as a live complaint.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        set_ink(self, ERROR_INK)
        self.hide()

    def fail(self, message: str) -> None:
        """Show ``message``."""
        self.setText(message)
        self.show()

    def dismiss(self, *_args: object) -> None:
        """Take the message down — a field it may be about was edited.

        Not named ``clear``: that is ``QLabel``'s own, which only empties the
        text and leaves the (now blank) row showing.
        """
        self.hide()


def run_modal(
    dialog: _DialogT, result: Callable[[_DialogT], _ResultT]
) -> _ResultT | None:
    """Run ``dialog`` modally; ``result(dialog)`` once OK'd, ``None`` otherwise.

    Every ``get_*``/``ask`` runner is this one line around its own dialog, and
    reading the answer only on Accepted is the part worth having once: a dialog
    cancelled after a failed validation may still hold half an answer.
    """
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return result(dialog)


def grid_slot_at(
    x_px: float,
    y_px: float,
    cell: tuple[float, float],
    columns: int,
    count: int,
    *,
    clamp: bool = False,
) -> int | None:
    """Which slot of a ``columns``-wide lattice of ``cell``-sized boxes a point is in.

    The two grids of addressable squares — the palette's swatches and the tile
    source sheet — ask this the same way and would answer it differently if each
    kept its own arithmetic; a *slot* is the position in the panel's own list,
    which is what both of them then look a colour or a tile ID up by.

    ``clamp`` picks the reading. Off, a point past the last column or past the
    end reads as ``None``: a click on the empty tail of a short final row landed
    on nothing. On, it snaps to the nearest slot instead, which is what a **drag**
    wants — running off an edge should keep the selection following the pointer
    rather than dropping it. ``None`` then means only that there is nothing on
    show at all.
    """
    cell_w, cell_h = cell
    # A cell is fractional wherever a non-square pixel is in force
    # (:mod:`celpix.core.aspect`), so the floor division is taken back to an int
    # here rather than at each call site — a slot indexes a list.
    col, row = int(int(x_px) // cell_w), int(int(y_px) // cell_h)
    if not clamp:
        slot = row * columns + col
        return slot if 0 <= col < columns and 0 <= slot < count else None
    if count <= 0:
        return None
    col = min(max(col, 0), columns - 1)
    row = min(max(row, 0), ceil_div(count, columns) - 1)
    # Past the last entry (the empty tail of a short final row) lands on the
    # last entry — dragging off the end selects the end.
    return min(row * columns + col, count - 1)


#: The keys that step a pick across a grid of squares (:func:`grid_step`).
GRID_ARROWS = (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down)


def grid_step(
    current: int | None, key: int, columns: int, count: int, *, start: int = 0
) -> int | None:
    """The slot an arrow key moves a pick to, or ``None`` to stay put.

    The palette's swatches and the tile source sheet step the same way:
    Left/Right by one square, crossing display rows, and Up/Down by one row. A
    step off the top or the bottom stays put rather than being clamped to a
    corner, which would change the column under the user; Left/Right clamp to
    the ends. ``start`` is where a grid with nothing picked steps from. ``key``
    is one of :data:`GRID_ARROWS` — the caller has already asked.
    """
    delta = {
        Qt.Key.Key_Left: -1,
        Qt.Key.Key_Right: 1,
        Qt.Key.Key_Up: -columns,
        Qt.Key.Key_Down: columns,
    }[key]
    target = (current if current is not None else start) + delta
    if abs(delta) == columns and not 0 <= target < count:
        return None
    return min(max(0, target), count - 1)


def paint_pick_ring(painter: QPainter, rect: QRect, width: int = 1) -> None:
    """A panel's own pick: the selection outline, inset a pixel and a touch soft.

    Inset so a square that is also *marked* (:func:`paint_mark_ring`) still
    reads as two rings rather than one thick one, and soft so it does not
    overpower the small art it sits on. ``width`` as
    :func:`paint_selection_outline` takes it, the inset with it.
    """
    paint_selection_outline(
        painter, rect.adjusted(width, width, -width, -width), alpha=230, width=width
    )


def paint_mark_ring(painter: QPainter, rect: QRect, width: int = 1) -> None:
    """What the canvas is pointing at, on a panel: the outline in structural blue.

    White is where the user pointed; blue is what that resolved to — the colour
    that marks structure rather than choice everywhere else, so the ring is not
    read as a second pick. ``width`` as :func:`paint_selection_outline` takes it.
    """
    paint_selection_outline(painter, rect, color=GRID_STRUCTURE_COLOR, width=width)


def value_spin(low: int, high: int, value: int, on_change) -> QSpinBox:  # noqa: ANN001
    """A plain integer spin that commits on finish rather than per keystroke.

    The view settings are all spins of this shape, and the keyboard tracking is
    the part worth having in one place: with it left on, typing a multi-digit
    value re-renders (and re-clamps) once per character, so "16" passes through
    "1" first.
    """
    spin = QSpinBox()
    spin.setRange(low, high)
    spin.setValue(value)
    spin.setKeyboardTracking(False)
    spin.valueChanged.connect(on_change)
    return spin


class RunSpinBox(QSpinBox):
    """A spin over **runs** of integers with gaps between them.

    The Cell spin's: a reference field can have a floor and holes as well as a
    top (:meth:`~celpix.plugins.base.TilemapCodecPlugin.index_runs`). The first
    run's start and the last one's end are the ordinary minimum and maximum, and
    a value between two runs is treated exactly as Qt treats one outside them —
    typed, it is never committed and the box goes back to the value it held;
    stepped, the arrows move over the gap to the next value there is, as they
    stop at either end rather than going past it.
    """

    def __init__(self) -> None:
        super().__init__()
        self._runs: tuple[range, ...] = ()

    def set_runs(self, runs: Sequence[range]) -> None:
        """Range the spin over ``runs`` — ascending, disjoint, at least one."""
        self._runs = tuple(runs)
        self.setRange(self._runs[0].start, self._runs[-1].stop - 1)

    def _holds(self, value: int) -> bool:
        return not self._runs or any(value in run for run in self._runs)

    def validate(self, text: str, pos: int) -> object:
        state = super().validate(text, pos)
        verdict = state[0] if isinstance(state, tuple) else state
        if verdict == QValidator.State.Acceptable and not self._holds(
            self.valueFromText(text)
        ):
            # Intermediate, as Qt answers a value below the minimum: still
            # typeable on the way to one that fits, never committed as it is.
            return (QValidator.State.Intermediate, text, pos)
        return state

    def stepBy(self, steps: int) -> None:
        target = self.value() + steps
        if steps > 0:
            over = next((run.start for run in self._runs if run.start > target), None)
        else:
            over = next(
                (run.stop - 1 for run in reversed(self._runs) if run.stop <= target),
                None,
            )
        if self._holds(target) or over is None:
            super().stepBy(steps)
        else:
            self.setValue(over)


def hex_spin(
    low: int,
    high: int,
    tip: str,
    value: int = 0,
    *,
    kind: type[_SpinT] = QSpinBox,
) -> _SpinT:
    """A hex spin — the toolbars' one way of showing an address.

    A spin rather than a free-text field so these numbers clamp and step like
    the rest of the bar, and hex because that is how every one of them is
    written down elsewhere: a bank layout, a tile index in a map, a code in a
    character table. The tooltip is suffixed rather than each caller remembering
    to say so, since a box showing ``20`` for thirty-two is only unambiguous
    once the reader knows which base it is in. No ``$`` prefix: like every
    address input, the box holds bare digits - the prefix belongs where a
    number is shown, not where it is typed.

    ``kind`` is the spin class, for one that ranges differently
    (:class:`RunSpinBox`).
    """
    spin = kind()
    spin.setRange(low, high)
    spin.setValue(value)
    spin.setDisplayIntegerBase(16)
    spin.setKeyboardTracking(False)
    spin.setToolTip(f"{tip} (hex)")
    return spin


#: The last line of every free-text address box's tooltip: the convention is
#: app-wide (:func:`~celpix.core.address.parse_hex`), so it is said the same way
#: wherever one is typed into.
HEX_ENTRY_NOTE = "Hex; $ and 0x prefixes accepted"


def hex_field(tip: str, value: int | None = None) -> QLineEdit:
    """A free-text address box: bare hex digits, ``$``/``0x`` accepted.

    A text field rather than :func:`hex_spin` where the value may be blank —
    a length a decompressor discovers, an input left unbound. The tooltip ends
    in :data:`HEX_ENTRY_NOTE` so no box forgets to say which base it reads, and
    ``value`` is shown in the one spelling an input box holds (no prefix, six
    digits, :func:`~celpix.core.address.format_hex`). Read back with
    :func:`hex_value`.
    """
    field = QLineEdit("" if value is None else format_hex(value, prefix=False))
    field.setToolTip(f"{tip}\n{HEX_ENTRY_NOTE}")
    return field


def hex_value(field: QLineEdit, *, allow_negative: bool = False) -> int | None:
    """What :func:`hex_field` ``field`` holds, or ``None`` for blank or unreadable.

    Negative is refused unless asked for: ``parse_hex("-5")`` is -5, and no
    offset or length a box here takes can be one.
    """
    value = parse_hex(field.text())
    if value is None or (value < 0 and not allow_negative):
        return None
    return value


def make_action(
    owner: QWidget,
    text: str,
    slot: Callable | None = None,
    *,
    menu=None,  # noqa: ANN001 — QMenu
    tip: str = "",
    shortcut=None,  # noqa: ANN001 — QKeySequence | StandardKey | str
    context: Qt.ShortcutContext | None = None,
    enabled: bool = True,
    checkable: bool = False,
    checked: bool = False,
) -> QAction:
    """One menu/toolbar action, built in the order the pieces have to go in.

    Spelling an action out longhand is five or six statements that are the same
    everywhere, and the two that are *not* interchangeable are exactly the ones
    a hand-written block gets wrong: a checkable action's initial state must be
    set **before** its handler is connected, or building the menu fires the
    handler; and ``slot`` goes on ``toggled`` for a checkable action and on
    ``triggered`` for the rest, since a switch's handler wants the new state and
    a command's wants nothing.

    ``context`` is for a **display-only** shortcut — one set for the label it
    puts in the menu and the F1 guide, then given
    ``Qt.ShortcutContext.WidgetShortcut`` so it never actually fires, because
    the working binding is somewhere else (the app-wide key filter, or a panel's
    own handling). ``menu`` adds the finished action where it belongs, for the
    common case that it has exactly one home.
    """
    action = QAction(text, owner)
    if tip:
        action.setToolTip(tip)
    if shortcut is not None:
        action.setShortcut(shortcut)
    if context is not None:
        action.setShortcutContext(context)
    if checkable:
        action.setCheckable(True)
        action.setChecked(checked)
    if slot is not None:
        (action.toggled if checkable else action.triggered).connect(slot)
    action.setEnabled(enabled)
    if menu is not None:
        menu.addAction(action)
    return action


def modal_tool_actions(
    owner: QWidget,
    text: str,
    icon_text: str,
    key: str,
    tip: str,
    toggled: Callable[[bool], None],
    triggered: Callable[[], None],
) -> tuple[QAction, QAction]:
    """A modal tool's two actions over one state: ``(bar button, menu row)``.

    Two because a bar button and a menu row want opposite things from Qt's
    checkable flag. The button needs it — a latched button is how an armed modal
    tool says it is armed — while the row must not have it: it sits among plain
    mode-toggle rows (Toggle Selection Mode, Toggle Edit Mode), and a lone
    checkbox there reads as a different kind of thing from its neighbours. The
    button drives the state through ``toggled``; the row's ``triggered`` should
    press the button, so the two cannot disagree, and
    :func:`sync_modal_tool` converges them afterwards.

    The button carries ``icon_text``, since QToolButton takes its label from it
    and the full row text would stretch the bar. ``key`` goes on the **row**,
    display-only (a widget-context shortcut that never fires): the bare letter
    is routed by the app-wide key filter, which yields to focused text inputs,
    and the row is where the menu and the F1 guide read it from. The row starts
    disabled, as nothing is open yet; the button is the caller's to place.
    """
    tool = make_action(owner, text, toggled, tip=tip, checkable=True)
    tool.setIconText(icon_text)
    row = make_action(
        owner,
        text,
        triggered,
        tip=tip,
        shortcut=QKeySequence(key),
        context=Qt.ShortcutContext.WidgetShortcut,
        enabled=False,
    )
    return tool, row


def sync_modal_tool(
    tool: QAction, row: QAction, *, armed: bool, available: bool, tip: str
) -> None:
    """Converge a :func:`modal_tool_actions` pair with the state they show.

    The button is re-latched under blocked signals, since a converge is not a
    gesture and must not re-enter the arming it reflects. The row holds no state
    of its own; it only needs to be as reachable, and say as much, as the button
    it stands in for. Disarming a tool that became unavailable is the caller's,
    before this: only it knows how to put the tool down.
    """
    if tool.isChecked() != armed:
        with signals_blocked(tool):
            tool.setChecked(armed)
    for action in (tool, row):
        action.setEnabled(available)
        action.setToolTip(tip)


def add_enum_action_group(
    owner: QWidget,
    menu,  # noqa: ANN001 — QMenu
    entries: Iterable[tuple[object, str, str]],
    current: object,
    on_triggered: Callable,
) -> tuple[QActionGroup, dict[object, QAction]]:
    """A radio group of checkable actions over an enum, built from a table.

    Every "pick exactly one" menu section is the same six lines around a table
    of ``(value, label, tooltip)`` — an empty tooltip where the label says it
    all — with the enum member on each action's ``data`` so the handler reads
    the choice back off the group rather than off a captured variable. The group
    is what makes the set exclusive; it is returned because the handlers ask it
    which action is checked, along with the actions by value for the callers
    that later enable or re-check one.

    ``current`` is compared by identity: these are enum members, and a value
    that is not among ``entries`` simply leaves the group unchecked rather than
    raising — a preference written by an older or newer build is not a reason to
    fail to open a menu.
    """
    group = QActionGroup(owner)  # exclusive: one action checked at a time
    actions: dict[object, QAction] = {}
    for value, label, tip in entries:
        action = make_action(
            owner, label, menu=menu, tip=tip, checkable=True, checked=value is current
        )
        action.setData(value)
        group.addAction(action)
        actions[value] = action
    group.triggered.connect(on_triggered)
    return group, actions


class FlowButtonGrid(QWidget):
    """A run of buttons folded to the widget's width, a row at a time.

    A grid rather than a strip that scrolls sideways: where the buttons are a
    whole vocabulary — the text window's insert row is a format's every command
    — one scrolled out of sight is one the user has to go hunting for, and a
    hidden name is no better than an unlisted one. So every button is on screen
    at once and the widget grows a line at a time instead.

    Columns are uniform and as wide as the widest caption needs, which keeps a
    control table reading as a table; how many of them fit is the width divided
    by that, recomputed as the window is dragged. The buttons stretch to fill,
    so the last row of a short list lines up with the ones above it rather than
    ending in a ragged edge.
    """

    def __init__(self, spacing: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(spacing)
        self._spacing = spacing
        self._buttons: list[QPushButton] = []
        self._columns = 0
        # Vertically Minimum: the height is whatever the rows come to, and the
        # content above keeps the rest.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    @property
    def layout_(self) -> QGridLayout:
        """The grid itself — the buttons in the order they were given."""
        return self._grid

    def set_buttons(self, buttons: list[QPushButton]) -> None:
        for button in self._buttons:
            self._grid.removeWidget(button)
            button.setParent(None)
            button.deleteLater()
        self._buttons = buttons
        for button in buttons:
            # Expanding, so a column wider than the caption is filled rather than
            # leaving the button floating in the middle of its cell.
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._columns = 0  # nothing is placed yet, whatever the count was before
        self._reflow()

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802 — Qt override
        super().resizeEvent(event)
        self._reflow()

    def _reflow(self) -> None:
        """Lay the buttons out in as many columns as the current width holds."""
        columns = self._fits()
        if columns == self._columns:
            return
        self._columns = columns
        # Taken out before being put back: adding a widget the layout already
        # manages to a second cell leaves it in both, and the row it came from
        # keeps its old height.
        for button in self._buttons:
            self._grid.removeWidget(button)
        for at, button in enumerate(self._buttons):
            self._grid.addWidget(button, *divmod(at, columns))
        for column in range(self._grid.columnCount()):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)

    def _fits(self) -> int:
        """How many uniform columns the width holds — at least one, at most all."""
        if not self._buttons:
            return 1
        widest = max(button.sizeHint().width() for button in self._buttons)
        step = widest + self._spacing
        return max(1, min(len(self._buttons), (self.width() + self._spacing) // step))


class ChecklistPopupButton(QToolButton):
    """A toolbar button that drops down a checkable list, with Select All / None.

    A compact multi-select filter: the owner supplies the current entries each
    time the popup opens (so a source list that changes — e.g. a plugin refresh
    adds a preset — stays in sync), and every change is handed to ``apply``,
    which does the real work and returns the set that ended up in force. The
    button then re-syncs its checkboxes to that set, so a request the owner had
    to clamp — you can never hide *everything* — visibly springs back. All of
    the filtering/selection logic lives with the owner and is unit-tested
    without driving this view.

    The popup is a top-level ``Qt.Popup``: clicks inside it (the checkboxes, the
    two buttons) leave it open, and a click anywhere else dismisses it — so the
    list stays up while several boxes are toggled.
    """

    def __init__(
        self,
        text: str,
        items: Callable[[], list[tuple[object, str, bool]]],
        apply: Callable[[set], set],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setText(text)
        self._items = items
        self._apply = apply
        self._boxes: dict[object, QCheckBox] = {}
        self._popup: QWidget | None = None
        self.clicked.connect(self._open)

    def _open(self) -> None:
        # The last popup is closed by now (a popup goes on any click away) but,
        # parented to this button, would stay alive with its checkboxes for the
        # session — one more set per open. Deleted here rather than on close so
        # the boxes stay valid for as long as they are the ones on record.
        if self._popup is not None:
            self._popup.deleteLater()
        popup = QWidget(self, Qt.WindowType.Popup)
        outer = QVBoxLayout(popup)
        buttons = QHBoxLayout()
        select_all = QPushButton("Select All")
        select_none = QPushButton("Select None")
        select_all.clicked.connect(lambda: self._bulk(True))
        select_none.clicked.connect(lambda: self._bulk(False))
        buttons.addWidget(select_all)
        buttons.addWidget(select_none)
        outer.addLayout(buttons)

        # The source list can be long (dozens of codecs); scroll rather than grow
        # a popup taller than the screen.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(400)
        inner = QWidget()
        column = QVBoxLayout(inner)
        self._boxes = {}
        for key, label, checked in self._items():
            box = QCheckBox(label)
            box.setChecked(checked)
            box.toggled.connect(self._on_toggle)
            self._boxes[key] = box
            column.addWidget(box)
        column.addStretch(1)
        scroll.setWidget(inner)
        # Wide enough for the longest name. A scroll area's own width hint is its
        # widget's and counts no scrollbar, but the height cap above guarantees a
        # vertical one over a list this long -- so left to adjustSize() the popup
        # opens exactly one scrollbar too narrow and puts a horizontal scrollbar
        # under names that would otherwise have fit.
        scroll.setMinimumWidth(
            inner.sizeHint().width()
            + scroll.verticalScrollBar().sizeHint().width()
            + 2 * scroll.frameWidth()
        )
        outer.addWidget(scroll)

        popup.adjustSize()
        popup.move(self.mapToGlobal(self.rect().bottomLeft()))
        popup.show()
        self._popup = popup  # keep a reference so it isn't collected mid-show

    def _checked_keys(self) -> set:
        return {key for key, box in self._boxes.items() if box.isChecked()}

    def _on_toggle(self, *_args) -> None:
        self._sync(self._apply(self._checked_keys()))

    def _bulk(self, checked: bool) -> None:
        self._sync(self._apply(set(self._boxes) if checked else set()))

    def _sync(self, effective: set) -> None:
        """Reflect the owner's authoritative set back onto the checkboxes."""
        for key, box in self._boxes.items():
            with signals_blocked(box):
                box.setChecked(key in effective)
