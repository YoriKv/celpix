"""Fitting arbitrary colors into a fixed palette: nearest-match quantization.

External art arrives as full 32-bit color — a paste from an image editor, an
imported PNG — but the pixel model is *indices* into a palette the hardware
fixes. This module is the bridge: given the candidate colors an interpretation
can actually reference (the active subpalette window), it maps every incoming
ARGB to the closest one.

The distance is a perceptually weighted RGB metric (the standard "redmean"
approximation), which tracks human color judgement far better than a plain
Euclidean RGB distance at a fraction of the cost of a real Lab conversion:
greens weigh heaviest, and red/blue trade weight with how red the pair is.

**Alpha is a category, not a channel.** Transparency is binary here — retro
targets can't store a partial alpha, so a pixel with *any* opacity is a drawn
color and only a fully clear pixel (alpha 0) is transparent. A transparent pixel
always resolves to index 0 — the retro convention — unconditionally, so a paste
from an external editor drops its clear background on the hole regardless of how
this matcher is configured. Opaque pixels, symmetrically, never match a
transparent palette entry while any opaque candidate exists, so an opaque black
can't land on a transparent slot that merely stores black. (The cut is the
``alpha_threshold`` knob — raised above 1, a band of faint pixels snaps to the
designated transparent entry instead; the default treats alpha 0 as the only
hole.)

The exception is a source with *no* alpha anywhere — an editor that ignores the
channel and leaves every pixel clear. There the zeros carry no meaning, so the
caller flags ``ignore_alpha`` and each pixel matches by RGB as if opaque; a paste
that would otherwise vanish onto index 0 lands as its colors instead.

Qt-free: this is model-layer code, shared by the clipboard paste path and PNG
import — both arrive here through :mod:`celpix.pipeline.importer`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# Below this alpha an incoming pixel counts as transparent rather than as a
# color. Retro targets have no partial transparency to store, so any pixel with
# *any* opacity is a drawn color; only a fully clear pixel (alpha 0) is the hole.
DEFAULT_ALPHA_THRESHOLD = 1


def color_distance(a: int, b: int) -> int:
    """Perceptual distance between two ``0xAARRGGBB`` colors (alpha ignored).

    The redmean weighting: green dominates, and the red/blue split shifts with
    the mean red of the pair. Returns a comparable magnitude, not a metric with
    meaningful units — only the ordering matters.
    """
    ar, ag, ab = (a >> 16) & 0xFF, (a >> 8) & 0xFF, a & 0xFF
    br, bg, bb = (b >> 16) & 0xFF, (b >> 8) & 0xFF, b & 0xFF
    rmean = (ar + br) >> 1
    dr, dg, db = ar - br, ag - bg, ab - bb
    return (
        (((512 + rmean) * dr * dr) >> 8)
        + 4 * dg * dg
        + (((767 - rmean) * db * db) >> 8)
    )


class ColorMatcher:
    """Maps ARGB colors onto the indices of a fixed candidate palette.

    ``colors`` is the window of entries the target interpretation can reference
    — for an indexed codec, the active subpalette (``2**bpp`` entries starting at
    the subpalette row); the returned index is **relative to that window**, which
    is exactly what a tile stores.

    Results are memoised per source color: real art reuses a handful of colors
    across thousands of pixels, so the scan over candidates runs once each.
    """

    __slots__ = (
        "_colors",
        "_opaque",
        "_transparent",
        "_threshold",
        "_ignore_alpha",
        "_cache",
    )

    def __init__(
        self,
        colors: Sequence[int],
        *,
        transparent_index: int | None = 0,
        alpha_threshold: int = DEFAULT_ALPHA_THRESHOLD,
        ignore_alpha: bool = False,
    ) -> None:
        self._colors = list(colors)
        self._threshold = alpha_threshold
        # A whole source with no alpha at all comes from an editor that doesn't
        # write the channel; its zeros are noise, not holes, so every pixel is
        # taken as opaque and matched by RGB. The caller — which sees all the
        # pixels — decides this; the matcher only sees one color at a time.
        self._ignore_alpha = ignore_alpha
        # Candidates an opaque source pixel may match: the transparent entries
        # are excluded so an opaque color can't be swallowed by a slot that is
        # never drawn. If *every* entry is transparent the distinction is
        # meaningless, so fall back to the full set.
        opaque = [
            i for i, c in enumerate(self._colors) if (c >> 24) & 0xFF >= alpha_threshold
        ]
        self._opaque = opaque or list(range(len(self._colors)))
        self._transparent = (
            transparent_index
            if transparent_index is not None and 0 <= transparent_index < len(colors)
            else None
        )
        self._cache: dict[int, tuple[int, bool]] = {}

    def __len__(self) -> int:
        return len(self._colors)

    @property
    def cache(self) -> dict[int, tuple[int, bool]]:
        """Every source color matched so far → its ``(index, exact)`` result.

        Doubles as the histogram of *distinct* colors an import saw, which is
        what makes a "3 of 27 colors approximated" summary possible without a
        second pass over the pixels.
        """
        return self._cache

    def match(self, argb: int) -> tuple[int, bool]:
        """``(index, exact)`` for ``argb`` — ``exact`` when the color is in the
        palette verbatim, so a caller can report how lossy an import was."""
        argb &= 0xFFFFFFFF
        hit = self._cache.get(argb)
        if hit is None:
            hit = self._cache[argb] = self._match_uncached(argb)
        return hit

    def index_of(self, argb: int) -> int:
        return self.match(argb)[0]

    def _match_uncached(self, argb: int) -> tuple[int, bool]:
        if not self._colors:
            return 0, False
        alpha = 0xFF if self._ignore_alpha else (argb >> 24) & 0xFF
        if alpha == 0:
            # A *fully* transparent source pixel is always index 0 — the retro
            # convention, and unconditional so it holds even for a matcher with
            # no designated hole ("no-hole" import) or a threshold that has been
            # moved. "exact" only if index 0 is itself transparent, else the
            # paste gained a color there.
            return 0, (self._colors[0] >> 24) & 0xFF < self._threshold
        if alpha < self._threshold and self._transparent is not None:
            # Partly transparent input: the designated hole, and "exact" only if
            # that entry really is transparent (otherwise the paste gained one).
            entry = self._colors[self._transparent]
            return self._transparent, (entry >> 24) & 0xFF < self._threshold
        rgb = argb & 0xFFFFFF
        best = self._opaque[0]
        best_d = -1
        for i in self._opaque:
            candidate = self._colors[i]
            if candidate & 0xFFFFFF == rgb:
                return i, True
            d = color_distance(argb, candidate)
            if best_d < 0 or d < best_d:
                best, best_d = i, d
        return best, False


@dataclass(frozen=True)
class QuantizeReport:
    """How faithful a quantization was — the basis of the UI's paste summary."""

    pixels: int = 0
    exact_pixels: int = 0
    source_colors: int = 0
    exact_colors: int = 0

    @property
    def approximated_colors(self) -> int:
        return self.source_colors - self.exact_colors

    @property
    def lossless(self) -> bool:
        """True when every source color existed in the palette verbatim."""
        return self.exact_colors == self.source_colors
