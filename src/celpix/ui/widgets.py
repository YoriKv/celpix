"""Small reusable UI widgets and shared painting idioms.

Qt lives here (this is the ``ui`` layer); the model stays Qt-free.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeVar

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPointF,
    QRect,
    QSettings,
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
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from celpix import APP_NAME
from celpix.core import ceil_div

_EnumT = TypeVar("_EnumT", bound=Enum)

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


def take_editing_shortcut(event: QEvent) -> bool:
    """Claim a canvas editing shortcut for a focused panel; call from ``event()``.

    Returns True (having *accepted* ``event``) when it is a ``ShortcutOverride``
    for one of :data:`_EDITING_SHORTCUTS`. Accepting the override routes the key
    to the focused widget as a normal press instead of letting the canvas's
    window-wide shortcut consume (or, for Delete, ambiguously drop) it - so a
    panel that has its own key handling isn't shadowed by the editing surface
    behind it. The widget then handles the resulting key press however it likes
    (or ignores it, so the key simply does nothing there). Mirrors how the
    app-wide arrow-key filter yields to these same panels, and how text inputs
    already claim their editing keys natively.

    Usually reached through :class:`ShortcutIsland` rather than called directly;
    it stays public for a widget that has its own ``event()`` to weave it into.
    """
    if event.type() == QEvent.Type.ShortcutOverride and any(
        event.matches(key) for key in _EDITING_SHORTCUTS
    ):
        event.accept()
        return True
    return False


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


def counted(count: int, noun: str) -> str:
    """``count`` with ``noun`` pluralised — ``"1 tile"``, ``"1,024 tiles"``.

    Every "Copied N tiles." / "Cleared N cells." the status bar says goes through
    here, so a count is never grouped in one message and not the next. Naive
    plurals (an ``s``), which is all these nouns need; a noun that pluralises any
    other way would have to be spelled out at the call site.
    """
    return f"{count:,} {noun}" + ("" if count == 1 else "s")


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
        for widget, was_blocked in zip(widgets, previous):
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


def ask_save_path(
    parent: QWidget, title: str, default: str, file_filter: str, suffix: str
) -> str | None:
    """A Save-As dialog whose answer always carries ``suffix``.

    ``None`` when the user cancels. The suffix is appended rather than assumed,
    because a typed name without one is the common case and every export here
    writes exactly one format — so the extension is not the user's decision to
    forget. One helper so no export path silently omits it.
    """
    path, _ = QFileDialog.getSaveFileName(parent, title, default, file_filter)
    if not path:
        return None
    return path if path.lower().endswith(suffix.lower()) else path + suffix


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


def icon_cache_key(widget: QWidget) -> tuple[int, float]:
    """What a widget's baked icons depend on: the theme, and the device scale.

    Both arrive as a ``changeEvent`` storm — Qt sends a burst of PaletteChange on
    startup and again on every theme switch — so every panel that rasterizes its
    own icons guards the re-bake on this rather than re-doing it per event.
    """
    return (widget.palette().cacheKey(), widget.devicePixelRatioF())


def tinted_icon(source: QImage, color: QColor, box: QSize, ratio: float) -> QPixmap:
    """``source`` recolored to ``color``, fitted and centred in a ``box`` square.

    The bundled icons ship as solid silhouettes cropped to their opaque bounds;
    SourceIn keeps only the alpha and stamps the tint through, so one piece of
    art tracks the theme in light and dark. Rasterized at ``ratio`` and stamped
    with it, so a scaled display gets crisp edges rather than a stretched 1x
    bitmap, and the pixmap still measures ``box`` in layout units. Centred
    because the art is rarely square.
    """
    tinted = source.convertToFormat(QImage.Format.Format_ARGB32)
    tinting = QPainter(tinted)
    tinting.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    tinting.fillRect(tinted.rect(), color)
    tinting.end()
    box_w, box_h = round(box.width() * ratio), round(box.height() * ratio)
    scaled = QPixmap.fromImage(tinted).scaled(
        box_w,
        box_h,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    canvas = QPixmap(box_w, box_h)
    canvas.fill(Qt.GlobalColor.transparent)
    placing = QPainter(canvas)
    placing.drawPixmap(
        (box_w - scaled.width()) // 2, (box_h - scaled.height()) // 2, scaled
    )
    placing.end()
    canvas.setDevicePixelRatio(ratio)
    return canvas


def paint_selection_outline(
    painter: QPainter, rect: QRect, alpha: int = 255, color: QColor | None = None
) -> None:
    """The app's shared selection outline: a white ring over a black one.

    One outline language for every "this is the active thing" highlight (the
    canvas's tile selection, the palette panel's active subpalette). Two 1px
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
    just inside it, so the whole 2px band lands *within* ``rect``: an aliased
    ``drawRect`` renders one pixel past its path, hence the -1 insets.
    """
    painter.setBrush(Qt.BrushStyle.NoBrush)
    outer = QColor(color) if color is not None else QColor(255, 255, 255)
    outer.setAlpha(alpha)
    painter.setPen(QPen(outer, 1))
    painter.drawRect(rect.adjusted(0, 0, -1, -1))
    painter.setPen(QPen(QColor(0, 0, 0, alpha), 1))
    painter.drawRect(rect.adjusted(1, 1, -2, -2))


# The width every **format picker** takes: pixel, palette, tilemap, compression
# and arrangement. One number rather than five, because they are one kind of
# control and a toolbar of them reads as a row — which is exactly what deriving
# the width from the content could not give, since the registry decides how long
# the longest preset name is and a plugin can make it longer at any time.
PRESET_COMBO_WIDTH = 160


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


def funnel_icon(color: QColor, size: int = 16, ratio: float = 1.0) -> QIcon:
    """A funnel/filter icon filled with ``color`` — the app's "filter a list" mark.

    Painted rather than bundled so it inherits the current theme's text color and
    stays crisp at any device-pixel ratio; Qt derives the disabled (greyed) form
    from it automatically. The silhouette sits in a padded unit box: a wide mouth
    converging to a short stem.
    """
    px = max(1, round(size * ratio))
    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    unit = [
        (0.12, 0.18),
        (0.88, 0.18),
        (0.58, 0.52),
        (0.58, 0.86),
        (0.42, 0.86),
        (0.42, 0.52),
    ]
    painter.drawPolygon(QPolygonF([QPointF(x * px, y * px) for x, y in unit]))
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return QIcon(pixmap)


# Amber for the warning level. The QToolTip rule is not decoration: Qt applies a
# bare `color:` to the widget's *tooltip* as well, which would render the whole
# explanation amber — pinning it to palette(text) keeps the tooltip readable in
# either theme.
BADGE_WARNING_STYLE = "QLabel { color: #c08a30; } QToolTip { color: palette(text); }"


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
    the text and forgot the stylesheet would carry the last badge's colour into
    this one's words.
    """
    label.setText(badge.text if badge else "")
    label.setToolTip(badge.detail if badge else "")
    label.setStyleSheet(BADGE_WARNING_STYLE if badge and badge.warning else "")
    label.setVisible(badge is not None)


# The zoom multipliers the view offers, in order. Whole numbers magnify, and
# **0.5 is the one reduction**: it is what a file too tall for the window is read
# at (a screen's worth of a tilemap, a sprite sheet end to end), where every
# larger step would only show less of it. Nothing between 0.5 and 1 - halving is
# the only reduction nearest-neighbour can do without inventing pixels, and the
# art these are for is pixel art.
ZOOM_LEVELS: tuple[float, ...] = (0.5, *range(1, 25))


def zoom_level_after(zoom: float, steps: int) -> float:
    """The level ``steps`` along from ``zoom``, clamped to the ends of the list.

    Stepping walks the list rather than adding to the value, so the gap under 1
    is one step like every other and Zoom Out from 1 lands on 0.5 instead of on
    nothing. An off-list value (a project written by hand) starts from the
    nearest level - the lower one when it falls exactly between two - so a step
    still lands somewhere the picker can show.
    """
    at = min(range(len(ZOOM_LEVELS)), key=lambda i: abs(ZOOM_LEVELS[i] - zoom))
    return ZOOM_LEVELS[max(0, min(len(ZOOM_LEVELS) - 1, at + steps))]


class ZoomSpinBox(QDoubleSpinBox):
    """The Zoom control: a multiplier, stepped through :data:`ZOOM_LEVELS`.

    A double spin because one level is fractional, but it never *reads* like one:
    :meth:`textFromValue` writes whole numbers bare, so the box shows "4" and not
    "4.0". Its arrows and Up/Down keys move one level, and a typed value snaps to
    the nearest one - the list is the whole of what the view supports, so an
    in-between number would be a setting that silently did something else.
    """

    def __init__(self, value: float = 4.0) -> None:
        super().__init__()
        self.setRange(ZOOM_LEVELS[0], ZOOM_LEVELS[-1])
        self.setDecimals(1)  # enough for the one fractional level
        # Commit on Enter / focus-out / stepping, not per keystroke, like every
        # other view spin: typing "12" must not re-render at "1".
        self.setKeyboardTracking(False)
        self.setValue(value)

    def textFromValue(self, value: float) -> str:
        return f"{value:g}"

    def valueFromText(self, text: str) -> float:
        """Snap typed text onto the nearest level; keep the current one if unread."""
        try:
            return zoom_level_after(float(text.replace(",", ".")), 0)
        except ValueError:
            return self.value()

    def stepBy(self, steps: int) -> None:
        self.setValue(zoom_level_after(self.value(), steps))


class PanZoomSurface:
    """Space-drag panning and Ctrl+wheel zooming, for a widget in a scroll area.

    The three surfaces that magnify pixel art inside a scroll area — the canvas,
    the tile source sheet, the animation frame — all offer the same two gestures,
    and a user who learns one on any of them expects it on the others. What they
    do *with* the gestures differs (each reports to a different controller over
    its own signals), but the mechanics are identical down to the reason for
    every line, so they live here rather than being kept in step by hand.

    The state is one armed flag and one dragging flag, deliberately apart:
    ``_pan_active`` is the space bar held — a pan is *armed*, and the open hand
    says so — while ``_panning`` is a drag actually under way. Disarming has to
    end a drag in progress, because the key can come up mid-drag.

    Mixed in **before** the Qt base (``class Canvas(PanZoomSurface, QWidget)``).
    The three mouse handlers are helpers returning "I took this event" rather
    than Qt overrides, because each surface has its own gesture stack to weave
    the pan into (and pan wins over all of it — see the call sites); only
    :meth:`wheelEvent` is complete enough to be the override itself.

    Two hooks: :meth:`_pan_cursor` for a surface with cursors of its own beyond
    the hand, and :meth:`_has_content` for the "nothing to zoom" guard, which is
    a different emptiness in each of them. A surface that wants the empty backing
    around it to zoom as well says so once, with
    :meth:`claim_background`.
    """

    # Declared by the concrete widget (a Signal only registers on a QObject
    # subclass), and named here so the helpers below read as the whole gesture:
    # ``pan_requested(dx, dy)`` in device pixels, ``zoom_requested(steps, pos)``
    # with the cursor in the widget's own coordinates.
    pan_requested: Signal
    zoom_requested: Signal

    _pan_active = False
    _panning = False
    _pan_last = QPointF()

    def set_pan_mode(self, on: bool) -> None:
        """Arm/disarm space-drag panning (the window drives this off the space key).

        Arming shows the open hand; disarming ends any pan drag in progress, the
        space key being free to come up mid-drag. Panning is modal over the
        mouse — while armed a press pans instead of selecting or painting.
        """
        if self._pan_active == on:
            return
        self._pan_active = on
        if not on:
            self._panning = False
        self._apply_cursor()

    def _pan_cursor(self) -> Qt.CursorShape | None:
        """The cursor when no pan is armed — ``None`` for the widget's own.

        Overridden by a surface that is modal in more ways than this one: the
        canvas arms tools that each want their own pointer.
        """
        return None

    def _has_content(self) -> bool:
        """Whether there is anything on show to zoom. Overridden per surface —
        each holds its picture in a different attribute."""
        return True

    def _apply_cursor(self) -> None:
        """Set the cursor for the current mode: closed hand while panning, open
        while a pan is merely armed, and the surface's own otherwise."""
        if self._panning:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self._pan_active:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            shape = self._pan_cursor()
            if shape is None:
                self.unsetCursor()
            else:
                self.setCursor(shape)

    def _pan_press(self, event) -> bool:  # noqa: ANN001 — Qt event
        """Begin a pan drag if one is armed; True when the press was taken.

        Called first in every surface's ``mousePressEvent``, so an armed pan wins
        over selecting, painting and picking alike.
        """
        if not (self._pan_active and event.button() == Qt.MouseButton.LeftButton):
            return False
        self._panning = True
        self._pan_last = event.globalPosition()
        self._apply_cursor()
        event.accept()
        return True

    def _pan_move(self, event) -> bool:  # noqa: ANN001 — Qt event
        """Report the drag's delta if a pan is under way; True when taken."""
        if not self._panning:
            return False
        # Global position, not widget-local: the widget shifts under the cursor
        # as the view scrolls, which would feed back into a widget-local delta.
        pos = event.globalPosition()
        delta = pos - self._pan_last
        self._pan_last = pos
        self.pan_requested.emit(round(delta.x()), round(delta.y()))
        event.accept()
        return True

    def _pan_release(self, event) -> bool:  # noqa: ANN001 — Qt event
        """End a pan drag; True when the release was taken."""
        if not (self._panning and event.button() == Qt.MouseButton.LeftButton):
            return False
        self._panning = False
        self._apply_cursor()  # back to the open hand (space may still be held)
        event.accept()
        return True

    def wheelEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """**Ctrl**+wheel zooms; a plain wheel falls through to the scroll area.

        Reports a signed step per notch and the cursor position, leaving the
        range and the cursor-anchoring to the controller: the level is a
        control's value, not this widget's. Only a zooming wheel is swallowed,
        so an unmodified one still scrolls the area that owns us.
        """
        if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            event.ignore()  # let the scroll area scroll as usual
            return
        self._report_zoom(event, event.position())
        event.accept()

    def _report_zoom(self, event, pos: QPointF) -> bool:  # noqa: ANN001 — QWheelEvent
        """Turn one Ctrl+wheel into a zoom step at ``pos`` (this widget's coords).

        False where there was nothing to report — an empty surface, or a wheel
        whose delta rounds to no notch at all. The event is swallowed either way
        by the caller: a Ctrl+wheel is a zoom the moment it is aimed here, and
        letting the leftovers fall through would scroll the view instead.
        """
        if not self._has_content():
            return False
        dy = event.angleDelta().y()
        if dy == 0:
            return False
        # One step per 120-unit notch, but at least one so a high-resolution
        # wheel sending small deltas still zooms.
        steps = int(dy / 120) or (1 if dy > 0 else -1)
        self.zoom_requested.emit(steps, pos)
        return True

    def claim_background(self, scroll: QScrollArea) -> None:
        """Count the backing around this surface in ``scroll`` as part of it.

        A surface is sized to its content, so anything smaller than its scroll
        area leaves a band of empty viewport around it — and that band is exactly
        where the pointer is when the picture is small enough to want zooming
        *in* on. Without this the gesture answers over the art and does nothing
        an inch to its right, which reads as the wheel zoom being broken rather
        than as a target having been missed. The **grey is the surface** as far
        as a user is concerned, so a Ctrl+wheel out there zooms and a click out
        there focuses — which is what puts the keys that address this sheet
        (Shift+Left/Right for its width, the arrows for its pick) on it.

        Filtered on the viewport rather than handled by a QScrollArea subclass so
        the whole gesture stays one class: the events land on the viewport (the
        surface is its child), and the filter gets them before the scroll area
        turns them into a scroll or takes the focus for itself.
        """
        scroll.viewport().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001 — Qt override
        """The backing's Ctrl+wheel and click, answered as the surface's own.

        The wheel's position is mapped into this widget and **clamped to it**, so
        a zoom started out on the backing anchors on the nearest content pixel
        rather than on a coordinate outside the picture — the anchoring
        arithmetic reads ``pos`` as content pixels times the zoom
        (:func:`zoom_anchored`), and a point past the edge would ask it to hold
        still something that is not there. A plain wheel is left alone and
        scrolls as usual.

        A **press** takes the focus and is then left to travel on. Qt has already
        handed focus to the scroll area by the time this runs — it is given
        before the press is delivered — so this is the surface taking it back,
        and it is what a click on the grey has to do for the keys addressed to
        this sheet to reach it. Not consumed: the press is still the scroll
        area's to do whatever else it does with.
        """
        if isinstance(obj, QWidget) and event.type() == QEvent.Type.MouseButtonPress:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
        elif (
            event.type() == QEvent.Type.Wheel
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and isinstance(obj, QWidget)
        ):
            local = self.mapFrom(obj, event.position().toPoint())
            self._report_zoom(
                event,
                QPointF(
                    min(max(local.x(), 0), max(0, self.width() - 1)),
                    min(max(local.y(), 0), max(0, self.height() - 1)),
                ),
            )
            return True
        return super().eventFilter(obj, event)


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


def grid_slot_at(
    x_px: float,
    y_px: float,
    cell: tuple[int, int],
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
    col, row = int(x_px) // cell_w, int(y_px) // cell_h
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


def pan_scroll_area(scroll: QScrollArea, dx: int, dy: int) -> None:
    """Shift ``scroll`` by a space-drag delta (device pixels).

    The bars clamp to the content, so a pan can never push the picture off
    screen, and is a no-op while the view already fits the viewport — which is
    the whole of the policy, hence one function for all three surfaces.
    """
    hbar = scroll.horizontalScrollBar()
    vbar = scroll.verticalScrollBar()
    hbar.setValue(hbar.value() - dx)
    vbar.setValue(vbar.value() - dy)


def zoom_anchored(scroll: QScrollArea, spin, new: float, pos) -> None:  # noqa: ANN001
    """Move ``spin`` to ``new``, keeping the content pixel under ``pos`` still.

    Driving the *spin* rather than the view is what keeps the readout, the
    keyboard and the wheel one value, and re-renders through the normal path.
    Without the two scroll-bar writes afterwards a zoom appears to slide the art
    out from beneath the pointer: ``pos`` is in the widget's own coordinates,
    which are content pixels times the old zoom, so the pixel under the cursor
    divides out and putting it back is arithmetic the bars then clamp.

    A no-op when the level does not actually change (an end of the range).
    """
    old = spin.value()
    if new == old:
        return
    hbar = scroll.horizontalScrollBar()
    vbar = scroll.verticalScrollBar()
    # The cursor's spot in the viewport, and the content pixel it sits on now.
    view_x, view_y = pos.x() - hbar.value(), pos.y() - vbar.value()
    img_x, img_y = pos.x() / old, pos.y() / old
    spin.setValue(new)  # re-renders and resizes the view synchronously
    hbar.setValue(round(img_x * new - view_x))
    vbar.setValue(round(img_y * new - view_y))


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


def hex_spin(low: int, high: int, tip: str, value: int = 0) -> QSpinBox:
    """A ``$``-prefixed hex spin — the toolbars' one way of showing an address.

    A spin rather than a free-text field so these numbers clamp and step like
    the rest of the bar, and hex because that is how every one of them is
    written down elsewhere: a bank layout, a tile index in a map, a code in a
    character table. The tooltip is suffixed rather than each caller remembering
    to say so, since a box showing ``$20`` for thirty-two is only unambiguous
    once the reader knows which base it is in.
    """
    spin = QSpinBox()
    spin.setRange(low, high)
    spin.setValue(value)
    spin.setDisplayIntegerBase(16)
    spin.setPrefix("$")
    spin.setKeyboardTracking(False)
    spin.setToolTip(f"{tip} (hex)")
    return spin


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


def source_icon(color: QColor, size: int = 16, ratio: float = 1.0) -> QIcon:
    """A ring with a dot at its centre — the app's "go to what this names" mark.

    Painted rather than bundled for the same reasons as :func:`funnel_icon`: it
    inherits the theme's text color and stays crisp at any device-pixel ratio,
    and Qt derives the greyed form itself. Reads as a target rather than as a
    direction: the button it marks does not step somewhere relative to here, it
    opens the one thing a control already names.
    """
    px = max(1, round(size * ratio))
    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    centre = QPointF(px / 2, px / 2)
    # The ring is stroked, so its radius is to the *centre* of the line: half a
    # pen width short of the box, or antialiasing clips the outer edge flat.
    pen = QPen(color)
    pen.setWidthF(max(1.0, px * 0.1))
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    ring = px * 0.34
    painter.drawEllipse(centre, ring, ring)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    dot = px * 0.13
    painter.drawEllipse(centre, dot, dot)
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return QIcon(pixmap)


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


def settings() -> QSettings:
    """The app's preference store, opened the one way everything here opens it.

    The organization is named **explicitly** rather than left to the bare
    ``QSettings()`` default. celPix sets only an application name on the
    QApplication (:mod:`celpix.app` says why), and with no organization Qt files
    the settings under a literal ``Unknown Organization`` — a directory on Linux
    and macOS, a registry key on Windows. celPix is its own organization, so
    naming it puts the file where its name says: ``~/.config/celPix.conf``,
    ``HKCU\\Software\\celPix``.

    The format is named too, as whatever the default currently *is*, rather than
    left to the constructor to imply: the overloads that take no format resolve
    the file through the platform's native path table whatever
    ``setDefaultFormat`` says, which would put the store beyond the reach of
    ``QSettings.setPath`` — and that is the one hook a test suite has for keeping
    its writes out of the developer's own config.
    """
    return QSettings(QSettings.defaultFormat(), QSettings.Scope.UserScope, APP_NAME)


def load_enum_setting(key: str, default: _EnumT) -> _EnumT:
    """An app-wide appearance/interaction preference out of QSettings.

    The app-global preferences (the grid, selection shape, active tool) are
    stored by their enum's string ``value``, so the settings file stays readable
    and stable. A stored value this build has no member for — an older or newer
    celPix wrote the settings — falls back to ``default`` rather than raising: a
    stale preference is not a reason to fail to start.
    """
    stored = settings().value(key, default.value)
    try:
        return type(default)(stored)
    except ValueError:
        return default


def save_enum_setting(key: str, value: Enum) -> None:
    """Persist an app-wide preference — the write half of
    :func:`load_enum_setting`, storing the enum's ``value`` so the two agree on
    the on-disk form in one place rather than at each call site."""
    settings().setValue(key, value.value)


def load_bool_setting(key: str, default: bool) -> bool:
    """An app-wide on/off preference out of QSettings.

    Read through Qt's own conversion rather than Python's: the INI backend hands
    a boolean back as the *string* it wrote, and ``bool("false")`` is True.
    """
    return settings().value(key, default, type=bool)


def save_bool_setting(key: str, value: bool) -> None:
    """Persist an app-wide on/off preference (see :func:`load_bool_setting`)."""
    settings().setValue(key, value)


# The recently opened projects, newest first. App-wide rather than per-project:
# the list is how you get *back* to a project, so it cannot live inside one. Ten
# is deep enough to reach last week's work while the menu stays a menu.
RECENT_PROJECTS_KEY = "recent/projects"
MAX_RECENT_PROJECTS = 10


def _recent_path(path: str) -> str:
    """A project path in the one spelling the list stores it under.

    The same project reaches us spelled differently depending on how it was
    opened — Qt hands back a dropped file's URL with POSIX separators even on
    Windows, where the file dialog's answer is native — and a list that stores
    both grows a second row for a project the user only has one of. Separators
    are normalized for storage; case only for the comparison (:func:`_recent_key`),
    since the name still has to be shown as the file system spells it.
    """
    return os.path.normpath(os.path.abspath(path))


def _recent_key(path: str) -> str:
    """The identity a recent path is de-duplicated by — case-folded on the
    platforms whose file names are (Windows), so a project opened as
    ``D:\\roms`` and again as ``d:\\ROMS`` stays one row."""
    return os.path.normcase(_recent_path(path))


def load_recent_projects() -> list[str]:
    """The remembered project paths, newest first.

    A one-item list comes back out of QSettings as a bare string — the INI
    backend can't tell a single-element list from a scalar — so both shapes are
    folded to a list here rather than at each call site.
    """
    stored = settings().value(RECENT_PROJECTS_KEY)
    if stored is None:
        return []
    if isinstance(stored, str):
        return [stored] if stored else []
    return [str(path) for path in stored]


def remember_recent_project(path: str) -> None:
    """Record ``path`` as the newest recent project, dropping the oldest over
    :data:`MAX_RECENT_PROJECTS`. Re-opening a listed project moves it back to
    the top rather than listing it twice."""
    key = _recent_key(path)
    recent = [p for p in load_recent_projects() if _recent_key(p) != key]
    recent.insert(0, _recent_path(path))
    settings().setValue(RECENT_PROJECTS_KEY, recent[:MAX_RECENT_PROJECTS])


def forget_recent_project(path: str) -> None:
    """Drop ``path`` from the recent list — for a project that is no longer
    where it was, which the list itself has no way of noticing."""
    key = _recent_key(path)
    settings().setValue(
        RECENT_PROJECTS_KEY,
        [p for p in load_recent_projects() if _recent_key(p) != key],
    )


def clear_recent_projects() -> None:
    """Forget every recent project."""
    settings().remove(RECENT_PROJECTS_KEY)
