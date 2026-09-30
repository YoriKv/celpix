"""A toolbar's » button that shows the controls which did not fit."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QStyle,
    QToolBar,
    QToolButton,
    QWidget,
)

from celpix.ui.icon_font import icon_cache_key, icon_qicon
from celpix.ui.icons import Icon

__all__ = ["ToolBarOverflow"]


class ToolBarOverflow(QObject):
    """Make a toolbar's » button show the controls that didn't fit.

    Qt draws the » itself once a toolbar runs out of width, but outside a
    QMainWindow's toolbar area it offers the hidden part as a *menu*, and a menu
    cannot hold a combo or a spin — so for bars made of nothing else Qt greys
    the button out, and the controls past the edge are simply unreachable.

    This keeps the button live and, on a press, hands the overflowing tail to a
    popup toolbar for as long as the popup is up. The *actions* move, not the
    widgets: a ``QWidgetAction`` releases its widget when it leaves one toolbar
    and hands it to the next, so the controls keep their signals, their action's
    visibility and their place in the order. The popup closing puts them back.

    Worth it even on a bar of plain buttons, which Qt's menu could hold: Qt's »
    is a fixed dark pixmap, and every bar over the canvas should overflow the
    same way.
    """

    def __init__(self, bar: QToolBar) -> None:
        super().__init__(bar)
        self._bar = bar
        self._button: QToolButton = bar.findChild(QToolButton, "qt_toolbar_ext_button")
        self._button.setToolTip("Controls that do not fit the toolbar")
        self._button.installEventFilter(self)
        bar.installEventFilter(self)
        self._button.setEnabled(True)
        self._icon_key: tuple[int, float] | None = None
        self._bake_icon()

    def _bake_icon(self) -> None:
        """Paint the » in the theme's button-text colour, at the button's size.

        Qt's own is a fixed dark pixmap that vanishes on the dark theme. Baked,
        so it is re-baked when the bar's palette or device scale moves (see
        :meth:`eventFilter`) — guarded on :func:`icon_cache_key` as every baked
        icon is, since the bar sees the same PaletteChange storm a panel does.
        Sized to the style's extension extent rather than the usual 16: the
        button is only that wide, and a 16px glyph drawn into it loses its thin
        strokes to the squeeze — a gray smudge rather than the theme's ink.
        """
        key = icon_cache_key(self._bar)
        if key == self._icon_key:
            return
        self._icon_key = key
        extent = self._bar.style().pixelMetric(
            QStyle.PixelMetric.PM_ToolBarExtensionExtent, None, self._bar
        )
        self._button.setIconSize(QSize(extent, extent))
        self._button.setIcon(
            icon_qicon(
                Icon.OVERFLOW,
                self._bar.palette(),
                size=extent,
                ratio=self._bar.devicePixelRatioF(),
            )
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if watched is self._bar and kind in (
            QEvent.Type.PaletteChange,
            QEvent.Type.DevicePixelRatioChange,
        ):
            self._bake_icon()
        elif kind == QEvent.Type.EnabledChange:
            # The toolbar's layout disables the button on every pass; undo it —
            # also when the bar itself comes back from being frozen, since the
            # layout's own disable left the button stuck off behind it.
            if self._bar.isEnabled() and not self._button.isEnabled():
                self._button.setEnabled(True)
        elif watched is not self._button:
            pass
        elif kind == QEvent.Type.MouseButtonPress:
            self._open()
            return True
        elif kind in (QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick):
            return True  # the button's own click would open Qt's empty menu
        return False

    def _open(self) -> None:
        actions = self._bar.actions()
        overflow = [
            index
            for index, action in enumerate(actions)
            if action.isVisible()
            and (widget := self._bar.widgetForAction(action)) is not None
            and not widget.isVisibleTo(self._bar)
        ]
        if not overflow:
            return
        # The layout lays out front to back and hides the rest, so what didn't fit
        # is always a tail — hidden actions inside it included, so their place in
        # the order survives the round trip.
        moved = actions[overflow[0] :]

        popup = _OverflowPopup(self._button)
        spare = QToolBar(popup)
        spare.layout().setSpacing(self._bar.layout().spacing())
        spare.setIconSize(self._bar.iconSize())
        spare.setToolButtonStyle(self._bar.toolButtonStyle())
        popup.layout().addWidget(spare)
        for action in moved:
            self._bar.removeAction(action)
            spare.addAction(action)

        def put_back() -> None:
            for action in moved:
                spare.removeAction(action)
                self._bar.addAction(action)
            popup.deleteLater()

        popup.closed.connect(put_back)
        popup.adjustSize()
        # Hang it under the » with right edges aligned, the way a menu drops from
        # a button at the end of a bar, kept on screen.
        corner = self._button.mapToGlobal(self._button.rect().bottomRight())
        pos = corner - popup.rect().topRight()
        screen = self._button.screen().availableGeometry()
        pos.setX(max(screen.left(), min(pos.x(), screen.right() - popup.width())))
        popup.move(pos)
        popup.show()


class _OverflowPopup(QFrame):
    """The frame :class:`ToolBarOverflow` shows, announcing when it goes."""

    closed = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        # A click on the » that dismisses the popup must not reopen it.
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 2, 2, 2)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.closed.emit()
