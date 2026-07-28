"""The app-wide light/dark appearance, in one palette applied in one place.

celPix themes through **QPalette**, not a stylesheet: every widget in the app
already draws from palette roles (the panels' glyphs are tinted from Text and
Highlight, the file-position rail derives its accent from Highlight, the hex
view shades its cursor from it), so handing the application a different palette
re-colors the whole UI without a per-widget rule anywhere. The only literals
left are the ones that are deliberately *not* theme colors — the canvas's
neutral gray backing, the grid's two levels, the warning ambers — all of which
have to read the same against the art whichever theme is on.

**Light is the platform's own look**, unchanged: its palette is the active
style's ``standardPalette()``, which is where Qt was already getting it.

**Dark is derived, not tabulated.** ``QPalette(QColor)`` computes a whole
palette from one surface color — window, button, text, and the Light/Mid/Dark/
Shadow bevel shades all fall out of it — so the constants below are only the
handful whose derived value is wrong for a dark UI (Qt's derivation assumes the
seed is a *button* color on a light desktop). That keeps one seed as the thing
to turn when the dark theme wants to be lighter or warmer.

Dark also forces the **Fusion** style. The native Windows and macOS styles paint
many controls from platform colors and ignore the application palette, so a dark
palette under them comes out half-light; Fusion honours the palette everywhere
and ships on every platform Qt does. ``setColorScheme`` is requested alongside
for the parts a palette cannot reach — most visibly the Windows title bar — and
is a no-op on platforms that don't support the request (X11/Wayland without a
portal), which is exactly why the palette above is built by hand rather than
left to Qt's own dark scheme.
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QStyleFactory

# App-wide, remembered across launches and shared by every project - the theme is
# a property of the person using celPix, not of what they have open.
THEME_KEY = "view/theme"


class Theme(Enum):
    """The app's appearance. ``value`` is the stable string persisted in app
    settings (:data:`THEME_KEY`)."""

    LIGHT = "light"
    DARK = "dark"


# The one color the dark palette is derived from: the surface behind everything
# that isn't an input. Dark enough that the art on the canvas is the brightest
# thing in the window, light enough that the bevel shades Qt derives from it
# (Light/Midlight/Mid/Dark/Shadow) still separate a raised control from its
# background.
_DARK_SURFACE = QColor(0x35, 0x35, 0x35)

# What derivation gets wrong for a dark UI, and nothing more.
#
# - Base/AlternateBase: Qt derives pure black, which reads as a hole punched in
#   the window. A shade *under* the surface is the same figure/ground relation
#   the light theme has, without the contrast jump.
# - Highlight: Qt's derived navy is nearly the surface color, so a selected row
#   would barely show. A saturated blue is legible against both the surface and
#   the dark Base, and keeps the accent the file-position rail and the panel
#   glyphs pick up.
# - ToolTip*: Qt keeps the light desktop's pale yellow, the one surface that
#   would still flash white.
# - PlaceholderText: derived as full-strength Text, which makes a hint look like
#   a value; the light theme's is faded, so fade it here too.
# - Link: the default blue is too dark to read on the surface.
_DARK_BASE = QColor(0x2B, 0x2B, 0x2B)
_DARK_ALTERNATE = QColor(0x30, 0x30, 0x30)
_DARK_HIGHLIGHT = QColor(0x2A, 0x6E, 0xB8)
_DARK_TOOLTIP_BASE = QColor(0x3C, 0x3C, 0x3C)
_DARK_TOOLTIP_TEXT = QColor(0xE6, 0xE6, 0xE6)
_DARK_PLACEHOLDER = QColor(0xFF, 0xFF, 0xFF, 0x66)
_DARK_LINK = QColor(0x54, 0xA6, 0xFF)
# Disabled ink. Qt derives this by *darkening* the text color, which on a dark
# surface lands a shade off black - a disabled label would be less readable than
# no label at all. Grayed toward the surface instead, the direction that reads as
# "off" on a dark UI.
_DARK_DISABLED_TEXT = QColor(0x7A, 0x7A, 0x7A)
_DARK_DISABLED_HIGHLIGHT = QColor(0x45, 0x45, 0x45)


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


def dark_palette() -> QPalette:
    """The dark theme's palette: derived from :data:`_DARK_SURFACE`, then the
    handful of roles whose derived value is wrong for a dark UI (see above)."""
    palette = QPalette(_DARK_SURFACE)
    palette.setColor(QPalette.ColorRole.Base, _DARK_BASE)
    palette.setColor(QPalette.ColorRole.AlternateBase, _DARK_ALTERNATE)
    palette.setColor(QPalette.ColorRole.Highlight, _DARK_HIGHLIGHT)
    palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.ToolTipBase, _DARK_TOOLTIP_BASE)
    palette.setColor(QPalette.ColorRole.ToolTipText, _DARK_TOOLTIP_TEXT)
    palette.setColor(QPalette.ColorRole.PlaceholderText, _DARK_PLACEHOLDER)
    palette.setColor(QPalette.ColorRole.Link, _DARK_LINK)
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.HighlightedText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, _DARK_DISABLED_TEXT)
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Highlight,
        _DARK_DISABLED_HIGHLIGHT,
    )
    return palette


def apply_theme(theme: Theme) -> None:
    """Put ``theme`` on the running application, live.

    A switch changes two things — the style and the palette — and **the palette
    has to be installed first**. Qt propagates an application palette through the
    event loop rather than inside ``setPalette``, and installing a style in
    between re-polishes every widget against the palette it already has: the
    queued PaletteChange is then considered satisfied and never delivered. The
    panels that bake a palette color into a pixmap (every tinted glyph in the
    app) listen for exactly that event, so in the other order they would keep
    yesterday's colors until something else invalidated them.

    Which is also why the style is *built* before it is installed: a light theme's
    palette is the style's own ``standardPalette()`` — where Qt was already
    getting it — so it has to be asked of the new style, not the outgoing one.
    """
    app = QApplication.instance()
    if app is None:  # nothing to theme yet (import-time, or a headless caller)
        return
    dark = theme is Theme.DARK
    # For the parts of the UI a palette cannot reach — most visibly the Windows
    # title bar. Requested before the palette is read back, since a platform that
    # honours it regenerates the style's standard palette to match.
    app.styleHints().setColorScheme(
        Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light
    )
    # A QProxyStyle with no base resolves to the platform's default style, which
    # is what the light theme wants; dark passes Fusion in as the base instead.
    style = _UnderlinedMnemonics(QStyleFactory.create("Fusion") if dark else None)
    app.setPalette(dark_palette() if dark else style.standardPalette())
    app.setStyle(style)
