"""Built-in plugin registration.

Every built-in behaviour is a plugin on the same API third parties would use
(``docs/design/overview.md`` §3). :func:`register_builtins` wires the stage
engines and loads every shipped preset (TOML data files under
``resources/data/presets/``) into a registry. Built-in presets use the *same*
TOML schema and folder-gives-the-stage layout as user-dropped presets (see
:mod:`celpix.plugins.discovery`) — they are simply the ones that ship inside the
package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from celpix import resources
from celpix.plugins.discovery import PRESET_FOLDER_STAGE, preset_from_toml

from .chunky_codec import ChunkyCodec
from .color_codec import ColorCodec
from .container_read import INesReader, SmdReader, SnesInterleavedReader
from .direct_color_codec import DirectColorCodec
from .indexed_codec import IndexedColorCodec
from .konami_rle import (
    KonamiFdsRleCompress,
    KonamiFdsRleDecompress,
    KonamiNesRleCompress,
    KonamiNesRleDecompress,
)
from .linear_codec import LinearBespokeCodec
from .lz16 import Lz16Compress, Lz16Decompress
from .lz_command import Lz1Compress, Lz1Decompress, Lz2Compress, Lz2Decompress
from .m7_interleave import M7VramCompress, M7VramDecompress
from .packed_codec import PackedCodec
from .passthrough import PassthroughCompress, PassthroughDecompress
from .planar_codec import PlanarCodec
from .raw_file import RawFileReader, RawFileWriter
from .wide_codecs import Pce2bpp16Codec, PceSgCodec, Wide1bppCodec

if TYPE_CHECKING:
    from celpix.core.errors import Stage
    from celpix.plugins.registry import Registry


def register_builtins(reg: Registry) -> None:
    for plugin in (
        RawFileReader(),
        RawFileWriter(),
        INesReader(),
        SmdReader(),
        SnesInterleavedReader(),
        PassthroughDecompress(),
        PassthroughCompress(),
        KonamiNesRleDecompress(),
        KonamiNesRleCompress(),
        KonamiFdsRleDecompress(),
        KonamiFdsRleCompress(),
        M7VramDecompress(),
        M7VramCompress(),
        Lz1Decompress(),
        Lz1Compress(),
        Lz2Decompress(),
        Lz2Compress(),
        Lz16Decompress(),
        Lz16Compress(),
        PlanarCodec(),
        PackedCodec(),
        ChunkyCodec(),
        LinearBespokeCodec(),
        Wide1bppCodec(),
        Pce2bpp16Codec(),
        PceSgCodec(),
        DirectColorCodec(),
        ColorCodec(),
        IndexedColorCodec(),
    ):
        reg.register(plugin)

    for stage, text in _shipped_presets():
        reg.register_preset(preset_from_toml(text, stage))


def _shipped_presets() -> list[tuple[Stage, str]]:
    # The shipped tree mirrors the user plugin layout: the folder name gives the
    # stage (the shared PRESET_FOLDER_STAGE map), so preset TOMLs carry none.
    named: list[tuple[str, Stage, str]] = []
    for subdir, stage in PRESET_FOLDER_STAGE.items():
        node = resources.resource("data", "presets", subdir)
        for entry in node.iterdir():
            if entry.name.endswith(".toml"):
                named.append((entry.name, stage, entry.read_text(encoding="utf-8")))
    # Stable order regardless of filesystem iteration order.
    named.sort(key=lambda item: item[0])
    return [(stage, text) for _, stage, text in named]
