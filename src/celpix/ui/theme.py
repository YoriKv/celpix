"""The app-wide light/dark appearance, in one palette applied in one place.

celPix themes through **QPalette**, not a stylesheet: every widget in the app
already draws from palette roles (the panels' icons are tinted from Text and
Highlight, the file-position rail derives its accent from Highlight, the hex
view shades its cursor from it), so handing the application a different palette
re-colors the whole UI without a per-widget rule anywhere. The only literals
left are the ones that are deliberately *not* theme colors — the canvas's
neutral gray backing, the grid's two levels, the warning ambers — all of which
have to read the same against the art whichever theme is on.

**Both themes run on Fusion.** The native Windows and macOS styles paint many
controls from platform colors and ignore the application palette, so a dark
palette under them comes out half-light; Fusion honours the palette everywhere
and ships on every platform Qt does. Using it for light as well is what makes a
screenshot from one machine describe the app on the others — and what lets a
theme be a *palette* here rather than a palette plus a note about which
platform it looks right on. ``setColorScheme`` is requested alongside for the
parts a palette cannot reach — most visibly the Windows title bar — and is a
no-op on platforms that don't support the request (X11/Wayland without a
portal), which is exactly why the palettes below are built here rather than
left to Qt's own dark scheme.

**A theme is a data row** (:class:`_PaletteSpec`, tabulated in
:data:`_PALETTES`): a surface color to derive the whole palette from, plus the
handful of roles whose derived value is wrong. ``QPalette(QColor)`` computes
window, button, text and the Light/Mid/Dark/Shadow bevel shades from that one
color, so a theme is a seed to turn rather than forty constants to keep
consistent — and adding one is a row here, not a code path. Light is the
exception in the other direction: it names no surface, because Fusion's own
``standardPalette()`` is already a tuned light palette and there is nothing to
improve on by re-deriving it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QStyleFactory

if TYPE_CHECKING:
    from collections.abc import Mapping

# App-wide, remembered across launches and shared by every project - the theme is
# a property of the person using celPix, not of what they have open.
THEME_KEY = "view/theme"


class Theme(Enum):
    """The app's appearance. ``value`` is the stable string persisted in app
    settings (:data:`THEME_KEY`)."""

    LIGHT = "light"
    DARK = "dark"


@dataclass(frozen=True)
class _PaletteSpec:
    """One theme's palette as data.

    ``surface`` is the color the whole palette is derived from — the ground
    behind everything that isn't an input — or ``None`` to start from the
    style's own ``standardPalette()``. ``roles`` and ``disabled`` then override
    what derivation gets wrong, the latter in the Disabled color group.
    """

    surface: QColor | None = None
    roles: Mapping[QPalette.ColorRole, QColor] = field(default_factory=dict)
    disabled: Mapping[QPalette.ColorRole, QColor] = field(default_factory=dict)


# The dark theme's seed: dark enough that the art on the canvas is the brightest
# thing in the window, light enough that the bevel shades Qt derives from it
# (Light/Midlight/Mid/Dark/Shadow) still separate a raised control from its
# background.
_DARK_SURFACE = QColor(0x35, 0x35, 0x35)

# What derivation gets wrong for a dark UI, and nothing more. Qt's derivation
# assumes the seed is a *button* color on a light desktop, so:
#
# - Base/AlternateBase: Qt derives pure black, which reads as a hole punched in
#   the window. A shade *under* the surface is the same figure/ground relation
#   the light theme has, without the contrast jump.
# - Highlight: Qt's derived navy is nearly the surface color, so a selected row
#   would barely show. A saturated blue is legible against both the surface and
#   the dark Base, and keeps the accent the file-position rail and the panel
#   icons pick up.
# - ToolTip*: Qt keeps the light desktop's pale yellow, the one surface that
#   would still flash white.
# - PlaceholderText: derived as full-strength Text, which makes a hint look like
#   a value; the light theme's is faded, so fade it here too.
# - Link: the default blue is too dark to read on the surface.
_DARK_ROLES = {
    QPalette.ColorRole.Base: QColor(0x2B, 0x2B, 0x2B),
    QPalette.ColorRole.AlternateBase: QColor(0x30, 0x30, 0x30),
    QPalette.ColorRole.Highlight: QColor(0x2A, 0x6E, 0xB8),
    QPalette.ColorRole.HighlightedText: QColor(Qt.GlobalColor.white),
    QPalette.ColorRole.ToolTipBase: QColor(0x3C, 0x3C, 0x3C),
    QPalette.ColorRole.ToolTipText: QColor(0xE6, 0xE6, 0xE6),
    QPalette.ColorRole.PlaceholderText: QColor(0xFF, 0xFF, 0xFF, 0x66),
    QPalette.ColorRole.Link: QColor(0x54, 0xA6, 0xFF),
}

# Disabled ink. Qt derives this by *darkening* the text color, which on a dark
# surface lands a shade off black - a disabled label would be less readable than
# no label at all. Grayed toward the surface instead, the direction that reads as
# "off" on a dark UI.
_DARK_DISABLED_TEXT = QColor(0x7A, 0x7A, 0x7A)
_DARK_DISABLED = {
    QPalette.ColorRole.WindowText: _DARK_DISABLED_TEXT,
    QPalette.ColorRole.Text: _DARK_DISABLED_TEXT,
    QPalette.ColorRole.ButtonText: _DARK_DISABLED_TEXT,
    QPalette.ColorRole.HighlightedText: _DARK_DISABLED_TEXT,
    QPalette.ColorRole.Highlight: QColor(0x45, 0x45, 0x45),
}

_PALETTES: dict[Theme, _PaletteSpec] = {
    Theme.LIGHT: _PaletteSpec(),
    Theme.DARK: _PaletteSpec(
        surface=_DARK_SURFACE, roles=_DARK_ROLES, disabled=_DARK_DISABLED
    ),
}


class _UnderlinedMnemonics(QProxyStyle):
    """Show every menu's mnemonic underline without holding Alt down.

    Windows (and any desktop whose theme asks for it) hides the underlines until
    Alt is pressed - the platform's "underline access keys" setting - so a menu
    looks as though it has no keyboard route at all until you already know to
    reach for one. celPix's menus are built around those letters, so the hint is
    forced on and they are drawn from the moment a menu opens. Everything else is
    delegated: this changes no other part of the style it wraps.
    """

    def styleHint(  # noqa: N802 - Qt override
        self,
        hint: QStyle.StyleHint,
        option=None,  # noqa: ANN001 - QStyleOption
        widget=None,  # noqa: ANN001 - QWidget
        returnData=None,  # noqa: ANN001, N803 - QStyleHintReturn
    ) -> int:
        if hint == QStyle.StyleHint.SH_UnderlineShortcut:
            return 1
        return super().styleHint(hint, option, widget, returnData)


def palette_for(theme: Theme, style: QStyle) -> QPalette:
    """``theme``'s palette, as ``style`` would wear it.

    ``style`` is asked rather than assumed because a theme that names no surface
    *is* the style's standard palette, and that has to come from the style about
    to be installed rather than from the outgoing one.
    """
    spec = _PALETTES[theme]
    palette = (
        style.standardPalette() if spec.surface is None else QPalette(spec.surface)
    )
    for role, color in spec.roles.items():
        palette.setColor(role, color)
    for role, color in spec.disabled.items():
        palette.setColor(QPalette.ColorGroup.Disabled, role, color)
    return palette


def apply_theme(theme: Theme) -> None:
    """Put ``theme`` on the running application, live.

    A switch changes two things — the style and the palette — and **the palette
    has to be installed first**. Qt propagates an application palette through the
    event loop rather than inside ``setPalette``, and installing a style in
    between re-polishes every widget against the palette it already has: the
    queued PaletteChange is then considered satisfied and never delivered. The
    panels that bake a palette color into a pixmap (every tinted icon in the
    app) listen for exactly that event, so in the other order they would keep
    yesterday's colors until something else invalidated them.
    """
    app = QApplication.instance()
    if app is None:  # nothing to theme yet (import-time, or a headless caller)
        return
    # For the parts of the UI a palette cannot reach — most visibly the Windows
    # title bar. Requested before the palette is read back, since a platform that
    # honours it regenerates the style's standard palette to match.
    app.styleHints().setColorScheme(
        Qt.ColorScheme.Dark if theme is Theme.DARK else Qt.ColorScheme.Light
    )
    style = _UnderlinedMnemonics(QStyleFactory.create("Fusion"))
    app.setPalette(palette_for(theme, style))
    app.setStyle(style)
