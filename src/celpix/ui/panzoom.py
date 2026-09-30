"""Magnifying a picture inside a scroll area: zoom levels, pan and wheel.

The canvas, the tile source sheet, the font alphabet's sheet, the subsprite
sheet and the animation frame all magnify pixel art inside a scroll area, and a
user who learns the gestures on one expects them on the others: Ctrl+wheel
zooms on the pixel under the pointer, a held space bar arms a drag that pans.
What each surface does *with* the gestures differs — each reports to its own
controller — but the mechanics are the same down to the reason for every line,
so they live here (:class:`PanZoomSurface`, and the helpers that wire a surface
to its scroll area and its Zoom spin).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QScrollArea,
    QSpinBox,
    QWidget,
)

from celpix.core.aspect import SQUARE, PixelAspect
from celpix.core.aspect import scale as aspect_scale

__all__ = [
    "ZOOM_LEVELS",
    "PanOnlyMouse",
    "PanZoomSurface",
    "SpacePanFilter",
    "ZoomSpinBox",
    "mount_surface",
    "pan_scroll_area",
    "wheel_zoom",
    "zoom_anchored",
    "zoom_level_after",
    "zoom_spin",
]


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


def _set_cursor(widget: QWidget, shape: Qt.CursorShape | None) -> None:
    """``shape`` on ``widget``, or back to whatever it inherits for ``None``."""
    if shape is None:
        widget.unsetCursor()
    else:
        widget.setCursor(shape)


class PanZoomSurface:
    """Space-drag panning and Ctrl+wheel zooming, for a widget in a scroll area.

    The four surfaces that magnify pixel art inside a scroll area — the canvas,
    the tile source sheet, the font alphabet's sheet, the animation frame — all
    offer the same two gestures, and a user who learns one on any of them expects
    it on the others. What they
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
    #: The scroll viewport this surface has claimed as its own backing, if it has
    #: (:meth:`claim_background`) — the one other widget the gestures answer over.
    _backing: QWidget | None = None

    #: The magnification, in the surface's own units — an integer level on the
    #: sheets, a :data:`ZOOM_LEVELS` multiplier on the canvas. Held here because
    #: the pixel aspect below is only meaningful against it: what reaches the
    #: screen is the two multiplied, and every surface has to multiply them the
    #: same way. Each subclass still owns its own range, through its ``set_zoom``.
    _zoom: float = 1.0
    #: The shape of one pixel (:mod:`celpix.core.aspect`) — one project-wide
    #: setting, pushed onto every surface by the window.
    _pixel_aspect: PixelAspect = SQUARE
    #: :func:`~celpix.core.aspect.scale` of the above, kept rather than recomputed:
    #: the per-cell geometry helpers below are called inside paint loops that run
    #: once per cell of a window holding thousands.
    _aspect_scale: tuple[float, float] = (1.0, 1.0)

    @property
    def _zoom_x(self) -> float:
        """Image pixels to device pixels, horizontally — zoom times the aspect.

        The pair below is what every surface draws, measures and hit-tests
        through. They are equal on a square pixel, which is every existing view,
        so a surface that reads both is unchanged wherever nothing has been set.
        """
        return self._zoom * self._aspect_scale[0]

    @property
    def _zoom_y(self) -> float:
        """Image pixels to device pixels, vertically — see :attr:`_zoom_x`."""
        return self._zoom * self._aspect_scale[1]

    def set_pixel_aspect(self, aspect: PixelAspect) -> None:
        """Draw one image pixel at ``aspect``'s shape from now on.

        The one entry point for the setting, so a surface added later is aspect-
        aware by inheriting rather than by remembering to be. Resizes through the
        subclass's own ``_update_size``, which is what puts the new geometry in
        front of the scroll area that holds it.
        """
        aspect = tuple(aspect)  # a list, off a project file, is the same shape
        if aspect == self._pixel_aspect:
            return
        self._pixel_aspect = aspect
        self._aspect_scale = aspect_scale(aspect)
        self._update_size()
        self.update()

    def _update_size(self) -> None:
        """Re-fit the widget to its content at the current zoom and aspect.

        Every surface has one already — it is how each answers its scroll area —
        and naming it here is what lets :meth:`set_pixel_aspect` be the whole of
        the setting rather than four copies of it.
        """

    def _scaled_size(self, width: int, height: int) -> tuple[int, int]:
        """``width`` x ``height`` image pixels as whole device pixels.

        What every surface's ``_update_size`` measures with. Rounded rather than
        truncated so a 7:8 pixel does not lose a device row off the bottom, and
        floored at 1 so an empty picture still has a widget.
        """
        return (
            max(1, round(width * self._zoom_x)),
            max(1, round(height * self._zoom_y)),
        )

    def _scaled_rect(self, x: int, y: int, width: int, height: int) -> QRect:
        """An image-pixel rectangle in device pixels.

        Each edge is rounded on its own — right edge from ``x + width``, not from
        a rounded width — so abutting rectangles keep abutting under a fractional
        aspect instead of leaving a seam between every pair.
        """
        left, top = round(x * self._zoom_x), round(y * self._zoom_y)
        right, bottom = (
            round((x + width) * self._zoom_x),
            round((y + height) * self._zoom_y),
        )
        return QRect(left, top, right - left, bottom - top)

    def _exposed_rows(self, exposed: QRect, row_height: int) -> tuple[int, int]:
        """Which rows of ``row_height`` image pixels ``exposed`` touches.

        A half-open ``(first, stop)`` in rows, for the per-cell overlays every
        surface draws — captions, ids, marks. Those loops are written per *cell*
        and a scrolled view exposes a sliver of a picture that is thousands of
        them: testing each cell against the exposed rectangle still costs a
        rectangle per cell, which is what made a repaint of a metatile map with
        its ids on take an eighth of a second. Turning the band into a row range
        first makes the cost the band's rather than the picture's.

        **One row of slack either side.** A caption sits at the top or the bottom
        of its cell, so the row above and the row below can each put ink inside
        the exposed band without their cell reaching into it.
        """
        step = row_height * self._zoom_y
        if step <= 0:
            return 0, 0
        return (
            max(0, int(exposed.top() // step) - 1),
            int(exposed.bottom() // step) + 2,
        )

    def _exposed_columns(self, exposed: QRect, column_width: int) -> tuple[int, int]:
        """:meth:`_exposed_rows` the other way — the columns ``exposed`` touches.

        The slack matters more across than down: a caption is drawn *rightward*
        from its cell's left edge, so a cell one column left of the band can put
        several characters inside it.
        """
        step = column_width * self._zoom_x
        if step <= 0:
            return 0, 0
        return (
            max(0, int(exposed.left() // step) - 1),
            int(exposed.right() // step) + 2,
        )

    def _image_pixel(self, pos: QPointF) -> tuple[int, int]:
        """The image pixel under a device position — the inverse of the above.

        Floored, so the answer is the pixel the point is *inside*. Callers decide
        what to do about a point outside the picture; this only undoes the scale.
        """
        return (int(pos.x() // self._zoom_x), int(pos.y() // self._zoom_y))

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
        while a pan is merely armed, and the surface's own otherwise.

        The claimed backing wears **the hands and nothing else**: a pan answers
        out there exactly as it does over the art (:meth:`claim_background`), so a
        pointer that changed shape on the way in would say otherwise — while the
        surface's own cursor is a promise about its content (the canvas's cross
        says "this paints"), which the grey cannot keep.
        """
        if self._panning:
            hand = Qt.CursorShape.ClosedHandCursor
        elif self._pan_active:
            hand = Qt.CursorShape.OpenHandCursor
        else:
            hand = None
        _set_cursor(self, hand if hand is not None else self._pan_cursor())
        if self._backing is not None:
            _set_cursor(self._backing, hand)

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
        as a user is concerned, so both gestures answer out there — a Ctrl+wheel
        zooms and an armed space-drag pans — and a click focuses, which is what
        puts the keys that address this sheet (Shift+Left/Right for its width,
        the arrows for its pick) on it.

        Filtered on the viewport rather than handled by a QScrollArea subclass so
        the whole gesture stays one class: the events land on the viewport (the
        surface is its child), and the filter gets them before the scroll area
        turns them into a scroll or takes the focus for itself.
        """
        self._backing = scroll.viewport()
        self._backing.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001 — Qt override
        """The backing's Ctrl+wheel, pan drag and click, answered as our own.

        The wheel's position is mapped into this widget and **clamped to it**, so
        a zoom started out on the backing anchors on the nearest content pixel
        rather than on a coordinate outside the picture — the anchoring
        arithmetic reads ``pos`` as content pixels times the zoom
        (:func:`zoom_anchored`), and a point past the edge would ask it to hold
        still something that is not there. A plain wheel is left alone and
        scrolls as usual.

        A **pan drag** needs no mapping at all: it is measured in global screen
        deltas, so where it started says nothing about what it moves. The whole
        drag is taken here rather than only its press — the implicit mouse grab
        belongs to the widget the press was accepted on, so every move and the
        release land on the backing too.

        Any other **press** takes the focus and is then left to travel on. Qt has
        already handed focus to the scroll area by the time this runs — it is
        given before the press is delivered — so this is the surface taking it
        back, and it is what a click on the grey has to do for the keys addressed
        to this sheet to reach it. Not consumed: the press is still the scroll
        area's to do whatever else it does with.
        """
        et = event.type()
        if et == QEvent.Type.MouseButtonPress and self._pan_press(event):
            return True
        if et == QEvent.Type.MouseMove and self._pan_move(event):
            return True
        if et == QEvent.Type.MouseButtonRelease and self._pan_release(event):
            return True
        if isinstance(obj, QWidget) and et == QEvent.Type.MouseButtonPress:
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


class PanOnlyMouse:
    """The mouse handlers of a surface whose only mouse gesture is the pan.

    A sheet that shows rather than edits — the subsprite sheet, the animation
    frame — answers the space-drag and nothing else, so its three handlers are
    :class:`PanZoomSurface`'s helpers with the widget's own behaviour behind
    them. Mixed in before the surface (``class Frame(PanOnlyMouse,
    PanZoomSurface, QWidget)``).
    """

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if not self._pan_press(event):
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if not self._pan_move(event):
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if not self._pan_release(event):
            super().mouseReleaseEvent(event)


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


def wheel_zoom(scroll: QScrollArea, spin, steps: int, pos) -> None:  # noqa: ANN001
    """One Ctrl+wheel report, applied to a whole-number zoom ``spin``.

    ``steps`` notches along the spin's own range, clamped to its ends, and
    anchored on the content pixel under ``pos`` (:func:`zoom_anchored`). The
    sheets' zooms are integer levels, so a notch is one level of the spin.
    """
    new = min(max(spin.value() + steps, spin.minimum()), spin.maximum())
    zoom_anchored(scroll, spin, new, pos)


def zoom_spin(
    zoom_range: tuple[int, int],
    value: int,
    on_change: Callable[[int], None],
    tip: str,
) -> QSpinBox:
    """A floating window's own Zoom: whole magnifications, shown as ``4x``.

    Whole numbers because what these windows magnify is small — a frame, a
    sheet of subsprites — and there is nothing to reduce. Committed on finish,
    like every view spin (:func:`value_spin`).
    """
    spin = QSpinBox()
    spin.setRange(*zoom_range)
    spin.setValue(value)
    spin.setKeyboardTracking(False)
    spin.setSuffix("x")
    spin.setToolTip(tip)
    spin.valueChanged.connect(on_change)
    return spin


def mount_surface(
    surface: PanZoomSurface,
    *,
    spin: QSpinBox | None = None,
    align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
) -> QScrollArea:
    """``surface`` in a scroll area of its own, with both gestures wired.

    The space-drag pans the area (:func:`pan_scroll_area`); a Ctrl+wheel steps
    ``spin`` anchored on the pointer (:func:`wheel_zoom`), and a surface without
    one wires its own zoom. The backing around the surface is claimed as part of
    it (:meth:`PanZoomSurface.claim_background`): a sheet is usually smaller
    than its window, and the grey is where the pointer is when it wants zooming.
    Not resizable — the surface sizes itself to its content at its zoom.
    """
    scroll = QScrollArea()
    scroll.setWidget(surface)
    scroll.setAlignment(align)
    scroll.setWidgetResizable(False)
    surface.claim_background(scroll)
    surface.pan_requested.connect(lambda dx, dy: pan_scroll_area(scroll, dx, dy))
    if spin is not None:
        surface.zoom_requested.connect(
            lambda steps, pos: wheel_zoom(scroll, spin, steps, pos)
        )
    return scroll


class SpacePanFilter(QObject):
    """Space arms a surface's pan wherever focus sits in its window.

    Filtered on the **application** rather than handled in ``keyPressEvent``,
    because a key press goes to the focused widget alone: with focus on a Zoom
    spin — where magnifying leaves it, and having magnified is the usual reason
    to want to pan — the press reached a widget that does nothing with it. The
    rule the main window's own space pan follows
    (:meth:`~celpix.ui.main_window.navigation.NavigationMixin._handle_space_pan`).

    Any widget of ``window`` and nothing outside it: only one window can be the
    one being typed into. ``yields`` names the widgets space still belongs to —
    a combo drops its list on it, a cell editor types it — and ``extra_key``
    takes one more key press for the window while the filter is looking
    (True when consumed). Auto-repeat is swallowed rather than acted on: holding
    space fires press after press, and each would re-arm a mode already on.

    Parented to ``window``, so it leaves the application's filter list when the
    window goes.
    """

    def __init__(
        self,
        window: QWidget,
        surface: PanZoomSurface,
        *,
        yields: Callable[[QObject], bool] | None = None,
        extra_key: Callable[[QEvent], bool] | None = None,
    ) -> None:
        super().__init__(window)
        self._window = window
        self._surface = surface
        self._yields = yields
        self._extra_key = extra_key
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # Qt override
        et = event.type()
        if et == QEvent.Type.WindowDeactivate and obj is self._window:
            # A hold that outlives the window's activation: the release lands in
            # whatever was raised over it and is never seen here, which would
            # leave the surface holding an open hand and eating the next press.
            self._surface.set_pan_mode(False)
            return False
        if et not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            return False
        if not (isinstance(obj, QWidget) and obj.window() is self._window):
            return False
        if event.key() == Qt.Key.Key_Space and not (
            self._yields is not None and self._yields(obj)
        ):
            if not event.isAutoRepeat():
                self._surface.set_pan_mode(et == QEvent.Type.KeyPress)
            return True
        return (
            et == QEvent.Type.KeyPress
            and self._extra_key is not None
            and self._extra_key(event)
        )
