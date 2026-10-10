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

import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QPainter
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


def whole_physical_pixels(zoom: float, ratio: float) -> float:
    """``zoom`` as the physical pixels one image pixel covers, made whole.

    ``ratio`` is the screen's physical pixels per logical one — 1.25 or 1.5 on a
    Windows display scaled to 125% or 150%. A zoom of 1 there is 1.5 physical
    pixels a pixel, and a nearest-neighbour blit can only land that as 2, 1, 2,
    1, …: art drawn at uneven widths, the one distortion pixel art cannot carry.
    So a magnification is rounded to whole physical pixels (half up, the way the
    blit itself rounds its edges), and a reduction to a whole fraction of one —
    1/2, 1/3 — so every image pixel is drawn the same size either way. A whole
    ``ratio`` with a listed zoom comes back unchanged.
    """
    units = zoom * ratio
    if units >= 1:
        return float(math.floor(units + 0.5))
    return 1.0 / max(1, math.floor(1 / units + 0.5))


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
    # ``pan_requested(dx, dy)`` in logical pixels, ``zoom_requested(steps, pos)``
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
    #: The screen's physical pixels per logical pixel, as last read — 0 until
    #: the first read (:meth:`_ratio`), and re-read when the widget is shown or
    #: moves to a screen at another scale (:meth:`event`).
    _device_ratio: float = 0.0
    #: :meth:`_zoom_xy`'s and :meth:`_physical_zoom`'s answers and what they
    #: were worked out from: each pair is read once per cell inside paint loops,
    #: and working it out is not free.
    _zoom_memo: tuple[object, tuple[float, float], tuple[float, float]] | None = None
    #: What a high-resolution wheel has turned that is not yet a whole notch
    #: (:meth:`_report_zoom`), in the wheel's 1/8-degree units.
    _wheel_rest: int = 0

    def _ratio(self) -> float:
        """The screen's physical pixels per logical pixel (1.5 at 150%)."""
        if not self._device_ratio:
            self._device_ratio = self.devicePixelRatioF() or 1.0
        return self._device_ratio

    def _physical_zoom(self) -> tuple[float, float]:
        """Image pixels to **physical** pixels, per axis: the zoom made whole in
        physical pixels (:func:`whole_physical_pixels`), times the aspect.

        The aspect multiplies *after* the rounding, so a 1:2 pixel is still
        exactly twice as tall as wide, and an 8:7 one is still the deliberately
        uneven run it is at any scale.
        """
        return self._zooms()[2]

    def _zoom_xy(self) -> tuple[float, float]:
        """Image pixels to logical pixels, per axis — what every surface draws,
        measures and hit-tests through (:attr:`_zoom_x`, :attr:`_zoom_y`).

        The physical zoom over the screen's ratio, so a ``painter.scale`` by it
        lands every image pixel on the same whole number of physical pixels. On
        an unscaled screen it is just the zoom times the aspect.
        """
        return self._zooms()[1]

    def _zooms(self) -> tuple[object, tuple[float, float], tuple[float, float]]:
        """The memo behind :meth:`_zoom_xy` and :meth:`_physical_zoom`."""
        key = (self._zoom, self._aspect_scale, self._ratio())
        memo = self._zoom_memo
        if memo is None or memo[0] != key:
            ratio = key[2]
            base = whole_physical_pixels(self._zoom, ratio)
            px, py = base * self._aspect_scale[0], base * self._aspect_scale[1]
            memo = self._zoom_memo = (key, (px / ratio, py / ratio), (px, py))
        return memo

    @property
    def _zoom_x(self) -> float:
        """Image pixels to logical pixels, horizontally (:meth:`_zoom_xy`).

        Equal to :attr:`_zoom_y` on a square pixel, which is every existing view,
        so a surface that reads both is unchanged wherever nothing has been set.
        """
        return self._zoom_xy()[0]

    @property
    def _zoom_y(self) -> float:
        """Image pixels to logical pixels, vertically — see :attr:`_zoom_x`."""
        return self._zoom_xy()[1]

    def event(self, event) -> bool:  # noqa: ANN001 — Qt override
        """Re-fit when the screen's scale may have changed under the widget.

        The zoom is whole in *physical* pixels (:meth:`_physical_zoom`), so a
        window dragged to a monitor at another scale draws at another logical
        size. Show is checked as well as the change itself: a surface measured
        before it had a screen read the primary one's ratio.
        """
        if event.type() in (QEvent.Type.DevicePixelRatioChange, QEvent.Type.Show):
            ratio = self.devicePixelRatioF() or 1.0
            if ratio != self._device_ratio:
                self._device_ratio = ratio
                self._update_size()
                self.update()
        return super().event(event)

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
        """``width`` x ``height`` image pixels as whole logical pixels.

        What every surface's ``_update_size`` measures with. The far edge is
        rounded the way the blit rounds it (:func:`_edge`), in physical pixels,
        and the widget then takes every logical pixel that edge reaches into —
        so a 7:8 pixel does not lose a row off the bottom, nor a scaled screen a
        physical column off the side. Floored at 1 so an empty picture still has
        a widget.
        """
        ratio = self._ratio()
        px, py = self._physical_zoom()
        return (
            max(1, math.ceil(_edge(width * px) / ratio - 1e-9)),
            max(1, math.ceil(_edge(height * py) / ratio - 1e-9)),
        )

    def _scaled_rect(self, x: int, y: int, width: int, height: int) -> QRect:
        """An image-pixel rectangle in logical pixels.

        Each edge is rounded on its own — right edge from ``x + width``, not from
        a rounded width — so abutting rectangles keep abutting under a fractional
        aspect instead of leaving a seam between every pair. Rounded half up, as
        the blit rounds the picture's own edges (:func:`_edge`), so an outline
        drawn here and the pixel it outlines start on the same column.
        """
        zx, zy = self._zoom_xy()
        left, top = _edge(x * zx), _edge(y * zy)
        right, bottom = _edge((x + width) * zx), _edge((y + height) * zy)
        return QRect(left, top, right - left, bottom - top)

    # -- drawing in physical pixels -------------------------------------------
    # On a screen scaled to 125% or 150% the art's edges are whole *physical*
    # pixels (:meth:`_physical_zoom`) that fall between logical ones, so a mark
    # placed in logical pixels — :meth:`_scaled_rect` — lands up to a physical
    # pixel off the edge it marks: a ring over a column of the art, a backing
    # with a sliver of a tile showing inside it. Whatever has to sit on the art's
    # edges is placed and drawn in physical pixels instead, through these. On an
    # unscaled screen physical and logical pixels are the same, and so is all of
    # it.

    def _device_rect(self, x: int, y: int, width: int, height: int) -> QRect:
        """An image-pixel rectangle in physical pixels: the very span the blit
        covers, every edge rounded on its own as :meth:`_scaled_rect` rounds."""
        px, py = self._physical_zoom()
        left, top = _edge(x * px), _edge(y * py)
        right, bottom = _edge((x + width) * px), _edge((y + height) * py)
        return QRect(left, top, right - left, bottom - top)

    def _device_area(self, exposed: QRect) -> QRect:
        """``exposed`` (logical) as the physical pixels it covers."""
        ratio = self._ratio()
        return QRect(
            QPoint(
                math.floor(exposed.left() * ratio), math.floor(exposed.top() * ratio)
            ),
            QPoint(
                math.ceil((exposed.right() + 1) * ratio) - 1,
                math.ceil((exposed.bottom() + 1) * ratio) - 1,
            ),
        )

    def _device_width(self, logical: int) -> int:
        """A stroke ``logical`` pixels wide as whole physical pixels, at least
        one — so a ring keeps its weight on a scaled screen and every side of it
        comes out the same width."""
        return max(1, _edge(logical * self._ratio()))

    @contextmanager
    def _device_pixels(self, painter: QPainter) -> Iterator[None]:
        """Draw in physical pixels for the block: the screen's scale undone.

        Saved and restored round the block, so a pen, a font or a clip set
        inside does not outlive it.
        """
        ratio = self._ratio()
        painter.save()
        if ratio != 1:
            painter.scale(1 / ratio, 1 / ratio)
        try:
            yield
        finally:
            painter.restore()

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
        """The image pixel drawn under a logical position — the inverse of the
        blit, not of the scale.

        A fractional scale (an 8:7 aspect, the 0.5 level) draws pixel *i* over
        the physical columns from ``_edge(i·z)`` to ``_edge((i+1)·z)``, and plain
        ``x // z`` disagrees with that wherever the rounding moved an edge — the
        pencil then painted the pixel beside the one under the cursor. So the
        physical pixel under ``pos`` is found first, and then the image pixel
        whose drawn span holds it: ``_edge(i·z) <= X`` is ``i·z < X + ½``.
        Callers decide what to do about a point outside the picture.
        """
        ratio = self._ratio()
        px, py = self._physical_zoom()
        # The epsilon absorbs the float error of a logical position that is an
        # exact physical one (4/3 * 1.5 must be pixel 2, not 1.999…).
        col = math.floor(pos.x() * ratio + 1e-6)
        row = math.floor(pos.y() * ratio + 1e-6)
        return (
            math.ceil((col + 0.5) / px) - 1,
            math.ceil((row + 0.5) / py) - 1,
        )

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
        # One step per 120-unit notch, counted across events: a high-resolution
        # wheel sends a notch as eight events of 15, and a touchpad as dozens,
        # and a notch is one level however many events carry it. The remainder
        # is dropped when the direction turns, so a reversal answers at once
        # instead of first paying back what was left over.
        if (dy > 0) != (self._wheel_rest > 0) and self._wheel_rest:
            self._wheel_rest = 0
        self._wheel_rest += dy
        steps = int(self._wheel_rest / 120)
        if steps == 0:
            return False
        self._wheel_rest -= steps * 120
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


def _edge(value: float) -> int:
    """A scaled edge as a whole pixel, rounded half **up** — the rule Qt's
    nearest-neighbour blit places a scaled image's pixel edges by, where
    Python's ``round`` would send every other tie the other way."""
    return math.floor(value + 0.5)


def pan_scroll_area(scroll: QScrollArea, dx: int, dy: int) -> None:
    """Shift ``scroll`` by a space-drag delta (logical pixels).

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
    # The scale the surface actually draws at, before and after, rather than
    # the spin's level: the two part under a pixel aspect and on a scaled
    # screen (:meth:`PanZoomSurface._zoom_xy`), and anchoring on the level
    # would slide the art from under the pointer by the difference.
    surface = scroll.widget()
    drawn = isinstance(surface, PanZoomSurface)
    before = surface._zoom_xy() if drawn else (old, old)
    # The cursor's spot in the viewport, and the content pixel it sits on now.
    view_x, view_y = pos.x() - hbar.value(), pos.y() - vbar.value()
    img_x, img_y = pos.x() / before[0], pos.y() / before[1]
    spin.setValue(new)  # re-renders and resizes the view synchronously
    after = surface._zoom_xy() if drawn else (new, new)
    hbar.setValue(round(img_x * after[0] - view_x))
    vbar.setValue(round(img_y * after[1] - view_y))


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
