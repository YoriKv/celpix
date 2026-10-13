"""Colour operations over a run of palette entries: pure maps over ARGB.

The palette dock's **Edit Palette** window applies one of these to the
selected colour, the active palette row or the whole palette, previewing live
and committing as one undo step (``docs/design/palette-editing.md`` §1). Each
operation is a function of a single colour, so a run is just the function
mapped over it — which is what keeps the preview, the commit and the tests on
one piece of arithmetic.

Every operation **keeps the entry's alpha**. Most retro palette formats store
none, and the one input that could carry one — the colour an operation blends
with — is taken as opaque; so alpha is never a channel these touch, and a
palette whose format does store it comes out with every entry's alpha exactly
as it went in.

The channel math runs in floats on ``0..1`` and lands back on 8-bit by
rounding, so a 50% amount of a 50% amount is not bit-identical to 100% of
nothing — fine for colours, which are what this is for.

The blend modes are the familiar layered-paint set, with the **palette entry
as the base** and the chosen colour as the layer blended over it
(``docs/design-reference/mushroms-editor-design.md`` §7 names where the set
came from). Hue, Saturation and Luminosity use the HCY form of colour — hue,
chroma and a Rec. 601 luma — which is what gives "keep this colour's
brightness, take that one's hue" an answer that stays in gamut most of the
time; where it does not, the channel is clamped.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from celpix.core.palette import format_argb

# Rec. 601 luma weights: the brightness a colour contributes, as broadcast
# television weighted it and as every grayscale conversion since has by habit.
_LUMA = (0.299, 0.587, 0.114)

Rgb = tuple[float, float, float]
ChannelRule = Callable[[float, float], float]


class OpKind(Enum):
    """Which operation; ``value`` is the label the dialog shows."""

    INVERT = "Invert"
    GRAYSCALE = "Grayscale"
    COLORIZE = "Colorize"
    HSV_SHIFT = "HSV Shift"
    BLEND = "Blend"
    SET = "Set to Color"


class BlendMode(Enum):
    """How :attr:`OpKind.BLEND` combines a colour with each entry; ``value`` is
    the label the dialog shows. Order is the dialog's list order."""

    MULTIPLY = "Multiply"
    SCREEN = "Screen"
    OVERLAY = "Overlay"
    HARD_LIGHT = "Hard Light"
    SOFT_LIGHT = "Soft Light"
    COLOR_DODGE = "Color Dodge"
    LINEAR_DODGE = "Linear Dodge (Add)"
    COLOR_BURN = "Color Burn"
    LINEAR_BURN = "Linear Burn"
    VIVID_LIGHT = "Vivid Light"
    LINEAR_LIGHT = "Linear Light"
    DIFFERENCE = "Difference"
    DARKEN = "Darken"
    LIGHTEN = "Lighten"
    DARKER_COLOR = "Darker Color"
    LIGHTER_COLOR = "Lighter Color"
    HUE = "Hue"
    SATURATION = "Saturation"
    LUMINOSITY = "Luminosity"
    DIVIDE = "Divide"


# -- ARGB <-> float channels --------------------------------------------------
def _rgb(argb: int) -> Rgb:
    return (
        ((argb >> 16) & 0xFF) / 255,
        ((argb >> 8) & 0xFF) / 255,
        (argb & 0xFF) / 255,
    )


def _channel(value: float) -> int:
    return max(0, min(255, round(value * 255)))


def _argb(alpha: int, rgb: Rgb) -> int:
    r, g, b = rgb
    return (alpha & 0xFF) << 24 | _channel(r) << 16 | _channel(g) << 8 | _channel(b)


def _mix(base: Rgb, result: Rgb, amount: float) -> Rgb:
    """``result`` at ``amount`` over ``base`` — the opacity every operation has."""
    if amount >= 1:
        return result
    if amount <= 0:
        return base
    return tuple(b + (r - b) * amount for b, r in zip(base, result, strict=True))  # type: ignore[return-value]


# -- HCY -----------------------------------------------------------------------
def luma(rgb: Rgb) -> float:
    return sum(w * c for w, c in zip(_LUMA, rgb, strict=True))


def _hue_chroma(rgb: Rgb) -> tuple[float, float]:
    """Hue on ``0..1`` (0 where there is none) and chroma, the usual hexcone."""
    r, g, b = rgb
    high, low = max(rgb), min(rgb)
    chroma = high - low
    if chroma == 0:
        return 0.0, 0.0
    if high == r:
        hue = (g - b) / chroma
    elif high == g:
        hue = (b - r) / chroma + 2
    else:
        hue = (r - g) / chroma + 4
    return (hue % 6) / 6, chroma


def _from_hcy(hue: float, chroma: float, target_luma: float) -> Rgb:
    """The colour with this hue and chroma whose luma is ``target_luma``.

    Built as the pure hue at that chroma, lifted or lowered uniformly until its
    luma matches; a lift that pushes a channel out of ``0..1`` is clamped, which
    moves the luma a little for very bright or very dark saturated targets —
    there is no in-gamut colour that answers exactly, and the nearest one is
    the honest result.
    """
    chroma = max(0.0, min(1.0, chroma))
    sector = (hue % 1) * 6
    x = chroma * (1 - abs(sector % 2 - 1))
    if sector < 1:
        r, g, b = chroma, x, 0.0
    elif sector < 2:
        r, g, b = x, chroma, 0.0
    elif sector < 3:
        r, g, b = 0.0, chroma, x
    elif sector < 4:
        r, g, b = 0.0, x, chroma
    elif sector < 5:
        r, g, b = x, 0.0, chroma
    else:
        r, g, b = chroma, 0.0, x
    lift = target_luma - luma((r, g, b))
    return _clamp((r + lift, g + lift, b + lift))


def _clamp(rgb: Rgb) -> Rgb:
    return tuple(max(0.0, min(1.0, c)) for c in rgb)  # type: ignore[return-value]


# -- HSV -----------------------------------------------------------------------
def _to_hsv(rgb: Rgb) -> tuple[float, float, float]:
    """Hue on ``0..1`` (0 where there is none), saturation and value."""
    hue, chroma = _hue_chroma(rgb)
    value = max(rgb)
    return hue, (chroma / value if value else 0.0), value


def _from_hsv(hue: float, saturation: float, value: float) -> Rgb:
    chroma = value * saturation
    sector = (hue % 1) * 6
    x = chroma * (1 - abs(sector % 2 - 1))
    if sector < 1:
        r, g, b = chroma, x, 0.0
    elif sector < 2:
        r, g, b = x, chroma, 0.0
    elif sector < 3:
        r, g, b = 0.0, chroma, x
    elif sector < 4:
        r, g, b = 0.0, x, chroma
    elif sector < 5:
        r, g, b = x, 0.0, chroma
    else:
        r, g, b = chroma, 0.0, x
    lift = value - chroma
    return (r + lift, g + lift, b + lift)


# -- the per-channel blend rules ---------------------------------------------
# Each takes (base, layer) on 0..1 and answers one channel of the result. The
# non-separable modes (the two "colour" picks and the three HCY swaps) are
# whole-colour functions further down.
def _multiply(b: float, s: float) -> float:
    return b * s


def _screen(b: float, s: float) -> float:
    return b + s - b * s


def _hard_light(b: float, s: float) -> float:
    return _multiply(b, 2 * s) if s <= 0.5 else _screen(b, 2 * s - 1)


def _overlay(b: float, s: float) -> float:
    return _hard_light(s, b)


def _soft_light(b: float, s: float) -> float:
    if s <= 0.5:
        return b - (1 - 2 * s) * b * (1 - b)
    d = ((16 * b - 12) * b + 4) * b if b <= 0.25 else math.sqrt(b)
    return b + (2 * s - 1) * (d - b)


def _color_dodge(b: float, s: float) -> float:
    if b == 0:
        return 0.0
    if s >= 1:
        return 1.0
    return min(1.0, b / (1 - s))


def _color_burn(b: float, s: float) -> float:
    if b >= 1:
        return 1.0
    if s <= 0:
        return 0.0
    return 1 - min(1.0, (1 - b) / s)


def _vivid_light(b: float, s: float) -> float:
    return _color_burn(b, 2 * s) if s <= 0.5 else _color_dodge(b, 2 * s - 1)


def _divide(b: float, s: float) -> float:
    return 1.0 if s == 0 else min(1.0, b / s)


_CHANNEL_RULES: dict[BlendMode, ChannelRule] = {
    BlendMode.MULTIPLY: _multiply,
    BlendMode.SCREEN: _screen,
    BlendMode.OVERLAY: _overlay,
    BlendMode.HARD_LIGHT: _hard_light,
    BlendMode.SOFT_LIGHT: _soft_light,
    BlendMode.COLOR_DODGE: _color_dodge,
    BlendMode.LINEAR_DODGE: lambda b, s: min(1.0, b + s),
    BlendMode.COLOR_BURN: _color_burn,
    BlendMode.LINEAR_BURN: lambda b, s: max(0.0, b + s - 1),
    BlendMode.VIVID_LIGHT: _vivid_light,
    BlendMode.LINEAR_LIGHT: lambda b, s: max(0.0, min(1.0, b + 2 * s - 1)),
    BlendMode.DIFFERENCE: lambda b, s: abs(b - s),
    BlendMode.DARKEN: min,
    BlendMode.LIGHTEN: max,
    BlendMode.DIVIDE: _divide,
}


def blend_rgb(base: Rgb, layer: Rgb, mode: BlendMode) -> Rgb:
    """``layer`` blended over ``base`` in ``mode``, at full opacity."""
    rule = _CHANNEL_RULES.get(mode)
    if rule is not None:
        return _clamp(tuple(rule(b, s) for b, s in zip(base, layer, strict=True)))  # type: ignore[arg-type]
    if mode is BlendMode.DARKER_COLOR:
        return layer if luma(layer) < luma(base) else base
    if mode is BlendMode.LIGHTER_COLOR:
        return layer if luma(layer) > luma(base) else base
    base_hue, base_chroma = _hue_chroma(base)
    layer_hue, layer_chroma = _hue_chroma(layer)
    if mode is BlendMode.HUE:
        return _from_hcy(layer_hue, base_chroma, luma(base))
    if mode is BlendMode.SATURATION:
        return _from_hcy(base_hue, layer_chroma, luma(base))
    if mode is BlendMode.LUMINOSITY:
        return _from_hcy(base_hue, base_chroma, luma(layer))
    raise ValueError(f"unknown blend mode {mode!r}")


# -- the operations -----------------------------------------------------------
@dataclass(frozen=True)
class PaletteOp:
    """One operation with its inputs, applied to a colour by :meth:`apply`.

    ``amount`` is the operation's opacity on ``0..1`` — how far each entry
    moves from itself toward the result — and is ignored by the ones that have
    no half-way (Invert, Set, HSV Shift). ``color`` is the colour an operation
    works with, ignored by the ones that have none (Invert, Grayscale, HSV
    Shift); ``mode`` is read by Blend alone. HSV Shift reads its own three:
    ``hue`` in degrees, wrapped, and ``saturation`` / ``value`` as ``-1..1``
    added to the entry's own and clamped.
    """

    kind: OpKind
    color: int = 0xFFFFFFFF
    amount: float = 1.0
    mode: BlendMode = BlendMode.MULTIPLY
    hue: float = 0.0
    saturation: float = 0.0
    value: float = 0.0

    def apply(self, argb: int) -> int:
        alpha = (argb >> 24) & 0xFF
        base = _rgb(argb)
        kind = self.kind
        if kind is OpKind.INVERT:
            return _argb(alpha, tuple(1 - c for c in base))  # type: ignore[arg-type]
        if kind is OpKind.SET:
            return _argb(alpha, _rgb(self.color))
        if kind is OpKind.HSV_SHIFT:
            h, s, v = _to_hsv(base)
            return _argb(
                alpha,
                _clamp(
                    _from_hsv(
                        h + self.hue / 360,
                        max(0.0, min(1.0, s + self.saturation)),
                        max(0.0, min(1.0, v + self.value)),
                    )
                ),
            )
        if kind is OpKind.GRAYSCALE:
            gray = luma(base)
            result: Rgb = (gray, gray, gray)
        elif kind is OpKind.COLORIZE:
            # The chosen colour's hue and chroma on each entry's own
            # brightness: a palette keeps its shading and takes one tint.
            hue, chroma = _hue_chroma(_rgb(self.color))
            result = _from_hcy(hue, chroma, luma(base))
        elif kind is OpKind.BLEND:
            result = blend_rgb(base, _rgb(self.color), self.mode)
        else:
            raise ValueError(f"unknown operation {kind!r}")
        return _argb(alpha, _mix(base, result, self.amount))

    def apply_all(self, colors: list[int]) -> list[int]:
        return [self.apply(argb) for argb in colors]

    def describe(self) -> str:
        """What was done, for the status line: ``"Multiply #808080 at 50%"``."""
        kind = self.kind
        if kind is OpKind.INVERT:
            return "Invert"
        if kind is OpKind.SET:
            return f"Set to {format_argb(self.color, alpha=False)}"
        if kind is OpKind.HSV_SHIFT:
            return (
                f"HSV shift H{round(self.hue):+d}° "
                f"S{round(self.saturation * 100):+d}% V{round(self.value * 100):+d}%"
            )
        percent = f"{round(self.amount * 100)}%"
        if kind is OpKind.GRAYSCALE:
            return f"Grayscale at {percent}"
        color = format_argb(self.color, alpha=False)
        if kind is OpKind.COLORIZE:
            return f"Colorize {color} at {percent}"
        return f"{self.mode.value} {color} at {percent}"
