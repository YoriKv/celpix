"""The palette operations' arithmetic: what each one does to a known colour.

Pinned values rather than re-derived ones, so a change to the channel math is
a visible change here. Alpha is the invariant every operation shares.
"""

from __future__ import annotations

import itertools

import pytest

from celpix.core.color_ops import BlendMode, OpKind, PaletteOp


def test_invert_flips_the_channels_and_keeps_alpha() -> None:
    assert PaletteOp(OpKind.INVERT).apply(0x80102030) == 0x80EFDFCF


def test_grayscale_lands_on_luma_and_the_amount_mixes() -> None:
    # Pure red is 29.9% luma; at half amount it is halfway there.
    assert PaletteOp(OpKind.GRAYSCALE).apply(0xFFFF0000) == 0xFF4C4C4C
    assert PaletteOp(OpKind.GRAYSCALE, amount=0.5).apply(0xFFFF0000) == 0xFFA62626


def test_colorize_takes_the_hue_on_the_entrys_own_brightness() -> None:
    # Mid grey tinted red: a red whose luma is the grey's, clamped where pure
    # red cannot be lifted that far without leaving gamut.
    assert PaletteOp(OpKind.COLORIZE, color=0xFFFF0000).apply(0xFF808080) == 0xFFFF3434


def test_hsv_shift_turns_the_hue_and_adds_to_saturation_and_value() -> None:
    assert PaletteOp(OpKind.HSV_SHIFT, hue=120).apply(0xFFFF0000) == 0xFF00FF00
    assert PaletteOp(OpKind.HSV_SHIFT, hue=-120).apply(0xFFFF0000) == 0xFF0000FF
    # Fully desaturated keeps the value: a light red becomes white, not grey.
    assert PaletteOp(OpKind.HSV_SHIFT, saturation=-1).apply(0xFFFF4040) == 0xFFFFFFFF
    assert PaletteOp(OpKind.HSV_SHIFT, saturation=0.5).apply(0xFF80C080) == 0xFF20C020
    # Value halves every channel alike, alpha untouched; a lift clamps.
    assert PaletteOp(OpKind.HSV_SHIFT, value=-0.5).apply(0x80FF8000) == 0x80804000
    assert PaletteOp(OpKind.HSV_SHIFT, value=1).apply(0xFF402010) == 0xFFFF7F40
    assert PaletteOp(OpKind.HSV_SHIFT).apply(0xFF123456) == 0xFF123456


def test_set_takes_the_color_but_not_its_alpha() -> None:
    assert PaletteOp(OpKind.SET, color=0x00123456).apply(0x80FFFFFF) == 0x80123456


@pytest.mark.parametrize(
    ("mode", "base", "layer", "expected"),
    [
        (BlendMode.MULTIPLY, 0xFF808080, 0xFF808080, 0xFF404040),
        (BlendMode.SCREEN, 0xFF808080, 0xFF808080, 0xFFC0C0C0),
        (BlendMode.DIFFERENCE, 0xFFFF0000, 0xFF00FF00, 0xFFFFFF00),
        # Blue takes red's hue at its own (dark) luma.
        (BlendMode.HUE, 0xFF0000FF, 0xFFFF0000, 0xFFD00000),
        # Blue lifted to red's luma, still blue.
        (BlendMode.LUMINOSITY, 0xFF0000FF, 0xFFFF0000, 0xFF2F2FFF),
        (BlendMode.DARKER_COLOR, 0xFF808080, 0xFF404040, 0xFF404040),
    ],
)
def test_blend_modes_on_known_colors(
    mode: BlendMode, base: int, layer: int, expected: int
) -> None:
    assert PaletteOp(OpKind.BLEND, color=layer, mode=mode).apply(base) == expected


def test_zero_amount_leaves_the_entry_alone() -> None:
    op = PaletteOp(OpKind.BLEND, color=0xFF808080, mode=BlendMode.MULTIPLY, amount=0)
    assert op.apply(0xFF123456) == 0xFF123456


def test_every_blend_mode_stays_in_range_at_the_extremes() -> None:
    # The dodge/burn/divide family divides by a channel; none may blow past
    # 8-bit or land on a NaN, whichever side the zero is on.
    corners = (0xFF000000, 0xFFFFFFFF, 0xFF7F3010)
    for mode in BlendMode:
        for base, layer in itertools.product(corners, repeat=2):
            result = PaletteOp(OpKind.BLEND, color=layer, mode=mode).apply(base)
            assert result >> 24 == 0xFF, (mode, base, layer)


def test_describe_names_the_operation_and_its_inputs() -> None:
    assert (
        PaletteOp(
            OpKind.BLEND, color=0xFF808080, mode=BlendMode.MULTIPLY, amount=0.5
        ).describe()
        == "Multiply #808080 at 50%"
    )
    assert PaletteOp(OpKind.COLORIZE, color=0xFF0000FF).describe() == (
        "Colorize #0000FF at 100%"
    )
    assert PaletteOp(OpKind.SET, color=0xFF000000).describe() == "Set to #000000"
    assert PaletteOp(OpKind.GRAYSCALE, amount=0.25).describe() == "Grayscale at 25%"
    assert (
        PaletteOp(OpKind.HSV_SHIFT, hue=30, saturation=-0.1, value=0.05).describe()
        == "HSV shift H+30° S-10% V+5%"
    )
