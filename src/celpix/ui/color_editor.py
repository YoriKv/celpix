"""The shared color editor: edit one ARGB entry, with an eyedropper.

One widget serves every "edit this color" surface. Today that is the palette
dock's swatch grid (double-click an entry); it is deliberately free of any
palette/document knowledge so a future surface — a direct-color pixel, a
plugin's color parameter — can host the same control.

**Editing is 8-bit; storage may not be.** The channel inputs are full 0..255
(``docs/design-reference/palette-workflow.md`` calls this the Tile Molester
model), because the editor has no single target format: a Custom palette is
stored as ARGB, while a File/Offset palette is re-encoded through whatever
color codec is selected. So the loss is *shown* rather than imposed — set a
quantizer (:meth:`ColorEditor.set_quantizer`) and the "Stored as" swatch
previews the round trip the codec will actually perform. Nothing here quantizes
the value it emits; the codec does that at write time.

**The eyedropper is host-driven.** This widget only toggles
:attr:`ColorEditor.pick_toggled`; whoever hosts it decides what is pickable
(the main window arms the canvas and the palette grid) and feeds the sampled
color back with :meth:`ColorEditor.set_color`.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from celpix import resources
from celpix.ui.widgets import CommittingLineEdit, signals_blocked

# Channel order as edited, most significant first — the same order the hex
# field spells and the ARGB int packs.
_CHANNELS = (("A", 24), ("R", 16), ("G", 8), ("B", 0))
# What each channel letter stands for, so the one-letter caption, its slider and
# its spin can all answer the same question on hover.
_CHANNEL_NAMES = {"A": "Alpha", "R": "Red", "G": "Green", "B": "Blue"}


def _eyedropper_pixmap(color: QColor, size: int, ratio: float) -> QPixmap:
    """The bundled eyedropper glyph, recolored to ``color`` at device resolution.

    The art ships as a solid silhouette cropped to its opaque bounds; SourceIn
    keeps only its alpha and stamps the tint through, so one glyph tracks the
    theme in light and dark. Rasterized at ``ratio`` (the pixmap then reports
    ``size`` logical units) so a scaled display gets crisp edges rather than a
    stretched 1x bitmap, and centred in the square box since the glyph is taller
    than it is wide.
    """
    source = QImage.fromData(resources.read_bytes("icons", "eyedropper.png"))
    glyph = source.convertToFormat(QImage.Format.Format_ARGB32)
    tinting = QPainter(glyph)
    tinting.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    tinting.fillRect(glyph.rect(), color)
    tinting.end()
    box = round(size * ratio)
    scaled = QPixmap.fromImage(glyph).scaled(
        box,
        box,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    canvas = QPixmap(box, box)
    canvas.fill(Qt.GlobalColor.transparent)
    placing = QPainter(canvas)
    placing.drawPixmap(
        (box - scaled.width()) // 2, (box - scaled.height()) // 2, scaled
    )
    placing.end()
    canvas.setDevicePixelRatio(ratio)
    return canvas


def parse_hex_color(text: str) -> int | None:
    """``#AARRGGBB`` / ``#RRGGBB`` (``#`` optional) to an ARGB int, else None.

    A 6-digit value is taken as opaque — the common case when typing a color
    copied from anywhere else — so alpha only has to be spelled when it matters.
    """
    cleaned = text.strip().lstrip("#")
    if len(cleaned) not in (6, 8):
        return None
    try:
        value = int(cleaned, 16)
    except ValueError:
        return None
    return value | 0xFF000000 if len(cleaned) == 6 else value


class ColorSwatch(QWidget):
    """A flat color chip with a hairline border.

    The border is what makes a swatch read as a *sample* rather than as a hole
    in the dialog when its color is near the window background.
    """

    def __init__(self, size: QSize, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = 0xFF000000
        self.setFixedSize(size)

    def set_color(self, argb: int) -> None:
        if argb != self._color:
            self._color = argb
            self.update()

    def paintEvent(self, event) -> None:  # noqa: ARG002, ANN001 — Qt override
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QColor.fromRgba(self._color & 0xFFFFFFFF))
        painter.setPen(QColor(0, 0, 0, 90))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        painter.end()


class ColorEditor(QWidget):
    """Channel sliders + hex + previews for one ARGB color."""

    # The edited color, on every change (drag included) — hosts apply live and
    # merge the run into one undo step rather than waiting for a commit.
    # ``object``, not ``int``: Qt's int is 32-bit *signed*, and any ARGB with
    # alpha >= 0x80 overflows it.
    color_changed = Signal(object)
    # The eyedropper button toggled; the host arms/disarms its pickable surfaces.
    pick_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = 0xFF000000
        self._original = 0xFF000000
        self._quantize: Callable[[int], int] | None = None
        # Set while pushing state into the inputs, so their change signals don't
        # re-enter and fight the value being installed.
        self._updating = False

        self._alpha = False  # no alpha input until a format says it stores one
        self._preview = ColorSwatch(QSize(72, 48))
        self._preview_label = QLabel("Color")
        self._stored = ColorSwatch(QSize(72, 48))
        self._stored_label = QLabel("Stored as")
        self._stored_note = QLabel()
        self._stored_note.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        # The loss warning gets its own line, and keeps that line's space while
        # hidden: appended to the hex it would widen the column and shove the
        # centred swatch pair sideways every time the color became inexact.
        self._stored_approx = QLabel("(approximated)")
        approx_policy = self._stored_approx.sizePolicy()
        approx_policy.setRetainSizeWhenHidden(True)
        self._stored_approx.setSizePolicy(approx_policy)
        self._stored_approx.setVisible(False)

        self._labels: dict[str, QLabel] = {}
        self._sliders: dict[str, QSlider] = {}
        self._spins: dict[str, QSpinBox] = {}

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        for row, (name, _shift) in enumerate(_CHANNELS):
            label = QLabel(name)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 255)
            spin = QSpinBox()
            spin.setRange(0, 255)
            tip = f"{_CHANNEL_NAMES[name]} (0-255)"
            for widget in (label, slider, spin):
                widget.setToolTip(tip)
            label.setBuddy(slider)
            slider.valueChanged.connect(
                lambda value, n=name: self._on_channel(n, value)
            )
            spin.valueChanged.connect(lambda value, n=name: self._on_channel(n, value))
            grid.addWidget(label, row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(spin, row, 2)
            self._labels[name] = label
            self._sliders[name] = slider
            self._spins[name] = spin
        # Match the widgets to ``_alpha``'s initial False, or set_alpha_enabled
        # would see no change and leave a stale alpha row on screen.
        for widget in (self._labels["A"], self._sliders["A"], self._spins["A"]):
            widget.setVisible(False)

        self._hex = CommittingLineEdit(parse_hex_color, self._hex_text)
        self._hex.setFixedWidth(80)  # widened by set_alpha_enabled when needed
        self._hex.setToolTip("#AARRGGBB, or #RRGGBB for an opaque color")
        self._hex.committed.connect(self._on_hex)

        self._pick = QPushButton()
        self._pick.setCheckable(True)
        self._pick.setToolTip("Pick a color from the canvas or palette")
        self._pick.toggled.connect(self.pick_toggled)
        self._refresh_pick_icon()

        # Both previews sit in identically-shaped columns so their swatches line
        # up, and the pair is centred (stretch on both sides) — with the stored
        # preview hidden, the remaining swatch stays centred rather than
        # drifting left.
        top = Qt.AlignmentFlag.AlignHCenter
        current_column = QVBoxLayout()
        current_column.addWidget(self._preview_label, alignment=top)
        current_column.addWidget(self._preview)
        current_column.addStretch(1)
        stored_column = QVBoxLayout()
        stored_column.addWidget(self._stored_label, alignment=top)
        stored_column.addWidget(self._stored)
        stored_column.addWidget(self._stored_note, alignment=top)
        stored_column.addWidget(self._stored_approx, alignment=top)
        stored_column.addStretch(1)

        previews = QHBoxLayout()
        previews.addStretch(1)
        previews.addLayout(current_column)
        previews.addSpacing(16)
        previews.addLayout(stored_column)
        previews.addStretch(1)

        # The eyedropper sits right of the hex field — a compact icon toggle
        # beside the value it fills in, not a labelled button of its own.
        hex_row = QHBoxLayout()
        hex_row.addWidget(QLabel("Hex:"))
        hex_row.addWidget(self._hex)
        hex_row.addWidget(self._pick)
        hex_row.addStretch(1)

        column = QVBoxLayout(self)
        column.addLayout(previews)
        column.addLayout(grid)
        column.addLayout(hex_row)

        self._refresh_inputs()

    def _refresh_pick_icon(self) -> None:
        """(Re)paint the eyedropper mark in the current theme's button color."""
        color = self.palette().color(
            QPalette.ColorGroup.Active, QPalette.ColorRole.ButtonText
        )
        self._pick.setIcon(
            QIcon(_eyedropper_pixmap(color, 16, self.devicePixelRatioF()))
        )
        self._pick.setIconSize(QSize(16, 16))

    def changeEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        # The eyedropper glyph is a pixmap baked in the old palette's color; a
        # theme switch has to re-render it or it keeps yesterday's tint.
        super().changeEvent(event)
        if event.type() is QEvent.Type.PaletteChange:
            self._refresh_pick_icon()

    # -- state -------------------------------------------------------------
    def color(self) -> int:
        return self._color

    def set_color(self, argb: int, *, mark_original: bool = False) -> None:
        """Install ``argb`` without emitting :attr:`color_changed`.

        The host calls this for programmatic moves — opening on a new entry,
        an eyedropper sample, an undo landing underneath the open editor — so
        an echo can never loop back as a fresh edit. ``mark_original`` also
        re-arms Revert, which the host does when the editor retargets.
        """
        self._color = argb & 0xFFFFFFFF
        if mark_original:
            self._original = self._color
        self._refresh_inputs()

    def set_alpha_enabled(self, enabled: bool) -> None:
        """Show the alpha input only when the target format actually stores one.

        Most retro palette formats have no alpha field, and offering the channel
        anyway invites edits that silently vanish on the next encode. With it
        off the color is pinned opaque and the hex field narrows to
        ``#RRGGBB``, so what the editor shows is what the format can hold.
        """
        if self._alpha == enabled:
            return
        self._alpha = enabled
        for widget in (self._labels["A"], self._sliders["A"], self._spins["A"]):
            widget.setVisible(enabled)
        self._hex.setFixedWidth(96 if enabled else 80)
        if not enabled and self._color >> 24 != 0xFF:
            # Whatever alpha was showing can't survive; make that visible now
            # rather than at write time.
            self._apply(self._color | 0xFF000000)
        else:
            self._refresh_inputs()

    def set_quantizer(self, quantize: Callable[[int], int] | None) -> None:
        """Set (or clear) the round-trip used for the "Stored as" preview.

        ``None`` hides the preview entirely — the right state wherever the
        color is stored verbatim (a Custom palette) or is never written at all
        (the generated default), since there is no loss to warn about.
        """
        self._quantize = quantize
        self._refresh_stored()

    def set_pick_active(self, active: bool) -> None:
        """Reflect the host's pick mode on the button (e.g. after a pick ends)."""
        if self._pick.isChecked() != active:
            with signals_blocked(self._pick):
                self._pick.setChecked(active)

    # -- input handling ----------------------------------------------------
    def _on_channel(self, name: str, value: int) -> None:
        if self._updating:
            return
        shift = dict(_CHANNELS)[name]
        self._apply(self._color & ~(0xFF << shift) | (value & 0xFF) << shift)

    def _on_hex(self, argb: object) -> None:
        if not self._updating:
            self._apply(int(argb))

    def revert(self) -> None:
        """Return to the color the editor opened on (the marked original).

        A real edit — it emits :attr:`color_changed`, so the host records the
        undo back to the baseline — and a no-op when nothing has moved. The
        dialog's Cancel drives this before it closes.
        """
        if self._original != self._color:
            self._apply(self._original)

    def _apply(self, argb: int) -> None:
        """Land a user-originated change: sync the inputs, then announce it."""
        argb &= 0xFFFFFFFF
        if not self._alpha:
            argb |= 0xFF000000  # no alpha input means no way to be transparent
        self._color = argb
        self._refresh_inputs()
        self.color_changed.emit(self._color)

    def _hex_text(self) -> str:
        if self._alpha:
            return f"#{self._color & 0xFFFFFFFF:08X}"
        return f"#{self._color & 0xFFFFFF:06X}"

    def _refresh_inputs(self) -> None:
        self._updating = True
        for name, shift in _CHANNELS:
            value = (self._color >> shift) & 0xFF
            self._sliders[name].setValue(value)
            self._spins[name].setValue(value)
        self._hex.refresh()
        self._updating = False
        self._preview.set_color(self._color)
        self._refresh_stored()

    def _refresh_stored(self) -> None:
        if self._quantize is None:
            self._show_stored(False)
            return
        try:
            stored = self._quantize(self._color)
        except Exception:  # noqa: BLE001 — a codec that can't encode just hides
            self._show_stored(False)
            return
        self._show_stored(True)
        self._stored.set_color(stored)
        exact = stored == self._color
        self._stored_note.setText(f"#{stored & 0xFFFFFFFF:08X}")
        self._stored_approx.setVisible(not exact)

    def _show_stored(self, visible: bool) -> None:
        self._stored_label.setVisible(visible)
        self._stored.setVisible(visible)
        self._stored_note.setVisible(visible)
        # The warning only reserves its line while the stored preview is up;
        # with the whole column gone it must give the space back too, or the
        # lone remaining swatch stops being centred.
        policy = self._stored_approx.sizePolicy()
        policy.setRetainSizeWhenHidden(visible)
        self._stored_approx.setSizePolicy(policy)
        if not visible:
            self._stored_approx.setVisible(False)


class ColorEditorDialog(QDialog):
    """A non-modal window hosting one :class:`ColorEditor`.

    Non-modal on purpose: the eyedropper picks from the canvas and the palette
    grid, which a modal dialog would lock out. It stays on top of the main
    window as a tool window, and edits apply live (undo is the app's commit
    model — ``docs/design/undo-redo.md``). OK/Cancel only decide the window's
    parting move: OK closes on whatever the color has become, Cancel reverts to
    the color the editor opened on first. Esc maps to Cancel; the window's close
    button keeps the color (like OK), since the edits are already live.

    Closing runs through Qt's own :meth:`accept`/:meth:`reject`/:meth:`done`,
    and :attr:`closed` is emitted from ``done`` — the one choke point every
    route funnels through — so the host always drops its reference and disarms
    the eyedropper. (Rerouting close through :meth:`QWidget.close` instead would
    recurse: :meth:`QDialog.closeEvent` itself calls ``reject``.)
    """

    # Emitted when the window closes, so the host can drop its reference and
    # disarm any pick mode still running.
    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit color")
        # Tool: floats above its parent without taking a taskbar slot.
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.editor = ColorEditor(self)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.setCenterButtons(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.editor)
        layout.addWidget(buttons)

    def set_entry(self, label: str) -> None:
        """Name the palette entry being edited in the title bar."""
        self.setWindowTitle(f"Edit color - {label}")

    def reject(self) -> None:  # Qt override
        # Cancel (and Esc): undo back to the opening color, then close. Routed
        # through revert so it lands as an ordinary edit on the undo stack,
        # matching the live edits. super().reject() hides via done().
        self.editor.revert()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        # The window's close button keeps the live-applied color rather than
        # reverting — only the explicit Cancel discards. QDialog's own
        # closeEvent would call reject() (and revert); accept() instead keeps it
        # and still funnels through done() for the single `closed` emit.
        self.accept()
        event.accept()

    def done(self, result: int) -> None:  # Qt override
        # The single exit every route funnels through — OK's accept(), Cancel's
        # reject(), Esc, the close button. Emit `closed` here so the host always
        # cleans up, whichever one fired.
        super().done(result)
        self.closed.emit()
