"""The animation player — a floating window that steps a sprite object's frames.

(:class:`AnimationOverlay` is a tool window beside the view, not drawn over it;
the class name and the ``layout/animation-player`` settings key are what the rest
of the code and a user's stored layout know it by.)

A sprite object's canvas shows its frames as a *strip*, laid out in file order,
which is a picture of the file. The table beside them says which frames play, in
what order and for how long (:mod:`celpix.core.animation`), and this is the
window that walks it — the picture of the motion.

It is a floating tool window (:class:`~celpix.ui.tool_window.ToolWindow`):
floats above the main window, takes no taskbar slot, placed beside it on the
first show and left where the user drags it after. Below the frame sits the tool
windows' status bar and :class:`~celpix.ui.widgets.Badge` — the picture cannot
show that a sequence names frames the file does not have, so it is said in words.

**Its zoom is its own**, deliberately not the main view's. That is why the frame
is a widget of this module rather than a :class:`~celpix.ui.canvas.Canvas`: the
canvas carries slot mapping, selection and a configurable lattice that an
animation frame has no use for, and its zoom belongs to the document's view.
Instead this follows the split the tile source dock makes with its panel
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
    QMenu,
    QPushButton,
    QSpinBox,
    QToolButton,
    QWidget,
)

from celpix.core.animation import Sequence, unknown_frames
from celpix.ui.panzoom import (
    PanOnlyMouse,
    PanZoomSurface,
    SpacePanFilter,
    mount_surface,
    zoom_spin,
)
from celpix.ui.tool_window import ToolWindow
from celpix.ui.widgets import (
    Badge,
    add_labelled,
    counted,
    select_combo_data,
    signals_blocked,
)

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


class AnimationFrame(PanOnlyMouse, PanZoomSurface, QWidget):
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

    def strip(self) -> QImage:
        """The composed sheet the frames are cut from."""
        return self._strip

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
        self.setFixedSize(*self._scaled_size(size.width(), size.height()))

    # -- painting ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        # Guarded before the painter exists rather than after: a painter built and
        # abandoned is only harmless while CPython's refcounting ends it for us.
        if self._strip.isNull() or self._source.isEmpty():
            return
        painter = QPainter(self)
        # No smoothing: this is pixel art, and the magnification has to show the
        # pixels rather than average them.
        painter.scale(self._zoom_x, self._zoom_y)
        painter.drawImage(QPoint(0, 0), self._strip, self._source)
        painter.end()


class AnimationOverlay(ToolWindow):
    """Floating player for one sprite object's sequences.

    :attr:`refresh_requested` asks for the strip to be recomposed, through the
    shell's debounce: the strip is the object's every frame, untrimmed, and the
    main window refreshes on things that arrive in bursts
    (:mod:`celpix.ui.main_window.animation`).
    """

    # Export ▸ one of four: (as GIF rather than PNGs, every sequence rather than
    # the one showing). Asked of the window, which owns the dialogs, the default
    # folder and the entry's name; the frames themselves are this window's to
    # hand over (:meth:`export_source`).
    export_requested = Signal(bool, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, "Animation", "layout/animation-player", (420, 420))

        # What is being played. Held rather than read back off the model: the
        # window outlives a repaint and must not depend on the document still
        # being the one it was opened for.
        self._sequences: tuple[Sequence, ...] = ()
        self._rects: list[QRect] = []
        self._frames = 0
        self._step = 0
        self._inferred = False

        self._frame = AnimationFrame()

        self._sequence = QComboBox()
        self._sequence.setToolTip(
            "Animation sequence to play\n"
            "A sequence is a list of (duration, frame) steps"
        )
        self._sequence.currentIndexChanged.connect(self._on_sequence_changed)

        # The three transport buttons take no focus, so stepping or starting the
        # playback leaves it on the frame rather than on the button last pressed.
        # Space is this window's pan gesture and is claimed window-wide
        # (:class:`~celpix.ui.widgets.SpacePanFilter`), so a button that held
        # focus could not click itself with it anyway — it would just wear a
        # focus ring for nothing.
        self._play = QPushButton("Play")
        self._play.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._play.setCheckable(True)
        self._play.toggled.connect(self._on_play_toggled)
        self._prev = QPushButton("<")
        self._prev.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._prev.setToolTip("Previous step")
        self._prev.clicked.connect(lambda: self._advance(-1))
        self._next = QPushButton(">")
        self._next.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._next.setToolTip("Next step")
        self._next.clicked.connect(lambda: self._advance(1))

        self._rate = QSpinBox()
        self._rate.setRange(*RATE_RANGE)
        self._rate.setValue(DEFAULT_RATE)
        self._rate.setKeyboardTracking(False)
        self._rate.setSuffix(" Hz")
        self._rate.setToolTip(
            "Ticks per second\nStep durations are in ticks; 60 = console frames"
        )
        self._rate.valueChanged.connect(self._on_rate_changed)

        self._zoom = zoom_spin(
            ZOOM_RANGE,
            DEFAULT_ZOOM,
            self._frame.set_zoom,
            "Frame magnification (Ctrl+Scroll over the frame)\n"
            "Independent of the main view. Space+drag pans",
        )
        # A frame is small and the window is not, so most of what is on screen is
        # backing — and a zoom gesture that only answers over the sprite itself
        # would be aimed at the wrong half of the window most of the time.
        self._scroll = mount_surface(self._frame, spin=self._zoom)

        # One button, four choices: a menu rather than four buttons, since an
        # export is occasional and the header is the transport's.
        self._export = QToolButton()
        self._export.setText("Export")
        self._export.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._export.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._export.setToolTip(
            "Export sequences as animated GIFs or numbered PNGs\n"
            "Frames are written at 1x, timed at the Rate above"
        )
        menu = QMenu(self._export)
        self._export_one = [
            menu.addAction(
                "Export as &GIF…", lambda: self.export_requested.emit(True, False)
            ),
            menu.addAction(
                "Export as &PNG Sequence…",
                lambda: self.export_requested.emit(False, False),
            ),
        ]
        menu.addSeparator()
        menu.addAction(
            "Export &All as GIFs…", lambda: self.export_requested.emit(True, True)
        )
        menu.addAction(
            "Export All as P&NG Sequences…",
            lambda: self.export_requested.emit(False, True),
        )
        self._export.setMenu(menu)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self._sequence, 1)
        header.addWidget(self._prev)
        header.addWidget(self._play)
        header.addWidget(self._next)
        add_labelled(header, "Rate", self._rate, self._rate.toolTip())
        add_labelled(header, "Zoom", self._zoom, self._zoom.toolTip())
        header.addWidget(self._export)
        self.content.addLayout(header)
        self.content.addWidget(self._scroll, 1)

        # Single-shot and re-armed per step, rather than one repeating timer at
        # the tick rate: a step's duration is known when it starts, so this waits
        # exactly that long and wakes once instead of counting down.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._advance(1))

        # Space pans wherever focus sits in here — except on the sequence
        # picker, a QComboBox dropping its list open on space. The list itself
        # is a window of its own, so an open popup falls outside the filter.
        self._space_pan = SpacePanFilter(
            self, self._frame, yields=lambda obj: isinstance(obj, QComboBox)
        )

    def _pixel_surface(self) -> PanZoomSurface:
        return self._frame

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
                    label = f"Sequence {at} - {counted(len(sequence.steps), 'step')}"
                    # Said where it is not the default, in the status line's
                    # 1-based numbering: otherwise playback skipping the first
                    # steps after one pass looks like a fault.
                    if 0 < sequence.loop < len(sequence.steps):
                        label += f", loops to step {sequence.loop + 1}"
                    self._sequence.addItem(label, at)
            self._step = 0
        elif keep is not None:
            select_combo_data(self._sequence, keep)
        self._refresh()
        # The frame takes the focus, not the picker, which would otherwise have
        # it as the first widget of the layout: the picker is a QComboBox and
        # space drops its list open, and that is the one place in this window
        # the pan gesture yields.
        self.present(focus=self._frame)

    def hide_overlay(self) -> None:
        """Hide and stop (the entry changed, or it is not an object any more)."""
        self._stop()
        super().hide_overlay()

    def closeEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Stop playing when the window is closed from its own frame.

        The one way out of this window that does not go through
        :meth:`hide_overlay`, and the timer has to hear about it: closed while
        playing, it would go on firing against the strip it was holding for as
        long as the app stayed open — and the window-side sync leaves a hidden
        player alone, so nothing else would ever stop it.
        """
        self._stop()
        super().closeEvent(event)

    def _stop(self) -> None:
        self._play.setChecked(False)
        self._timer.stop()

    # -- export --------------------------------------------------------------
    def export_source(
        self, every: bool
    ) -> tuple[QImage, list[QRect], list[tuple[int, Sequence]], int]:
        """What an export writes: the strip, its frame rectangles, the
        ``(number, sequence)`` pairs to write and the tick rate.

        The sequence showing, or with ``every`` each one that holds a step —
        the ones the picker offers, under the numbers it gives them.
        """
        if every:
            chosen = [(at, seq) for at, seq in enumerate(self._sequences) if seq]
        else:
            at = self._sequence.currentData()
            chosen = [] if at is None else [(at, self._sequences[at])]
        return self._frame.strip(), self._rects, chosen, self._rate.value()

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
        """Step forward as playback does, or back through every step.

        Forward follows the sequence's own wrap (:meth:`Sequence.following`), so
        Next shows what plays next. Back is plain arithmetic over the whole run,
        which is what keeps a lead-in the loop skips reachable by hand.
        """
        sequence = self._current
        if sequence is None or not sequence.steps:
            return
        if by > 0:
            self._step = sequence.following(self._step)
        else:
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
            self.set_status("No sequences" if not self._sequences else "Empty")
            for control in (self._play, self._prev, self._next, *self._export_one):
                control.setEnabled(False)
            self._export.setEnabled(any(self._sequences) and bool(self._rects))
            return
        for control in (self._play, self._prev, self._next, *self._export_one):
            control.setEnabled(True)
        self._export.setEnabled(bool(self._rects))
        step = sequence.steps[self._step]
        known = 0 <= step.frame < self._frames
        self._frame.show_frame(self._rects[step.frame] if known else None)
        ticks = counted(step.duration, "tick")
        where = f"frame {step.frame}" if known else f"frame {step.frame} - not in file"
        self.set_status(
            f"Step {self._step + 1}/{len(sequence.steps)} - {where} - {ticks}",
            self._badge_for(sequence),
        )

    def _badge_for(self, sequence: Sequence) -> Badge | None:
        """What the picture cannot show about the sequence being played.

        Two things can be worth saying and they are not alternatives, so they
        compose rather than one winning: a badge is one line, but a run of
        clauses in it still reads, where showing only the more urgent would let
        the other go unsaid whenever both are true.

        A step naming a frame the file does not have is a **warning** — the table
        is making a claim the file contradicts, and the frame simply is not there
        (the corpus holds 7,019 such steps). That the split into a frame array
        and a duration array was *inferred* is a **fact**: nothing is wrong, but
        a reading shown as confidently as a confirmed one becomes a fact by
        repetition.
        """
        parts: list[Badge] = []
        if self._inferred:
            parts.append(
                Badge(
                    "inferred",
                    "Which array holds frames and which durations\n"
                    "was inferred from the data; the file does not say",
                )
            )
        missing = unknown_frames((sequence,), self._frames)
        if missing:
            parts.append(
                Badge(
                    counted(missing, "missing frame"),
                    "The sequence names frames the file does not hold\n"
                    "Those steps show as blank",
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
