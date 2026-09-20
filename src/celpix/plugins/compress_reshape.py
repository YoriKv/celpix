"""Compress & Reshape — a compression scheme with a reshape run over its output.

A game that bolts a second pass onto a stock compressor needs two transforms where
a slice has one Compression slot. The running sum is the case that built it: a
title screen's map is packed as *differences*, so the loader unpacks the stream
and then sums it, and neither step alone reads the map
(:mod:`celpix.plugins.builtins.running_sum`). The Reshape stage cannot hold the
second step, because it runs **before** decompression — over the packed bytes
(``docs/design/reshape-stage.md`` §4).

So the pair is one plugin, in the Compression slot, described by a TOML preset:

```toml
id = "compression.rnc2-running-sum"
name = "RNC method 2 + running sum"
engine_id = "compression.compress-reshape"

[params]
compression = "compression.rnc2"
reshape = "reshape.running-sum"
```

```
load:  decompress -> reshape
save:  unshape    -> compress
```

Like a reshape preset it is adapted into an **ordinary plugin instance** at load
(:func:`compress_reshape_from_spec`), because the Compression stage resolves plain
plugin ids everywhere — the pipeline, the pickers, a slice's ``compression_id``.
Adapting at the edge leaves all of that untouched, and it is what lets everything
the host decides *off the plugin object* be decided before any entry opens:

- **Whether it saves.** Shipping ``compress`` is the declaration
  (:func:`~celpix.plugins.base.writes_back`), so the method exists here only when
  both members can run backwards. A pair with a view-only half opens view-only,
  rather than looking writable until the save fails.
- **Whether it finds its own end**, and the rest of the decode contract —
  ``compression.compressed-size``, ``compression.complete``, the partial flag, the
  surround. All of it is the *compression* member's, which is the half that reads
  the file's bytes. A reshape is length-preserving and reads none of those keys,
  so it cannot disturb them.
- **What it needs from outside its bytes.** The compression member's inputs are
  declared as this plugin's own, so an entry binds them under this plugin's id and
  the member finds them on the context exactly where it always looks
  (``docs/design/plugin-inputs.md`` §2).

**The reshape sees the whole decompressed stream as its region**, which is the
right reading: a structure's output is a complete buffer with nothing around it.
Under the window preview the stream may be cut short, and a reshape whose
boundaries are fractions of the region then reads a different region — the
preview is a best effort there, as it says it is. A sum is unaffected, a prefix of
the sum being the sum of the prefix.

Qt-free.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import (
    NO_COMPRESSION,
    NO_RESHAPE,
    CompressionPlugin,
    PluginInfo,
    ReshapePlugin,
    check_declared_stage,
    writes_back,
)

if TYPE_CHECKING:
    from celpix.plugins.discovery import RegistryLike

COMPRESS_RESHAPE_ENGINE = "compression.compress-reshape"

# The typed folder a preset of this engine is loaded from, under any plugin root.
FOLDER = "compression"


class CompressReshape:
    """One compression scheme and one reshape as a single compression plugin."""

    def __init__(
        self,
        plugin_id: str,
        name: str,
        compression: CompressionPlugin,
        reshape: ReshapePlugin,
        category: str = "",
    ) -> None:
        self._compression = compression
        self._reshape = reshape
        self.info = PluginInfo(
            id=plugin_id,
            name=name,
            stage=Stage.COMPRESSION,
            self_delimiting=compression.info.self_delimiting,
            category=category,
            inputs=compression.info.inputs,
        )
        # Bound per instance rather than defined on the class: whether a save-back
        # works is a fact about the two members, and the host asks it by looking
        # for the method.
        if writes_back(compression, Stage.COMPRESSION) and writes_back(
            reshape, Stage.RESHAPE
        ):
            self.compress = self._compress

    @property
    def members(self) -> tuple[str, str]:
        """``(compression id, reshape id)`` — what this pair was built from."""
        return self._compression.info.id, self._reshape.info.id

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return self._reshape.reshape(self._compression.decompress(data, ctx), ctx)

    def _compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return self._compression.compress(self._reshape.unshape(data, ctx), ctx)


def _member(reg: RegistryLike, stage: Stage, params: dict, key: str, none: str):
    """The plugin ``params[key]`` names at ``stage``, refusing what makes no pair."""
    wanted = params.get(key)
    if not isinstance(wanted, str) or not wanted:
        raise ValueError(f"params.{key} must name a {stage.value} plugin")
    try:
        plugin = reg.plugin(stage, wanted)
    except KeyError:
        raise ValueError(
            f"params.{key} names {wanted!r}, and no {stage.value} plugin with "
            "that id is loaded"
        ) from None
    if plugin.info.id == none:
        # Half a pair is a plugin that already exists, under a picker that says
        # what it is. Accepting it would put the same behaviour in the list twice.
        raise ValueError(
            f"params.{key} is the pass-through, which leaves nothing to pair: pick "
            f"the other half directly in its own picker"
        )
    if isinstance(plugin, CompressReshape):
        raise ValueError(
            f"params.{key} names {wanted!r}, itself a Compress & Reshape plugin; "
            "a pair is one compression scheme and one reshape"
        )
    return plugin


def compress_reshape_from_spec(spec: dict, reg: RegistryLike) -> CompressReshape:
    """Build the plugin a parsed preset spec describes, its members out of ``reg``.

    The members are resolved **now** rather than at decode time, which is what
    makes the pair's save support, end-finding and inputs plain attributes like any
    other plugin's. The price is an ordering rule the loader keeps: a pair is built
    after everything else in its plugin root, so it may name a plugin dropped
    beside it (:func:`~celpix.plugins.discovery.load_directory`).
    """
    engine = spec.get("engine_id")
    if engine != COMPRESS_RESHAPE_ENGINE:
        raise ValueError(
            f"engine_id {engine!r} is not a compression engine "
            f"(expected {COMPRESS_RESHAPE_ENGINE!r})"
        )
    check_declared_stage(spec, Stage.COMPRESSION)
    params = spec.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("params must be a table")
    return CompressReshape(
        spec["id"],
        spec["name"],
        _member(reg, Stage.COMPRESSION, params, "compression", NO_COMPRESSION),
        _member(reg, Stage.RESHAPE, params, "reshape", NO_RESHAPE),
        spec.get("category", ""),
    )


# -- authoring ---------------------------------------------------------------
# The preset is five lines a user could type, and the dialog that writes it
# (``ui/compress_reshape_dialog.py``) exists so they need not know the ids. What
# it writes is built here so the file's shape is stated beside the loader that
# reads it, and so a test can hold the two to each other without a window.

ID_PREFIX = "compression."


def slug(name: str) -> str:
    """``name`` as the last segment of a plugin id, and as a filename stem.

    Lowercase ASCII letters, digits and single hyphens — the shape every shipped
    id has (``docs/design/plugin-system.md`` §3). Empty when ``name`` holds
    nothing that survives, which a caller reports rather than inventing an id.
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def preset_toml(plugin_id: str, name: str, compression_id: str, reshape_id: str) -> str:
    """The TOML source of one Compress & Reshape preset."""

    # A JSON string is a TOML basic string: the same quotes, the same escapes.
    def quoted(text: str) -> str:
        return json.dumps(text, ensure_ascii=False)

    return (
        "# Compress & Reshape: a compression scheme, then a reshape run over what\n"
        "# it unpacks. On save the two run backwards, in the opposite order.\n"
        f"id = {quoted(plugin_id)}\n"
        f"name = {quoted(name)}\n"
        f"engine_id = {quoted(COMPRESS_RESHAPE_ENGINE)}\n"
        "\n"
        "[params]\n"
        f"compression = {quoted(compression_id)}\n"
        f"reshape = {quoted(reshape_id)}\n"
    )


def preset_path(plugin_root: str | Path, plugin_id: str) -> Path:
    """Where the preset for ``plugin_id`` lives under the plugin root."""
    return Path(plugin_root) / FOLDER / f"{plugin_id.removeprefix(ID_PREFIX)}.toml"


def write_preset(
    plugin_root: str | Path,
    plugin_id: str,
    name: str,
    compression_id: str,
    reshape_id: str,
) -> Path:
    """Write a new preset under ``plugin_root`` and return its path.

    Creates the root and the typed folder, since a project has no ``plugins/``
    until something is put in it. **Never overwrites**: the file is opened for
    exclusive creation, so a name that is already taken raises
    :class:`FileExistsError` rather than replacing somebody's plugin.
    """
    path = preset_path(plugin_root, plugin_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(preset_toml(plugin_id, name, compression_id, reshape_id))
    return path
