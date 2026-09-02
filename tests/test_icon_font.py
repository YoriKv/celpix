"""The bundled icon font, and the style both themes are drawn in.

Both of these fail *silently* if they break — a glyph the shipped face doesn't
map renders as nothing at all, and a theme that quietly falls back to the
platform style still looks like a theme on the machine it was tested on.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from celpix.ui.glyphs import Glyph
from celpix.ui.icon_font import glyph_mask, glyph_pixmap, icon_font_family
from celpix.ui.theme import Theme, apply_theme, palette_for

# The two boxes the app actually asks for: the file list's row marker and the
# tool rail's button.
_BOXES = (QSize(13, 16), QSize(20, 20))


def _ink(pixmap) -> int:  # noqa: ANN001 - QPixmap
    """How many pixels the glyph actually put down."""
    image = pixmap.toImage()
    return sum(
        1
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).alpha() > 0
    )


def test_every_glyph_is_a_single_codepoint(qapp) -> None:
    # The enum spells its codepoints as escapes, and an escape that doesn't take
    # (a doubled backslash, say) is a *string* the icon font will happily draw as
    # text - which looks like ink to any test that only counts pixels, and like a
    # smudge to the user. One character is the whole invariant.
    for glyph in Glyph:
        assert len(glyph.value) == 1, f"{glyph.name} is {glyph.value!r}, not one char"


def test_the_icon_font_loads_and_every_glyph_draws(qapp) -> None:
    # One test over the whole enum rather than one per icon: what can go wrong is
    # shared — the font missing from the package, or a codepoint that isn't in
    # the Solid face — and either way the symptom is a blank icon rather than a
    # crash, which nothing else in the suite would notice.
    assert icon_font_family() is not None
    for glyph in Glyph:
        for box in _BOXES:
            mask = glyph_mask(glyph, box)
            assert mask.size() == box
            assert _ink(mask) > 0, f"{glyph.name} drew nothing at {box.width()}px"


def test_every_glyph_is_centred_in_its_box(qapp) -> None:
    # The bug this exists for: glyphs were positioned from
    # QFontMetricsF.tightBoundingRect, which a *variable* face at a non-default
    # weight does not honour - it over-reported some glyphs' height by a third,
    # and the transform bar's flip arrows sat visibly high. Nothing else here
    # noticed, because the icon was the right size and full of ink; it was just
    # in the wrong place. So: measure the actual pixels, both axes, every box.
    for box in (*_BOXES, QSize(26, 32)):
        for glyph in Glyph:
            image = glyph_mask(glyph, box).toImage()
            rows = [
                y
                for y in range(image.height())
                for x in range(image.width())
                if image.pixelColor(x, y).alpha() > 0
            ]
            cols = [
                x
                for y in range(image.height())
                for x in range(image.width())
                if image.pixelColor(x, y).alpha() > 0
            ]
            gaps = {
                "top/bottom": (min(rows), image.height() - 1 - max(rows)),
                "left/right": (min(cols), image.width() - 1 - max(cols)),
            }
            # One pixel apart is the most an odd split of an even gap can be.
            for axis, (before, after) in gaps.items():
                assert abs(before - after) <= 1, (
                    f"{glyph.name} at {box.width()}x{box.height()} sits "
                    f"{before}/{after} on {axis}"
                )


def test_a_glyph_is_stamped_in_the_asked_for_color_at_device_scale(qapp) -> None:
    # The icon is baked, so the color and the resolution are decided here rather
    # than by the widget drawing it: a 2x display gets twice the pixels, and the
    # pixmap still measures the logical box.
    box = QSize(20, 20)
    pixmap = glyph_pixmap(Glyph.PENCIL, QColor(0xFF, 0x00, 0x00), box, 2.0)
    assert pixmap.size() == QSize(40, 40)
    assert pixmap.devicePixelRatio() == 2.0
    image = pixmap.toImage()
    inked = [
        image.pixelColor(x, y)
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).alpha() == 255
    ]
    assert inked, "the stamp left no fully opaque ink to check"
    assert all(c.red() == 255 and c.green() == 0 and c.blue() == 0 for c in inked)


@pytest.mark.parametrize("theme", list(Theme))
def test_both_themes_are_drawn_in_fusion(qapp, theme: Theme) -> None:
    # Fusion for light as well as dark, so a palette is the whole of what a theme
    # is: the native styles ignore an application palette in half their controls,
    # which is how a "dark" theme comes out half-light on Windows.
    try:
        apply_theme(theme)
        app = QApplication.instance()
        assert app.style().baseStyle().objectName().lower() == "fusion"
        window = app.palette().color(QPalette.ColorRole.Window).lightness()
        assert (window < 128) is (theme is Theme.DARK)
    finally:
        # The QApplication outlives the test; leave it as the rest of the suite
        # expects to find it.
        apply_theme(Theme.LIGHT)


def test_a_theme_that_names_no_surface_wears_the_styles_own_palette(qapp) -> None:
    # Light is Fusion's standardPalette, not a table of colors - the palette has
    # to come from the style about to be installed, so palette_for is asked for
    # one rather than deriving it.
    style = QApplication.instance().style()
    assert palette_for(Theme.LIGHT, style) == style.standardPalette()
    assert palette_for(Theme.DARK, style) != style.standardPalette()
