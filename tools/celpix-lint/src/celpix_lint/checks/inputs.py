"""The ``inputs`` block: what an entry binds to the data its plugins declare
they need from outside its own bytes (``docs/design/plugin-inputs.md``).

Keyed by **plugin id**, then by the input's key, and every binding is one of
three shapes told apart by its keys: a region (``offset`` and ``length``), an
integer read from bytes (``offset``, ``width``, ``endian``), or a bare integer.
Either of the first two may add ``entry_index`` to reach into another entry's
resolved bytes, the composite piece's rule and the same positional fragility.

The half that needs the plugin — which keys a codec declares, the range an
integer must fall in, the stride a region's length must divide by — travels in
the registry snapshot (``known.KnownIds.inputs``), because the consequence of
getting it wrong is the quietest failure in the format: the app **refuses** a
binding outside the declared range and drops the whole stage, so a compressed
map whose part count exceeds the codec's spin opens as its raw bytes, with only
a notice to say so. A generator that seeded the context by hand decoded it
perfectly; only the app saw the difference. A project's own plugin declares
nothing the snapshot can see, so those bindings are checked for shape only.
"""

from __future__ import annotations

from celpix_lint.context import Context, EntryView, region_size
from celpix_lint.schema import (
    INPUT_ENDIANS,
    INPUT_STAGES,
    KINDS_WITH_DOCUMENT,
    MAX_INPUT_WIDTH,
    is_int,
)


def check(ctx: Context) -> None:
    for view in ctx.entries:
        if view.raw and "inputs" in view.raw:
            _inputs(ctx, view)


def _inputs(ctx: Context, view: EntryView) -> None:
    data = view.raw["inputs"]
    if not isinstance(data, dict):
        ctx.error(
            "E901",
            f"`inputs` is {type(data).__name__}, not an object — every binding is "
            "ignored",
            pointer=view.at("inputs"),
            entry=view,
            detail="An input a format needs then opens the entry degraded, with a "
            "notice naming it.",
        )
        return
    for plugin_id, bindings in data.items():
        pointer = view.at("inputs", plugin_id)
        if not isinstance(bindings, dict):
            ctx.error(
                "E902",
                f"the bindings for {plugin_id!r} are {type(bindings).__name__}, not "
                "an object — they are ignored",
                pointer=pointer,
                entry=view,
            )
            continue
        _plugin(ctx, view, plugin_id, pointer)
        specs = ctx.ids.input_specs(plugin_id) if ctx.ids.usable else None
        for key, binding in bindings.items():
            at = view.at("inputs", plugin_id, key)
            _binding(ctx, view, binding, at)
            if specs is not None:
                _declared(ctx, view, plugin_id, key, binding, specs, at)


def _plugin(ctx: Context, view: EntryView, plugin_id: str, pointer: str) -> None:
    """The plugin the bindings belong to: known at a stage that takes inputs,
    and one the entry actually reads through."""
    if not ctx.ids.usable:
        return
    if not any(ctx.ids.has(stage, plugin_id) for stage in INPUT_STAGES):
        elsewhere = ctx.ids.stage_of(plugin_id)
        if elsewhere is not None:
            ctx.error(
                "E903",
                f"`inputs` names {plugin_id!r}, a {elsewhere} id — only a "
                f"{' or '.join(INPUT_STAGES)} plugin takes inputs",
                pointer=pointer,
                entry=view,
                detail="Nothing reads these bindings; they are dropped on the next "
                "save.",
            )
        elif ctx.ids.authoritative:
            ctx.error(
                "E904",
                f"`inputs` names {plugin_id!r}, which nothing is registered under",
                pointer=pointer,
                entry=view,
                detail="The bindings are kept until the plugin is installed and "
                "pruned by the next save if it never is.",
            )
        else:
            ctx.warn(
                "W905",
                f"`inputs` names {plugin_id!r}, which is not one of celPix's built-in "
                "compression or tilemap ids",
                pointer=pointer,
                entry=view,
                detail="A project plugin declaring inputs is the usual case here "
                "(no built-in declares any). Re-run with --live to check against "
                "your own registry.",
            )
        return
    # A slice reads through exactly one compression scheme; bindings for any
    # other are inert. A file's are the preview's, one set per codec, and a
    # tilemap's engine cannot be told from its preset id here.
    if view.kind == "slice":
        codec = view.raw.get("compression_id", "compression.none")
        if ctx.ids.has("compression", plugin_id) and plugin_id != codec:
            ctx.info(
                "I906",
                f"bindings for {plugin_id!r} on a slice read through {codec!r}",
                pointer=pointer,
                entry=view,
                detail="They are kept so that switching the codec back costs "
                "nothing, and pruned by the next save.",
            )


def _declared(
    ctx: Context,
    view: EntryView,
    plugin_id: str,
    key: str,
    binding: object,
    specs: list[dict],
    pointer: str,
) -> None:
    """The binding against what the plugin declared: a key it asks for, and a
    literal inside the range it accepts."""
    spec = next((s for s in specs if s.get("key") == key), None)
    if spec is None:
        ctx.warn(
            "W913",
            f"{plugin_id!r} declares no input {key!r}",
            pointer=pointer,
            entry=view,
            detail="It is ignored: the plugin asks for "
            + (", ".join(repr(s.get("key")) for s in specs) or "nothing")
            + ". A key from another codec, or a typo.",
        )
        return
    if isinstance(binding, bool) or not isinstance(binding, int):
        return
    lo, hi = spec.get("minimum", 0), spec.get("maximum", 0xFFFF_FFFF)
    if not (lo <= binding <= hi):
        ctx.error(
            "E914",
            f"{key!r} = {binding} is outside the {lo}..{hi} that {plugin_id!r} "
            "accepts — celPix refuses the binding and drops the stage",
            pointer=pointer,
            entry=view,
            detail="The entry opens with that stage removed, so a compressed "
            "slice shows its compressed bytes as if they were the data, with "
            "only a notice to say why. Bring the value into range, or raise the "
            "plugin's declared maximum if the value is what the game's loader "
            "really passes.",
        )


def _binding(ctx: Context, view: EntryView, binding: object, pointer: str) -> None:
    if isinstance(binding, bool):
        ctx.error(
            "E907",
            "a binding is a boolean — it is skipped, and the input stays unbound",
            pointer=pointer,
            entry=view,
        )
        return
    if isinstance(binding, int):
        if binding < 0:
            ctx.warn(
                "W908",
                f"a literal integer input is {binding} — no format accepts a "
                "negative value, so the entry opens degraded",
                pointer=pointer,
                entry=view,
            )
        return
    if not isinstance(binding, dict):
        ctx.error(
            "E907",
            f"a binding is {type(binding).__name__} — it is skipped, and the input "
            "stays unbound",
            pointer=pointer,
            entry=view,
            detail="A region is {offset, length}; an integer read from bytes is "
            "{offset, width, endian}; a literal is a bare number.",
        )
        return
    offset = binding.get("offset")
    if not is_int(offset) or offset < 0:
        ctx.error(
            "E909",
            f"`offset` is {offset!r} — the binding is skipped, and the input stays "
            "unbound",
            pointer=pointer + "/offset",
            entry=view,
        )
        return
    reaches = _extent(ctx, view, binding, pointer)
    if reaches is None:
        return
    at = binding.get("entry_index")
    if "entry_index" in binding:
        _target(ctx, view, at, pointer + "/entry_index")
        return
    # Into the entry's own file: absolute, so it is measured against the whole
    # joined region exactly as a slice offset is.
    size = region_size(ctx.doc, view) if ctx.check_files else None
    if size is not None and offset + reaches > size:
        ctx.error(
            "E912",
            f"the binding reaches to {offset + reaches:#x}, past the end of the file "
            f"({size:#x} bytes) — the input does not resolve",
            pointer=pointer,
            entry=view,
            detail="The entry opens degraded, with a notice naming the input.",
        )


def _extent(ctx: Context, view: EntryView, binding: dict, pointer: str) -> int | None:
    """How many bytes the binding reads, by its shape; None when its shape is
    wrong (reported)."""
    if "width" in binding:
        width = binding.get("width")
        if not is_int(width) or width <= 0:
            ctx.error(
                "E910",
                f"`width` is {width!r} — the binding is skipped, and the input stays "
                "unbound",
                pointer=pointer + "/width",
                entry=view,
            )
            return None
        if width > MAX_INPUT_WIDTH:
            ctx.error(
                "E910",
                f"`width` is {width}; at most {MAX_INPUT_WIDTH} bytes can be read as "
                "one number — the input does not resolve",
                pointer=pointer + "/width",
                entry=view,
            )
            return None
        endian = binding.get("endian", "big")
        if endian not in INPUT_ENDIANS:
            ctx.warn(
                "W911",
                f'`endian` is {endian!r} — anything but "little" reads as big-endian',
                pointer=pointer + "/endian",
                entry=view,
            )
        return width
    length = binding.get("length")
    if not is_int(length) or length < 0:
        ctx.error(
            "E909",
            f"`length` is {length!r} — the binding is skipped, and the input stays "
            "unbound",
            pointer=pointer + "/length",
            entry=view,
        )
        return None
    if length == 0:
        ctx.warn(
            "W913",
            "a region of length 0 — no format accepts an empty region, so the entry "
            "opens degraded",
            pointer=pointer + "/length",
            entry=view,
        )
    return length


def _target(ctx: Context, view: EntryView, at: object, pointer: str) -> None:
    """An ``entry_index`` — in range, and naming something that can supply bytes."""
    if not is_int(at):
        ctx.error(
            "E914",
            f"`entry_index` is {at!r}, not an integer — the binding is dropped on load",
            pointer=pointer,
            entry=view,
            detail="The input stays unbound, and the entry opens degraded.",
        )
        return
    if at == -1:
        ctx.warn(
            "W915",
            "`entry_index` is -1 — the binding is dropped on load",
            pointer=pointer,
            entry=view,
            detail="-1 is what celPix writes for a binding onto an entry that is no "
            "longer open. The input stays unbound.",
        )
        return
    if not 0 <= at < len(ctx.entries):
        ctx.error(
            "E916",
            f"`entry_index` {at} is out of range (there are {len(ctx.entries)} "
            "entries) — the binding is dropped on load",
            pointer=pointer,
            entry=view,
        )
        return
    target = ctx.entries[at]
    if at == view.index:
        ctx.error(
            "E917",
            "`entry_index` names this entry itself — the input does not resolve",
            pointer=pointer,
            entry=view,
        )
        return
    if target.skipped or target.kind not in KINDS_WITH_DOCUMENT:
        ctx.error(
            "E917",
            f"`entry_index` names entry {at}, which cannot supply bytes — the input "
            "does not resolve",
            pointer=pointer,
            entry=view,
            detail="Only a file, slice or composite view holding pixels can be read "
            "from; a palette, a bookmark or a dropped entry cannot.",
        )
        return
    if target.content_kind != "pixels":
        ctx.error(
            "E917",
            f"`entry_index` names entry {at}, a {target.content_kind} entry — the "
            "input does not resolve",
            pointer=pointer,
            entry=view,
            detail="A tilemap's buffer is borrowed art, not bytes of its own.",
        )
        return
    if _names_an_entry(target):
        ctx.error(
            "E918",
            f"`entry_index` names entry {at}, whose own inputs reach into another "
            "entry — the input does not resolve",
            pointer=pointer,
            entry=view,
            detail="Depth is one hop: a source may be compressed or reshaped, but not "
            "itself read through another entry, so a cycle is refused.",
        )


def _names_an_entry(view: EntryView) -> bool:
    data = view.raw.get("inputs")
    if not isinstance(data, dict):
        return False
    return any(
        isinstance(binding, dict) and "entry_index" in binding
        for bindings in data.values()
        if isinstance(bindings, dict)
        for binding in bindings.values()
    )
