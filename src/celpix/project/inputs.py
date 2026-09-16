"""Plugin inputs: what an entry binds to the data a stage needs from outside its
own bytes, and how the host resolves those bindings into values.

A stage plugin is handed one byte range plus a context. A scheme whose code
table is shared by forty streams and stored ahead of them, or a sprite mapping
kept as parallel arrays, needs bytes no slice can reach — so the plugin
**declares** what it needs (:class:`~celpix.plugins.base.InputSpec`), the entry
**binds** each one (:attr:`~celpix.project.workspace.Entry.inputs`), and this
module turns the bindings into the ``bytes`` and ``int`` values the pipeline
hands over as :data:`~celpix.core.context.KEY_INPUTS`
(``docs/design/plugin-inputs.md``).

Three rules, each stated once here:

- **Where a region's bytes come from.** ``entry`` ``None`` on a binding is *the
  file this entry's bytes come from* — a slice's parent, or a file entry itself
  — read exactly as a raw slice at that offset would read it: the parent's own
  view buffer where it reorders or holds unsaved edits, the file otherwise
  (:func:`~celpix.project.workspace.entry_view_bytes`). A named entry supplies
  its **resolved** bytes, the composite piece's rule.
- **Depth is one hop.** A binding may name an entry that is compressed or
  reshaped, but not one whose own inputs resolve through another entry
  (:func:`can_supply_input`) — a gate read off the bindings before anything is
  loaded, so a cycle is refused rather than recursed into.
- **Every failure is a problem, not an exception.** The caller puts the stage on
  its fallback and the load says why; the file still opens.

Qt-free, like everything under ``project``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from celpix.core.capabilities import ContentKind
from celpix.core.context import KEY_SOURCE_OFFSET
from celpix.core.errors import PipelineError, Stage
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import STAGE_DEFAULT_PRESET, FileRef, InputKind, InputSpec
from celpix.plugins.registry import Registry

if TYPE_CHECKING:
    from celpix.project.workspace import Entry, Workspace

__all__ = [
    "INPUT_STAGES",
    "Bindings",
    "InputBinding",
    "InputProblem",
    "IntegerFromBytes",
    "RegionBinding",
    "ResolvedInputs",
    "StageInputs",
    "can_supply_input",
    "declared_inputs",
    "engine_id_of",
    "input_specs",
    "iter_bindings",
    "names_entry",
    "plugin_id_at",
    "prune_bindings",
    "resolve_inputs",
    "with_bindings",
]


@dataclass(frozen=True)
class RegionBinding:
    """Where a plugin's **region** input gets its bytes: a byte range of the
    entry's own file, or of another entry.

    ``entry`` is ``None`` for **the file this entry's bytes come from** — a
    slice's parent, or a file entry itself — and then ``offset`` is absolute
    from byte 0 of that file in the ``slice_offset`` coordinate space: past
    nothing, so a container appearing or disappearing does not move it, and
    counted through the join for a region spread over several chips. ``None``
    rather than the parent object so that a binding made on a file for its
    compression preview copies onto a slice of that file unchanged
    (:func:`~celpix.project.workspace.slice_of`) — the one shape means "this
    file" wherever it lands.

    With an ``entry`` it is the composite piece's shape and rules
    (:class:`~celpix.project.workspace.CompositePiece`): the range addresses
    that entry's **resolved** bytes — the decompressed stream where it is
    compressed — which is what lets a table that is itself compressed, or that
    lives in another file, be an input. Held as the object rather than a
    position for :class:`~celpix.project.workspace.TileSource`'s reason, and
    written as a position by :mod:`~celpix.project.projectfile` alone.

    ``length`` is in bytes and must be a whole number of the spec's ``stride``.
    """

    entry: Entry | None = None
    offset: int = 0
    length: int = 0


@dataclass(frozen=True)
class IntegerFromBytes:
    """An **integer** input read out of the file rather than typed.

    The other shape an integer binding takes, beside a bare ``int`` literal. A
    format that keeps a stream's output size in a length table is honestly
    described by *where that number is*, not by a copy of it: reading it from
    there is what keeps the decode right when the table is edited. Same source
    rule as :class:`RegionBinding` — ``entry`` None is the entry's own file —
    and ``width`` bytes at ``offset``, big-endian unless ``little_endian``.
    """

    entry: Entry | None = None
    offset: int = 0
    width: int = 2
    little_endian: bool = False


#: One bound input: a region, an integer read from bytes, or a literal integer.
InputBinding = RegionBinding | IntegerFromBytes | int
#: One plugin's bindings, by the spec's key.
Bindings = dict[str, InputBinding]

#: The stages an entry can bind inputs for, in pipeline order. Compression and
#: the tilemap codec are the two real cases; a container is handed its whole
#: source and needs none, and no pixel or palette format has asked yet.
INPUT_STAGES: tuple[Stage, ...] = (Stage.COMPRESSION, Stage.INTERPRET_TILEMAP)

# The widest integer a binding may read from bytes: a 64-bit word.
_MAX_INT_WIDTH = 8


@dataclass(frozen=True)
class StageInputs:
    """What one stage of an entry declares: the plugin, and its specs."""

    stage: Stage
    plugin_id: str
    specs: tuple[InputSpec, ...]


@dataclass(frozen=True)
class InputProblem:
    """Why one input could not be resolved, in the notice's two halves.

    ``summary`` is the single line a list shows; ``detail`` follows the tooltip
    rule — hard-wrapped with explicit newlines, since Qt never wraps one.
    """

    key: str
    label: str
    detail: str

    @property
    def summary(self) -> str:
        return f"Input not resolved: {self.label}"


@dataclass(frozen=True)
class ResolvedInputs:
    """One stage's bindings resolved: the values, and what could not be.

    ``values`` holds every input that resolved — an optional one nobody bound is
    **absent**, not None, unless its spec names a default. Any problem at all
    means the stage cannot honestly run, required or optional: an optional table
    bound to a range off the end of the file is a mistake to report, not a
    binding to quietly drop.
    """

    values: dict[str, bytes | int]
    problems: tuple[InputProblem, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


# -- what a plugin declares -----------------------------------------------
def input_specs(
    registry: Registry, stage: Stage, plugin_id: str
) -> tuple[InputSpec, ...]:
    """The inputs ``plugin_id`` declares at ``stage``; empty for one this build
    has not got, which the caller reads as "nothing to bind" — the missing
    plugin is reported by the stage fallback, not here."""
    try:
        plugin = registry.plugin(stage, plugin_id)
    except KeyError:
        return ()
    return tuple(plugin.info.inputs)


def engine_id_of(registry: Registry, preset_id: str | None) -> str:
    """The code plugin behind an interpret preset, or ``""`` for none.

    An Interpret stage's inputs are declared by the **engine** — a preset is
    data — so a tilemap entry's bindings are keyed by the engine's id and this
    is the hop from the preset the entry names to that key.
    """
    if not preset_id:
        return ""
    try:
        return registry.preset(preset_id).engine_id
    except KeyError:
        return ""


def plugin_id_at(entry: Entry, stage: Stage, registry: Registry) -> str:
    """The plugin ``entry``'s bindings for ``stage`` are keyed by, or ``""``.

    A slice's compression scheme; a tilemap's cell engine. A **file** entry has
    no compression of its own — its bindings at that stage are the preview's,
    keyed by whatever codec the preview is set to, which the UI asks for
    directly rather than through here.
    """
    from celpix.project.workspace import EntryKind  # noqa: PLC0415 — circular by nature

    if stage is Stage.COMPRESSION:
        return entry.compression_id if entry.kind is EntryKind.SLICE else ""
    if stage is Stage.INTERPRET_TILEMAP:
        if entry.content_kind is not ContentKind.TILEMAP:
            return ""
        return engine_id_of(registry, entry.tilemap_preset_id)
    return ""


def declared_inputs(entry: Entry, registry: Registry) -> list[StageInputs]:
    """Every stage of ``entry`` whose current plugin declares inputs, in
    pipeline order — what the Inputs window has sections for, and what decides
    whether the entry has any to show at all."""
    out: list[StageInputs] = []
    for stage in INPUT_STAGES:
        plugin_id = plugin_id_at(entry, stage, registry)
        specs = input_specs(registry, stage, plugin_id) if plugin_id else ()
        if specs:
            out.append(StageInputs(stage, plugin_id, specs))
    return out


# -- what an entry binds ----------------------------------------------------
def names_entry(binding: InputBinding) -> bool:
    """Whether ``binding`` reaches into another entry rather than its own file."""
    return (
        isinstance(binding, RegionBinding | IntegerFromBytes)
        and binding.entry is not None
    )


def can_supply_input(entry: Entry, candidate: Entry) -> bool:
    """Whether ``candidate`` may be named as the source of one of ``entry``'s
    region inputs — the one rule behind the picker and the resolver.

    Only **pixels**, for :func:`~celpix.project.workspace.can_compose`'s reason:
    a tilemap's buffer is a borrowed copy of somebody else's art, and a palette
    or bookmark has no bytes to contribute. Not itself. And **one hop**: a
    candidate whose own inputs name an entry is refused, so a chain has one
    link and a cycle has none — decided from the bindings alone, before anything
    is loaded.
    """
    return (
        candidate is not entry
        and candidate.kind.has_document
        and candidate.content_kind is ContentKind.PIXELS
        and not any(
            names_entry(binding)
            for bindings in candidate.inputs.values()
            for binding in bindings.values()
        )
    )


def with_bindings(
    entry: Entry, plugin_id: str, bindings: Bindings | None
) -> dict[str, Bindings]:
    """``entry.inputs`` with ``plugin_id``'s bindings replaced — a new mapping,
    never a mutation, so an undo command can hold the old one by reference.
    ``None`` or an empty mapping removes the plugin's key."""
    out = {key: dict(value) for key, value in entry.inputs.items()}
    if bindings:
        out[plugin_id] = dict(bindings)
    else:
        out.pop(plugin_id, None)
    return out


def prune_bindings(entry: Entry, registry: Registry) -> dict[str, Bindings]:
    """``entry.inputs`` as a save writes it: only what still means something.

    A **slice** or **tilemap** keeps bindings for the plugins it currently reads
    through and drops the rest — the picker has moved on, and what it left
    behind was kept only so that moving it back cost nothing. A **file** keeps
    every codec's, since each is the preview binding for that codec and the
    preview may be set to any of them. Within a plugin this build has, keys it
    no longer declares go too; a plugin this build lacks keeps every key, because
    nothing here can say which of them it still wants.
    """
    from celpix.project.workspace import EntryKind  # noqa: PLC0415 — circular by nature

    kept: dict[str, Bindings] = {}
    live = (
        set(entry.inputs)
        if entry.kind is EntryKind.FILE
        else {plugin_id_at(entry, stage, registry) for stage in INPUT_STAGES} - {""}
    )
    for plugin_id, bindings in entry.inputs.items():
        if plugin_id not in live:
            continue
        specs = _specs_any_stage(registry, plugin_id)
        if specs is not None:
            keys = {spec.key for spec in specs}
            bindings = {k: v for k, v in bindings.items() if k in keys}
        if bindings:
            kept[plugin_id] = dict(bindings)
    return kept


def _specs_any_stage(
    registry: Registry, plugin_id: str
) -> tuple[InputSpec, ...] | None:
    """The specs of ``plugin_id`` at whichever input stage has it; ``None`` when
    no stage does (a plugin this build lacks)."""
    for stage in INPUT_STAGES:
        try:
            return tuple(registry.plugin(stage, plugin_id).info.inputs)
        except KeyError:
            continue
    return None


# -- resolution --------------------------------------------------------------
def resolve_inputs(
    entry: Entry,
    stage: Stage,
    plugin_id: str,
    registry: Registry,
    workspace: Workspace | None = None,
) -> ResolvedInputs:
    """``entry``'s bindings for ``plugin_id`` at ``stage``, as values.

    Walks the plugin's specs in declaration order, reading each bound source
    once however many inputs share it. A spec nothing binds is a problem when
    required and a default (or an absence) when not; a binding of the wrong
    shape for its spec, a range past its source, a length off the stride, an
    integer outside the spec's range, and a source that is closed, refused by
    the one-hop rule, or unreadable are all problems naming the input.

    Pass ``workspace`` so a parent-file region sees the parent's unsaved edits
    and a named entry is checked for being open; without one the file is the
    source and every named entry is taken on trust.
    """
    specs = input_specs(registry, stage, plugin_id)
    bindings = entry.inputs.get(plugin_id, {})
    values: dict[str, bytes | int] = {}
    problems: list[InputProblem] = []
    sources: dict[int, tuple[bytes, int]] = {}

    def source_of(binding: RegionBinding | IntegerFromBytes) -> tuple[bytes, int]:
        key = id(binding.entry)
        if key not in sources:
            sources[key] = _source_bytes(entry, binding.entry, registry, workspace)
        return sources[key]

    for spec in specs:
        binding = bindings.get(spec.key)
        if binding is None:
            if spec.required:
                problems.append(
                    InputProblem(
                        spec.key,
                        spec.label,
                        f"Nothing is bound to {spec.label}, which this format\n"
                        "needs. Bind it from Inputs… and the entry will be\n"
                        "read again.",
                    )
                )
            elif spec.kind is InputKind.INTEGER and spec.default is not None:
                values[spec.key] = spec.default
            continue
        try:
            values[spec.key] = _resolve_one(spec, binding, source_of)
        except _Unresolved as exc:
            problems.append(InputProblem(spec.key, spec.label, str(exc)))
    return ResolvedInputs(values, tuple(problems))


class _Unresolved(Exception):
    """One binding's failure, carrying the notice detail."""


def _resolve_one(
    spec: InputSpec,
    binding: InputBinding,
    source_of,  # noqa: ANN001 — Callable[[RegionBinding | IntegerFromBytes], tuple[bytes, int]]
) -> bytes | int:
    if spec.kind is InputKind.REGION:
        if not isinstance(binding, RegionBinding):
            raise _Unresolved(
                f"{spec.label} is bound as a number, but this format\n"
                "needs a byte range here."
            )
        stride = max(1, spec.stride)
        if binding.length <= 0:
            raise _Unresolved(f"{spec.label} has no length.")
        if binding.length % stride:
            raise _Unresolved(
                f"{spec.label} is {binding.length} bytes, which is not a\n"
                f"whole number of {stride}-byte {spec.unit or 'element'}s\n"
                f"({binding.length % stride} left over)."
            )
        return _cut(spec, binding, binding.length, source_of)
    if spec.kind is InputKind.INTEGER:
        if isinstance(binding, IntegerFromBytes):
            if not 1 <= binding.width <= _MAX_INT_WIDTH:
                raise _Unresolved(
                    f"{spec.label} reads a {binding.width}-byte number;\n"
                    f"only 1 to {_MAX_INT_WIDTH} bytes can be read."
                )
            raw = _cut(spec, binding, binding.width, source_of)
            value = int.from_bytes(raw, "little" if binding.little_endian else "big")
        elif isinstance(binding, int) and not isinstance(binding, bool):
            value = binding
        else:
            raise _Unresolved(
                f"{spec.label} is bound as a byte range, but this format\n"
                "needs a number here."
            )
        if not spec.minimum <= value <= spec.maximum:
            raise _Unresolved(
                f"{spec.label} is {value}, outside the {spec.minimum}\n"
                f"to {spec.maximum} this format accepts."
            )
        return value
    raise _Unresolved(
        f"{spec.label} is a kind of input ({spec.kind}) this build\n"
        "does not know how to bind."
    )


def _cut(
    spec: InputSpec,
    binding: RegionBinding | IntegerFromBytes,
    length: int,
    source_of,  # noqa: ANN001
) -> bytes:
    """``length`` bytes at the binding's offset in its source, or a problem."""
    try:
        data, base = source_of(binding)
    except _Unresolved:
        raise
    except (KeyError, PipelineError, OSError, ValueError) as exc:
        raise _Unresolved(f"{spec.label} could not be read:\n{exc}") from exc
    # A named entry's offset is into its resolved bytes, 0-based; the file's is
    # absolute, and the buffer may start past a header the container skipped.
    start = binding.offset if binding.entry is not None else binding.offset - base
    if start < 0 or start + length > len(data):
        where = binding.entry.name if binding.entry is not None else "the file"
        raise _Unresolved(
            f"{spec.label} at {binding.offset:#x} for {length} bytes\n"
            f"reaches past the end of {where} ({len(data)} bytes\n"
            f"from {base:#x})."
        )
    return bytes(data[start : start + length])


def _source_bytes(
    entry: Entry,
    source: Entry | None,
    registry: Registry,
    workspace: Workspace | None,
) -> tuple[bytes, int]:
    """The buffer a binding's source supplies and the file offset it starts at.

    ``None`` is the entry's own file, read as a raw slice of it would be. A
    named entry is read as it reads itself — container, reshape, decompressor —
    and its offsets are into that result, so the base comes back as 0.
    """
    from celpix.project import workspace as ws  # noqa: PLC0415 — circular by nature

    if source is not None:
        if not can_supply_input(entry, source):
            raise _Unresolved(
                f"{source.name} cannot supply bytes to an input: only a\n"
                "pixel entry whose own inputs stay inside its file can."
            )
        if workspace is not None and not any(e is source for e in workspace.entries):
            raise _Unresolved(f"{source.name} is not open.")
        return _view_bytes(source, registry, workspace)[0], 0
    owner = entry if entry.kind is ws.EntryKind.FILE else None
    if owner is None and workspace is not None:
        owner = workspace.find_file(entry.path)
    if owner is None:
        # No parent in hand — the files as plain bytes, which is what a slice's
        # parent is as far as anything here can know.
        data, ctx = pipeline.read_region(
            PathwayConfig(
                source=FileRef(entry.paths),
                interpret_preset_id=STAGE_DEFAULT_PRESET[Stage.INTERPRET_PIXEL],
            ),
            registry,
        )
        return data, ctx.get(KEY_SOURCE_OFFSET, 0)
    return _view_bytes(owner, registry, workspace)


def _view_bytes(
    entry: Entry, registry: Registry, workspace: Workspace | None
) -> tuple[bytes, int]:
    """``entry``'s own bytes and their base — the one definition of what it
    shows, except for a tilemap file, whose pixel buffer is borrowed art and
    whose own bytes are its cells."""
    from celpix.project import workspace as ws  # noqa: PLC0415 — circular by nature

    preset = (
        entry.session.pixel_preset_id
        if entry.session is not None
        else STAGE_DEFAULT_PRESET[Stage.INTERPRET_PIXEL]
    )
    if entry.content_kind is ContentKind.TILEMAP:
        data, ctx = pipeline.read_region(
            ws.tilemap_config_for(
                entry,
                entry.tilemap_preset_id
                or STAGE_DEFAULT_PRESET[Stage.INTERPRET_TILEMAP],
                registry,
                workspace,
            ),
            registry,
        )
        return data, ctx.get(KEY_SOURCE_OFFSET, 0)
    return ws.entry_view_bytes(entry, registry, preset, workspace)


def iter_bindings(entry: Entry) -> Iterator[tuple[str, str, InputBinding]]:
    """Every binding on ``entry`` as ``(plugin id, key, binding)``."""
    for plugin_id, bindings in entry.inputs.items():
        for key, binding in bindings.items():
            yield plugin_id, key, binding
