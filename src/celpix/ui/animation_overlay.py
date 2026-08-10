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

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import (
    QApplication,
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
from celpix.ui.widgets import (
    Badge,
    PanZoomSurface,
    apply_badge,
    pan_scroll_area,
    select_combo_data,
    signals_blocked,
    zoom_anchored,
)
from celpix.ui.window_layout import WindowLayout

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

# How long a burst of view refreshes is allowed to coalesce into one recompose of
# the strip. Long enough to swallow a stroke's worth, short enough that an edit
# appears to land here at the same time it lands on the canvas.
REFRESH_DEBOUNCE_MS = 120


class AnimationFrame(PanZoomSurface, QWidget):
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
        # Every frame of an object is drawn in one shared bounding box, so this is
        # *the* frame size rather than the current frame's. Kept so a step naming a
        # frame the file does not have can draw nothing at the size the others
        # take: sizing that step to its empty rectangle instead would collapse the
        # widget to a pixel and snap the scroll geometry once per step.
        self._frame_size = None
        self._zoom = DEFAULT_ZOOM
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_strip(self, strip: QImage, frame_size=None) -> None:  # noqa: ANN001 — QSize
        """Take the composed sheet every frame is a rectangle of, and their size."""
        self._strip = strip
        self._frame_size = frame_size
        self._update_size()
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

    def _has_content(self) -> bool:
        return not self._strip.isNull()

    def _update_size(self) -> None:
        """Size to the object's frame box, whatever this step happens to draw."""
        size = self._frame_size or self._source.size()
        self.setFixedSize(
            max(1, size.width() * self._zoom), max(1, size.height() * self._zoom)
        )

    # -- painting ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        # Guarded before the painter exists rather than after: a painter built and
        # abandoned is only harmless while CPython's refcounting ends it for us.
        if self._strip.isNull() or self._source.isEmpty():
            return
        painter = QPainter(self)
        # No smoothing: this is pixel art, and the magnification has to show the
        # pixels rather than average them.
        painter.scale(self._zoom, self._zoom)
        painter.drawImage(QPoint(0, 0), self._strip, self._source)
        painter.end()

    # -- interaction ---------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if self._pan_press(event):
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if self._pan_move(event):
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if self._pan_release(event):
            return
        super().mouseReleaseEvent(event)


class AnimationOverlay(QWidget):
    """Floating player for one sprite object's sequences."""

    # Asked for when the entry underneath has changed and the strip wants
    # recomposing. A signal rather than a direct call because the composing half
    # lives on the window (:mod:`celpix.ui.main_window.animation`), and it is
    # **debounced**: the strip is the object's every frame, untrimmed, and the
    # window refreshes on things that arrive in bursts — each pixel of a stroke,
    # each step of a drag. One recompose per burst is imperceptible here and the
    # difference between a player that is open and one that is in the way.
    refresh_requested = Signal()

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
        # A frame is small and the window is not, so most of what is on screen is
        # backing — and a zoom gesture that only answers over the sprite itself
        # would be aimed at the wrong half of the window most of the time.
        self._frame.claim_background(self._scroll)

        self._sequence = QComboBox()
        self._sequence.setToolTip(
            "Which of the file's animation sequences to play\n"
            "A sequence is a run of (duration, frame) steps"
        )
        self._sequence.currentIndexChanged.connect(self._on_sequence_changed)

        # The three transport buttons take no focus, so stepping or starting the
        # playback leaves it on the frame rather than on the button last pressed.
        # Space is this window's pan gesture and is claimed window-wide
        # (:meth:`eventFilter`), so a button that held focus could not click
        # itself with it anyway — it would just wear a focus ring for nothing.
        self._play = QPushButton("Play")
        self._play.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._play.setCheckable(True)
        self._play.toggled.connect(self._on_play_toggled)
        self._prev = QPushButton("<")
        self._prev.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._prev.setToolTip("Step back one step of the sequence")
        self._prev.clicked.connect(lambda: self._advance(-1))
        self._next = QPushButton(">")
        self._next.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._next.setToolTip("Step forward one step of the sequence")
        self._next.clicked.connect(lambda: self._advance(1))

        self._rate = QSpinBox()
        self._rate.setRange(*RATE_RANGE)
        self._rate.setValue(DEFAULT_RATE)
        self._rate.setKeyboardTracking(False)
        self._rate.setSuffix(" Hz")
        self._rate.setToolTip(
            "How many ticks pass per second\n"
            "A step's duration is counted in ticks; 60 reads\n"
            "them as console frames"
        )
        self._rate.valueChanged.connect(self._on_rate_changed)

        self._zoom = QSpinBox()
        self._zoom.setRange(*ZOOM_RANGE)
        self._zoom.setValue(DEFAULT_ZOOM)
        self._zoom.setKeyboardTracking(False)
        self._zoom.setSuffix("x")
        self._zoom.setToolTip(
            "How big the frame is drawn here\n"
            "This window's own zoom, separate from the main view\n"
            "Ctrl+wheel over the frame steps it; space-drag pans"
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
        # Kept across runs like the main window's: the size that fits the sprite
        # being animated is the user's to find, and finding it again every time
        # the player opens is the tax celpix.ui.window_layout exists to stop.
        self._layout_memory = WindowLayout(self, "layout/animation-player")
        # A remembered position counts as already placed (see the overlay this
        # follows, :mod:`celpix.ui.decompress_overlay`).
        self._positioned = self._layout_memory.restore()

        # Single-shot and re-armed per step, rather than one repeating timer at
        # the tick rate: a step's duration is known when it starts, so this waits
        # exactly that long and wakes once instead of counting down.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._advance(1))

        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.setInterval(REFRESH_DEBOUNCE_MS)
        self._pending.timeout.connect(self.refresh_requested)

        # The pan gesture's space key is taken off an application filter rather
        # than a key event of this window, so it answers wherever focus sits in
        # here - see eventFilter.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

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
        self._frame.set_strip(strip, rects[0].size() if rects else None)
        self._rects = rects
        self._inferred = inferred
        self._frames = len(rects)
        # **The same sequences means the same session.** This is called again on
        # every refresh of the entry underneath — a pixel edit, a palette change,
        # a zoom — so that the strip follows the art. Rebuilding the picker
        # unconditionally would snap the combo back to the first sequence and the
        # step back to 1 each time, which during playback restarts the animation
        # several times a second. What the user picked survives anything that did
        # not change what there is to pick.
        same = sequences == self._sequences and self._sequence.count()
        keep = self._sequence.currentData() if same else None
        self._sequences = sequences
        if not same:
            # Only the sequences that hold anything are offered, since a file has
            # room for 16 or 32 and fills a handful — but each keeps its own
            # number, which is what the file calls it.
            #
            # Blocked while it is refilled: the clear emits currentIndexChanged
            # and so does the first insert, and each would reset the step of a
            # sequence the user has not chosen yet.
            with signals_blocked(self._sequence):
                self._sequence.clear()
                for at, sequence in enumerate(sequences):
                    if not sequence:
                        continue
                    steps = len(sequence.steps)
                    self._sequence.addItem(
                        f"Sequence {at} - {steps} step{'s' if steps != 1 else ''}", at
                    )
            self._step = 0
        elif keep is not None:
            select_combo_data(self._sequence, keep)
        self._refresh()
        if not self.isVisible():
            if not self._positioned and self.parentWidget() is not None:
                anchor = self.parentWidget().frameGeometry().topRight()
                self.move(anchor + QPoint(12, 0))
                self._positioned = True
            self.show()
            # The frame takes the focus, not the picker, which would otherwise
            # have it as the first widget of the layout: the picker is a
            # QComboBox and space drops its list open, and that is the one place
            # in this window the pan gesture yields (:meth:`eventFilter`).
            self._frame.setFocus()

    def request_refresh(self) -> None:
        """Ask for the strip to be recomposed shortly, coalescing a burst into one."""
        if self.isVisible():
            self._pending.start()

    def hide_overlay(self) -> None:
        """Hide and stop (the entry changed, or it is not an object any more)."""
        self._play.setChecked(False)
        self._timer.stop()
        self._pending.stop()
        if self.isVisible():
            self.hide()

    def closeEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Stop playing when the window is closed from its own frame.

        The one way out of this window that does not go through
        :meth:`hide_overlay`, and the timer has to hear about it: closed while
        playing, it would go on firing against the strip it was holding for as
        long as the app stayed open — and the window-side sync leaves a hidden
        player alone, so nothing else would ever stop it. The pan mode goes down
        for the same reason, a space release landing anywhere else being a
        release this window never sees.
        """
        self._play.setChecked(False)
        self._timer.stop()
        self._frame.set_pan_mode(False)
        super().closeEvent(event)

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
                    "Which block holds frames and which durations is\n"
                    "read off the data, not declared by the file.",
                )
            )
        missing = unknown_frames((sequence,), self._frames)
        if missing:
            parts.append(
                Badge(
                    f"{missing} missing frame{'s' if missing != 1 else ''}",
                    "This sequence names frames the file does not hold.\n"
                    "Those steps show as blank.",
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

        The tile source dock's wheel zoom over this window's own scroll area,
        with its levels: whole magnifications the spin steps through, so a notch
        is one step of it.
        """
        spin = self._zoom
        new = min(max(spin.value() + steps, spin.minimum()), spin.maximum())
        zoom_anchored(self._scroll, spin, new, pos)

    def _pan(self, dx: int, dy: int) -> None:
        """Shift the scroll view by a space-drag delta (device pixels)."""
        pan_scroll_area(self._scroll, dx, dy)

    # -- space arms the pan --------------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001 — Qt override
        """Claim the space bar for the pan wherever focus sits in this window.

        Filtered on the application rather than handled in ``keyPressEvent``,
        because a key press goes to the focused widget alone and a hold sends
        exactly one: with focus on the Zoom spin — where magnifying the frame
        leaves it, and having magnified it is the usual reason to want to pan —
        the press reached a widget that does nothing with it, and the gesture was
        dead until the picture happened to get clicked. Taking it here instead is
        the rule the main window's own space pan follows
        (:meth:`~celpix.ui.main_window.navigation.NavigationMixin._handle_space_pan`).

        Any widget of *this* window, and nothing outside it: the main window's
        filter arms its own surfaces off the same key, and only one of the two
        windows can be the one being typed into. The sequence picker is the
        exception, a QComboBox dropping its list open on space; the list itself
        is a window of its own, so an open popup falls outside this test.
        """
        et = event.type()
        if et == QEvent.Type.WindowDeactivate and obj is self:
            # A hold that outlives the window's activation: the release lands in
            # whatever was raised over it and is never seen here, which would
            # leave the frame holding an open hand and eating the next press.
            self._frame.set_pan_mode(False)
        elif (
            et in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
            and event.key() == Qt.Key.Key_Space
            and isinstance(obj, QWidget)
            and obj.window() is self
            and not isinstance(obj, QComboBox)
        ):
            # Auto-repeat is swallowed rather than acted on: holding space fires
            # press after press, and each would re-arm a mode already on.
            if not event.isAutoRepeat():
                self._frame.set_pan_mode(et == QEvent.Type.KeyPress)
            return True
        return super().eventFilter(obj, event)
