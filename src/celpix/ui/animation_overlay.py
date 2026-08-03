"""The animation player — a floating window that steps a sprite object's frames.

A sprite object's canvas shows its frames as a *strip*, laid out in file order,
which is a picture of the file. The table beside them says which frames play, in
what order and for how long (:mod:`celpix.core.animation`), and this is the
window that walks it — the picture of the motion.

It is a `Qt.Tool` window on the decompression overlay's pattern
(:mod:`celpix.ui.decompress_overlay`): floats above the main window, takes no
taskbar slot, placed beside it on the first show and left where the user drags it
after. Below the frame sits the same status bar and the same
:class:`~celpix.ui.widgets.Badge`, for the same reason — the picture cannot show
that a sequence names frames the file does not have, so it is said in words.

**Its zoom is its own**, deliberately not the main view's. That is why the frame
is a widget of this module rather than a :class:`~celpix.ui.canvas.Canvas`: the
canvas carries slot mapping, selection and a configurable lattice that an
animation frame has no use for, and its zoom belongs to the document's view.
Instead this follows the tile source panel's split
(:mod:`celpix.ui.tile_source_panel`) one level in — :class:`AnimationFrame`
reports Ctrl+wheel and space-drag and owns no state, while the window holds the
level in its Zoom spin and the scrolling in its scroll area.

**A tick is a blit, not a render.** Every frame of the strip is drawn in one
shared bounding box (:func:`~celpix.core.sprite.frame_bounds`), so frame *n* is a
fixed sub-rectangle of an image the main window has already composed: playing is
a matter of which rectangle to draw, and the zoom is a transform at paint time.
Nothing here re-renders, and nothing here reads the model.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from celpix.core.animation import Sequence, unknown_frames
from celpix.ui.widgets import Badge, apply_badge, signals_blocked

# The rate the durations are read at, and the range the spin offers. A duration is
# the authoring tool's own tick and not a time (``core.animation.Step``), so this
# is celPix's reading of it rather than the file's: one console frame, which is
# what the hardware these were drawn for ran at. Adjustable because nothing in the
# corpus or in the tool's writer proves it.
DEFAULT_RATE = 60
RATE_RANGE = (1, 240)

# Zoom is whole magnifications here, not the view's list: there is no reduction to
# offer — a frame is one object, not a file too big for the window — and the
# window's own spin is what steps it.
ZOOM_RANGE = (1, 16)
DEFAULT_ZOOM = 4

# A step whose duration is zero but which is not the terminator (0, 0): the file
# says "show this for no time", which as a timer interval is a busy loop. Shown
# for one tick instead, which is the shortest thing the format can mean.
MIN_TICKS = 1


class AnimationFrame(QWidget):
    """One frame of an already-composed strip, drawn at this window's zoom.

    Owns no zoom or scroll state of its own — both are reported and applied by
    the window, the tile source panel's division and for its reason: the level is
    a control's value, not this widget's.
    """

    zoom_requested = Signal(int, object)  # steps, QPointF cursor pos (widget)
    pan_requested = Signal(int, int)  # dx, dy

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._strip = QImage()
        # Null until a frame is picked, which is not the same as an empty one: a
        # step naming a frame the file does not have has nothing to draw, and
        # drawing frame 0 instead would be a plausible lie.
        self._source = QRect()
        self._zoom = DEFAULT_ZOOM
        self._pan_active = False
        self._panning = False
        self._pan_last = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_strip(self, strip: QImage) -> None:
        """Take the composed sheet every frame is a rectangle of."""
        self._strip = strip
        self.update()

    def show_frame(self, source: QRect | None) -> None:
        """Draw the strip's ``source`` rectangle — or nothing, for a missing frame."""
        self._source = source or QRect()
        self._update_size()
        self.update()

    def set_zoom(self, zoom: int) -> None:
        if zoom != self._zoom:
            self._zoom = max(1, zoom)
            self._update_size()
            self.update()

    def set_pan_mode(self, on: bool) -> None:
        """Arm/disarm space-drag panning (the window drives this off the space key)."""
        if self._pan_active == on:
            return
        self._pan_active = on
        if not on:
            self._panning = False
        self._apply_cursor()

    def _apply_cursor(self) -> None:
        if self._panning:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self._pan_active:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()

    def _update_size(self) -> None:
        self.setFixedSize(
            max(1, self._source.width() * self._zoom),
            max(1, self._source.height() * self._zoom),
        )

    # -- painting ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        painter = QPainter(self)
        if self._strip.isNull() or self._source.isEmpty():
            return
        # No smoothing: this is pixel art, and the magnification has to show the
        # pixels rather than average them.
        painter.scale(self._zoom, self._zoom)
        painter.drawImage(QPoint(0, 0), self._strip, self._source)

    # -- interaction ---------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if self._pan_active and event.button() == Qt.MouseButton.LeftButton:
            self._panning = True
            self._pan_last = event.globalPosition()
            self._apply_cursor()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if not self._panning:
            super().mouseMoveEvent(event)
            return
        # Global position, not widget-local: the widget shifts under the cursor as
        # the view scrolls, which would feed back into the delta.
        pos = event.globalPosition()
        delta = pos - self._pan_last
        self._pan_last = pos
        self.pan_requested.emit(round(delta.x()), round(delta.y()))
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if self._panning and event.button() == Qt.MouseButton.LeftButton:
            self._panning = False
            self._apply_cursor()  # back to the open hand (space may still be held)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """**Ctrl**+wheel zooms; a plain wheel falls through to the scroll area."""
        if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            event.ignore()
            return
        dy = event.angleDelta().y()
        if dy == 0 or self._strip.isNull():
            return
        # One step per 120-unit notch, but at least one so a high-resolution wheel
        # sending small deltas still zooms.
        steps = int(dy / 120) or (1 if dy > 0 else -1)
        self.zoom_requested.emit(steps, event.position())
        event.accept()


class AnimationOverlay(QWidget):
    """Floating player for one sprite object's sequences."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle("Animation")
        self._positioned = False

        # What is being played. Held rather than read back off the model: the
        # window outlives a repaint and must not depend on the document still
        # being the one it was opened for.
        self._sequences: tuple[Sequence, ...] = ()
        self._rects: list[QRect] = []
        self._frames = 0
        self._step = 0
        self._inferred = False

        self._frame = AnimationFrame()
        self._frame.zoom_requested.connect(self._on_wheel_zoom)
        self._frame.pan_requested.connect(self._pan)
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._frame)
        self._scroll.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self._scroll.setWidgetResizable(False)

        self._sequence = QComboBox()
        self._sequence.setToolTip(
            "Which of the file's animation sequences to play.\n"
            "A sequence is a run of (duration, frame) steps; the\n"
            "file has room for 16 or 32 and usually fills a few."
        )
        self._sequence.currentIndexChanged.connect(self._on_sequence_changed)

        self._play = QPushButton("Play")
        self._play.setCheckable(True)
        self._play.toggled.connect(self._on_play_toggled)
        self._prev = QPushButton("<")
        self._prev.setToolTip("Step back one step of the sequence.")
        self._prev.clicked.connect(lambda: self._advance(-1))
        self._next = QPushButton(">")
        self._next.setToolTip("Step forward one step of the sequence.")
        self._next.clicked.connect(lambda: self._advance(1))

        self._rate = QSpinBox()
        self._rate.setRange(*RATE_RANGE)
        self._rate.setValue(DEFAULT_RATE)
        self._rate.setKeyboardTracking(False)
        self._rate.setSuffix(" Hz")
        self._rate.setToolTip(
            "How many ticks pass per second.\n"
            "A step's duration is counted in the authoring tool's\n"
            "own ticks, and nothing in the files says what one was\n"
            "worth; 60 reads them as console frames."
        )
        self._rate.valueChanged.connect(self._on_rate_changed)

        self._zoom = QSpinBox()
        self._zoom.setRange(*ZOOM_RANGE)
        self._zoom.setValue(DEFAULT_ZOOM)
        self._zoom.setKeyboardTracking(False)
        self._zoom.setSuffix("x")
        self._zoom.setToolTip(
            "How big the frame is drawn here.\n"
            "This window's own zoom - the main view keeps its own,\n"
            "and neither follows the other. Ctrl+wheel over the\n"
            "frame steps it; hold space and drag to pan."
        )
        self._zoom.valueChanged.connect(self._frame.set_zoom)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self._sequence, 1)
        header.addWidget(self._prev)
        header.addWidget(self._play)
        header.addWidget(self._next)
        header.addWidget(QLabel("Rate"))
        header.addWidget(self._rate)
        header.addWidget(QLabel("Zoom"))
        header.addWidget(self._zoom)

        self._status = QStatusBar()
        self._status.setSizeGripEnabled(False)
        self._badge = QLabel()
        self._badge.hide()
        self._status.addPermanentWidget(self._badge)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 0)
        layout.addLayout(header)
        layout.addWidget(self._scroll, 1)
        layout.addWidget(self._status)
        self.resize(420, 420)

        # Single-shot and re-armed per step, rather than one repeating timer at
        # the tick rate: a step's duration is known when it starts, so this waits
        # exactly that long and wakes once instead of counting down.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._advance(1))

    # -- presenting ----------------------------------------------------------
    def show_object(
        self,
        strip: QImage,
        rects: list[QRect],
        sequences: tuple[Sequence, ...],
        title: str,
        *,
        inferred: bool = False,
    ) -> None:
        """Present ``sequences`` over an already-composed ``strip``.

        ``rects`` is frame *n*'s rectangle within the strip, **untrimmed** — every
        slot the file has, not the run the canvas shows. A sequence may name a
        frame past the last drawn one (349 of the corpus's objects do), and the
        player has to be able to show what it names.
        """
        self.setWindowTitle(title)
        self._frame.set_strip(strip)
        self._rects = rects
        self._inferred = inferred
        self._frames = len(rects)
        live = [at for at, sequence in enumerate(sequences) if sequence]
        self._sequences = sequences
        # Only the sequences that hold anything are offered, since a file has room
        # for 16 or 32 and fills a handful — but each keeps its own number, which
        # is what the file calls it.
        # Blocked while it is refilled: the clear emits currentIndexChanged and so
        # does the first insert, and each would reset the step of a sequence the
        # user has not chosen yet.
        with signals_blocked(self._sequence):
            self._sequence.clear()
            for at in live:
                steps = len(sequences[at].steps)
                self._sequence.addItem(
                    f"Sequence {at} - {steps} step{'s' if steps != 1 else ''}", at
                )
        self._step = 0
        self._refresh()
        if not self.isVisible():
            if not self._positioned and self.parentWidget() is not None:
                anchor = self.parentWidget().frameGeometry().topRight()
                self.move(anchor + QPoint(12, 0))
                self._positioned = True
            self.show()

    def hide_overlay(self) -> None:
        """Hide and stop (the entry changed, or it is not an object any more)."""
        self._play.setChecked(False)
        self._timer.stop()
        if self.isVisible():
            self.hide()

    # -- playback ------------------------------------------------------------
    @property
    def _current(self) -> Sequence | None:
        at = self._sequence.currentData()
        return None if at is None else self._sequences[at]

    def _on_sequence_changed(self, _index: int) -> None:
        self._step = 0
        self._refresh()

    def _on_play_toggled(self, on: bool) -> None:
        self._play.setText("Pause" if on else "Play")
        if on:
            self._arm()
        else:
            self._timer.stop()

    def _on_rate_changed(self, _value: int) -> None:
        # Re-armed rather than left to finish: a rate change the user cannot see
        # take effect until the current step ends reads as one that did nothing.
        if self._play.isChecked():
            self._arm()

    def _advance(self, by: int) -> None:
        sequence = self._current
        if sequence is None or not sequence.steps:
            return
        self._step = (self._step + by) % len(sequence.steps)
        self._refresh()
        if self._play.isChecked():
            self._arm()

    def _arm(self) -> None:
        """Wait out the current step, then move on."""
        sequence = self._current
        if sequence is None or not sequence.steps:
            self._timer.stop()
            return
        ticks = max(MIN_TICKS, sequence.steps[self._step].duration)
        self._timer.start(round(ticks * 1000 / max(1, self._rate.value())))

    def _refresh(self) -> None:
        """Draw the current step and say what it is."""
        sequence = self._current
        if sequence is None or not sequence.steps:
            self._frame.show_frame(None)
            self._status.showMessage("No sequences" if not self._sequences else "Empty")
            apply_badge(self._badge, None)
            for control in (self._play, self._prev, self._next):
                control.setEnabled(False)
            return
        for control in (self._play, self._prev, self._next):
            control.setEnabled(True)
        step = sequence.steps[self._step]
        known = 0 <= step.frame < self._frames
        self._frame.show_frame(self._rects[step.frame] if known else None)
        ticks = f"{step.duration} tick{'s' if step.duration != 1 else ''}"
        where = f"frame {step.frame}" if known else f"frame {step.frame} - not in file"
        self._status.showMessage(
            f"Step {self._step + 1}/{len(sequence.steps)} - {where} - {ticks}"
        )
        apply_badge(self._badge, self._badge_for(sequence))

    def _badge_for(self, sequence: Sequence) -> Badge | None:
        """What the picture cannot show about the sequence being played.

        Two things can be worth saying and they are not alternatives, so they
        compose rather than one winning: a badge is one line, but a run of
        clauses in it still reads, where showing only the more urgent would let
        the other go unsaid whenever both are true.

        A step naming a frame the file does not have is a **warning** — the table
        is making a claim the file contradicts, and the frame simply is not there
        (the corpus holds 7,019 such steps). That the block split was *inferred*
        is a **fact**: nothing is wrong, but a reading shown as confidently as a
        confirmed one becomes a fact by repetition.
        """
        parts: list[Badge] = []
        if self._inferred:
            parts.append(
                Badge(
                    "inferred",
                    "How this file stores its animation is read off the\n"
                    "data, not off the tool that wrote it: the two blocks\n"
                    "are opaque byte arrays to the writer, and which holds\n"
                    "frames and which durations comes from the corpus.",
                )
            )
        missing = unknown_frames((sequence,), self._frames)
        if missing:
            parts.append(
                Badge(
                    f"{missing} missing frame{'s' if missing != 1 else ''}",
                    "This sequence names frames the file does not hold.\n"
                    "The table carries whatever was in the tool's buffer\n"
                    "past its terminator, so a step can name a frame that\n"
                    "was never drawn. Those steps show as blank.",
                    warning=True,
                )
            )
        if not parts:
            return None
        return Badge(
            " - ".join(part.text for part in parts),
            "\n\n".join(part.detail for part in parts),
            warning=any(part.warning for part in parts),
        )

    # -- zoom and pan --------------------------------------------------------
    def _on_wheel_zoom(self, steps: int, pos) -> None:  # noqa: ANN001 — QPointF
        """Ctrl+wheel over the frame, anchored on the pixel under the cursor.

        The tile source dock's wheel zoom over this window's own scroll area:
        ``pos`` is already in frame pixels times the zoom, so the pixel under the
        cursor divides out and putting it back is two scroll-bar writes.
        """
        old = self._zoom.value()
        new = min(max(old + steps, self._zoom.minimum()), self._zoom.maximum())
        if new == old:
            return
        hbar = self._scroll.horizontalScrollBar()
        vbar = self._scroll.verticalScrollBar()
        view_x, view_y = pos.x() - hbar.value(), pos.y() - vbar.value()
        img_x, img_y = pos.x() / old, pos.y() / old
        self._zoom.setValue(new)  # resizes the frame synchronously
        hbar.setValue(round(img_x * new - view_x))
        vbar.setValue(round(img_y * new - view_y))

    def _pan(self, dx: int, dy: int) -> None:
        """Shift the scroll view by a space-drag delta (device pixels)."""
        hbar = self._scroll.horizontalScrollBar()
        vbar = self._scroll.verticalScrollBar()
        hbar.setValue(hbar.value() - dx)
        vbar.setValue(vbar.value() - dy)

    # -- space arms the pan --------------------------------------------------
    def keyPressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        # Auto-repeat ignored: holding space fires press over and over, and each
        # would re-arm a mode that is already on.
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._frame.set_pan_mode(True)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._frame.set_pan_mode(False)
            event.accept()
            return
        super().keyReleaseEvent(event)
