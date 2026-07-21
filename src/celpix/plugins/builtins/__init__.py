"""Built-in plugin registration.

Every built-in behaviour is a plugin on the same API third parties would use
(``docs/design/overview.md`` §3). :func:`register_builtins` wires the stage
engines and loads every shipped preset (TOML data files under
``resources/data/presets/``) into a registry. Built-in presets use the *same*
self-describing TOML schema as user-dropped presets (see
:mod:`celpix.plugins.discovery`) — they are simply the ones that ship inside the
package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from celpix import resources
from celpix.plugins.discovery import preset_from_toml

from .color_codec import ColorCodec
from .passthrough import PassthroughCompress, PassthroughDecompress
from .planar_codec import PlanarCodec
from .raw_file import RawFileReader, RawFileWriter

if TYPE_CHECKING:
    from celpix.plugins.registry import Registry

# Subdirectories under resources/data/presets/ scanned for shipped preset TOML.
# (Organisational only — each preset's stage comes from its own ``stage`` field.)
_PRESET_DIRS = ("pixel", "palette")


def register_builtins(reg: Registry) -> None:
    for plugin in (
        RawFileReader(),
        RawFileWriter(),
        PassthroughDecompress(),
        PassthroughCompress(),
        PlanarCodec(),
        ColorCodec(),
    ):
        reg.register(plugin)

    for text in _shipped_preset_texts():
        reg.register_preset(preset_from_toml(text))


def _shipped_preset_texts() -> list[str]:
    named: list[tuple[str, str]] = []
    for subdir in _PRESET_DIRS:
        node = resources.resource("data", "presets", subdir)
        for entry in node.iterdir():
            if entry.name.endswith(".toml"):
                named.append((entry.name, entry.read_text(encoding="utf-8")))
    # Stable order regardless of filesystem iteration order.
    named.sort()
    return [text for _, text in named]
