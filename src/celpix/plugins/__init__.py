"""Plugin API and registry (``docs/design/overview.md`` §3).

Every stage is an extension point and every concrete behaviour — including the
built-ins — is a plugin on this API: data-first, with code as the escape hatch
for what data cannot express.

The plugin-authoring names are re-exported here so a drop-in file in any folder
can write ``from celpix.plugins import FormatInfo``. That is the whole of what a
plugin needs: the three descriptors, the two protocols' worth of format classes,
:class:`~celpix.plugins.base.ContainerField` and :func:`format_size` for a
container's optional ``describe``, and the two helpers a read and a write would
otherwise each reimplement.

:class:`~celpix.plugins.base.FileRef` is *not* among them: it is the host's own
descriptor of where bytes live, and a container is handed the bytes themselves.
"""

from celpix.plugins.base import (
    ContainerField,
    PluginInfo,
    ReadSource,
    WriteTarget,
    format_size,
    plain_read,
    splice,
)
from celpix.plugins.formats import (
    FormatInfo,
    PaletteFormat,
    PixelFormat,
    TilemapFormat,
)

__all__ = [
    "ContainerField",
    "FormatInfo",
    "PaletteFormat",
    "PixelFormat",
    "PluginInfo",
    "ReadSource",
    "TilemapFormat",
    "WriteTarget",
    "format_size",
    "plain_read",
    "splice",
]
