"""Plugin API and registry (``docs/design/overview.md`` §3).

Every stage is an extension point and every concrete behaviour — including the
built-ins — is a plugin on this API: data-first, with code as the escape hatch
for what data cannot express.

The names re-exported here are the ones that describe a plugin *as a plugin*, so
a drop-in file in any folder can write ``from celpix.plugins import FormatInfo``:
the descriptors, the format classes, :class:`~celpix.plugins.base.ContainerField`
and :func:`format_size` for a container's optional ``describe``, and the two
helpers a read and a write would otherwise each reimplement.

**The data a plugin handles is not here**, and deliberately — it belongs to the
model, so there is one import path for each name rather than two. A plugin reaches
for it where it lives: :class:`~celpix.core.errors.Stage`,
:class:`~celpix.core.context.PipelineContext` and the ``KEY_*`` hints,
:class:`~celpix.core.capabilities.ContentKind`,
:class:`~celpix.core.index_grid.IndexGrid`, :class:`~celpix.core.palette.Palette`,
:class:`~celpix.core.tilemap.Cell`, :class:`~celpix.core.font.Glyph`, and
:func:`~celpix.core.notices.warn` / :func:`~celpix.core.notices.inform`. The
shipped examples under ``resources/data/plugin-examples`` import them that way.

:class:`~celpix.plugins.base.FileRef` is *not* among the re-exports: it is the
host's own descriptor of where bytes live, and a container is handed the bytes
themselves.
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
