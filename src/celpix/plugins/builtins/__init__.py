"""Built-in plugin registration.

Every built-in behaviour is a plugin on the same API third parties use
(``docs/design/overview.md`` §3). :func:`register_builtins` wires the stage
engines into a registry and loads every shipped preset from the TOML under
``resources/data/presets/``. Those use the same schema and
folder-gives-the-stage layout as user-dropped presets
(:mod:`celpix.plugins.discovery`); they simply ship inside the package.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.9/3.10
    import tomli as tomllib

from celpix import resources
from celpix.plugins.discovery import (
    INTERPRET_FOLDER_STAGE,
    RESHAPE_ENGINES,
    preset_from_toml,
)

from .byte_swap import ByteSwapReshape
from .color_codec import ColorCodec
from .containers import (
    CopierHeaderContainer,
    INesContainer,
    SmdContainer,
    SnesInterleavedContainer,
)
from .direct_color_codec import DirectColorCodec
from .gb_rom import GbRomContainer
from .indexed_codec import IndexedColorCodec
from .konami_rle import KonamiFdsRle, KonamiNesRle
from .linear_codec import LinearBespokeCodec
from .lz16 import Lz16Compression
from .lz_command import Lz1, Lz2
from .m7_vram import M7VramReshape
from .n64_rom import N64RomContainer
from .nibble_planar_codec import NibblePlanarCodec
from .packbits import PackBitsCompression
from .packed_codec import PackedCodec
from .passthrough import PassthroughCompression, PassthroughReshape
from .planar_codec import PlanarCodec
from .raw_file import RawFileContainer
from .split_planes import split_part_plugins

if TYPE_CHECKING:
    from celpix.plugins.base import Preset, ReshapePlugin
    from celpix.plugins.registry import Registry


def register_builtins(reg: Registry) -> None:
    for plugin in (
        RawFileContainer(),
        CopierHeaderContainer(),
        INesContainer(),
        SmdContainer(),
        SnesInterleavedContainer(),
        GbRomContainer(),
        N64RomContainer(),
        PassthroughReshape(),
        M7VramReshape(),
        ByteSwapReshape(),
        *split_part_plugins(),
        PassthroughCompression(),
        KonamiNesRle(),
        KonamiFdsRle(),
        Lz1(),
        Lz2(),
        Lz16Compression(),
        PackBitsCompression(),
        PlanarCodec(),
        PackedCodec(),
        NibblePlanarCodec(),
        LinearBespokeCodec(),
        DirectColorCodec(),
        ColorCodec(),
        IndexedColorCodec(),
    ):
        reg.register(plugin)

    for preset in _shipped_presets():
        reg.register_preset(preset)
    for plugin in _shipped_reshape_plugins():
        reg.register(plugin)


@lru_cache(maxsize=8)
def _read_preset_dir(subdir: str) -> tuple[str, ...]:
    """Every ``.toml`` under ``data/presets/<subdir>``, in name order.

    Cached because this is *read-only package data*: the shipped tree cannot
    change while the app runs, yet on a Windows drive mounted into WSL these reads
    cost ~0.35 s a pass, and a registry is built for every window, every plugin
    refresh and every test. Name order keeps the registered order stable
    regardless of how the filesystem iterates.
    """
    node = resources.resource("data", "presets", subdir)
    named = sorted(
        (entry.name, entry.read_text(encoding="utf-8"))
        for entry in node.iterdir()
        if entry.name.endswith(".toml")
    )
    return tuple(text for _, text in named)


@lru_cache(maxsize=1)
def _shipped_presets() -> tuple[Preset, ...]:
    """Every shipped pixel/palette preset, parsed once per process.

    The parse is cached as well as the read, being almost all of what building a
    registry costs. Sharing the objects across registries is safe:
    :class:`~celpix.plugins.base.Preset` is frozen and nothing mutates a preset's
    ``params`` in place — the pipeline derives a new dict when it needs different
    values.
    """
    # The shipped tree mirrors the user plugin layout: the folder name gives the
    # stage (the shared INTERPRET_FOLDER_STAGE map), so preset TOMLs carry none.
    return tuple(
        preset_from_toml(text, stage)
        for subdir, stage in INTERPRET_FOLDER_STAGE.items()
        for text in _read_preset_dir(subdir)
    )


def _shipped_reshape_plugins() -> list[ReshapePlugin]:
    """The shipped ``reshape/*.toml`` tables, adapted into reshape plugins.

    A reshape preset becomes a *plugin* rather than a ``Preset``: its engine_id
    discriminates which adapter builds it instead of naming an engine to resolve
    at decode time, exactly as for user-dropped ones
    (:func:`~celpix.plugins.discovery._load_reshape_preset`). Built per registry
    rather than cached, since only the file read is expensive and
    :func:`_read_preset_dir` already caches that.
    """
    return [
        RESHAPE_ENGINES[spec["engine_id"]](spec)
        for spec in (tomllib.loads(text) for text in _read_preset_dir("reshape"))
    ]
