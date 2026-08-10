"""What one container made of one file, reported rather than loaded.

The model behind Container Info. :func:`inspect_container` runs a pathway's
container stage **alone**, on a context of its own: reshape, decompress and the
codec are not run, and nothing here reaches the entry's document. That isolation
is the whole point — the context a loaded entry carries has every later stage's
contributions mixed into it, and this has to be able to say that the *container*
published a value. It is a fresh read rather than a look at what is loaded, so a
file that has never opened successfully can still be inspected.

Failures are reported, not raised. A missing file, an unregistered container and
a read that threw all land in :attr:`ContainerReport.error` alongside whatever
the plugin managed to publish first, which is usually what explains the failure.
That is the opposite of the hard stop the load takes, and deliberately: this is
reached precisely when an entry did not come out as expected, and a popup that
explains is more use than one that refuses to open.

Running the stages for real is :mod:`celpix.pipeline.pipeline`'s.
"""

from __future__ import annotations

from dataclasses import dataclass

from celpix.core.address import format_hex
from celpix.core.context import (
    KEY_SOURCE_FILES,
    KEY_SOURCE_OFFSET,
    KEY_SOURCE_PATH,
    KEY_TILEMAP_ANIMATIONS,
    PipelineContext,
    hint_info,
)
from celpix.core.errors import Stage
from celpix.core.notices import KEY_NOTICES, Notice, notices
from celpix.pipeline._stage import _acquire
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import ContainerField, ContainerPlugin, ReadSource
from celpix.plugins.registry import Registry


@dataclass(frozen=True)
class ContainerReport:
    """What one container made of one file — the model behind Container Info.

    Three groups, in the order a reader needs them. ``fields`` is what the
    container itself says it read
    (:meth:`~celpix.plugins.base.ContainerPlugin.describe`),
    ``hints`` is what its read published for the stages after it, and ``notices``
    is anything it had to drop, assume or substitute on the way. Both hint and
    notice rows are the read's *own*, not the entry's: they come from a context
    nothing else has touched, so every row here is attributable to this container.

    ``error`` is set when the read raised. The rest of the report still stands —
    a plugin may record notices and *then* fail, and what it managed to publish
    before giving up is usually what explains why.
    """

    container_id: str
    container_name: str
    paths: tuple[str, ...]
    source_size: int
    payload_offset: int
    payload_size: int
    fields: tuple[ContainerField, ...] = ()
    hints: tuple[ContainerField, ...] = ()
    notices: tuple[Notice, ...] = ()
    error: str = ""


def inspect_container(cfg: PathwayConfig, reg: Registry) -> ContainerReport:
    """Run ``cfg``'s container read alone and report what it did with the file.

    The **container stage on its own**, on a context of its own: reshape,
    decompress and the codec are not run, and nothing here reaches the entry's
    document. That isolation is the point — the live context an entry carries has
    every later stage's contributions mixed into it, and this has to be able to
    say "the *container* published this".

    A re-read rather than a look at the loaded document, so a file that has never
    been opened (or never opened successfully) can still be inspected: reaching
    for this is most useful precisely when the entry did not come out as expected.

    Failures are reported, not raised. A missing file, an unregistered container
    and a read that threw all land in :attr:`ContainerReport.error`, because a
    popup that explains what went wrong is more use here than one that refuses to
    open.
    """
    try:
        plugin = reg.plugin(Stage.CONTAINER, cfg.container_id, ContainerPlugin)
    except KeyError as exc:
        # ``args[0]`` rather than ``str``: a KeyError renders its message with the
        # quotes it was raised with, and this one is a sentence.
        return ContainerReport(
            cfg.container_id, cfg.container_id, (), 0, 0, 0, error=str(exc.args[0])
        )
    name = plugin.info.name
    paths = tuple(cfg.source.paths)
    try:
        source, files = _acquire(cfg.source)
    except OSError as exc:
        return ContainerReport(cfg.container_id, name, paths, 0, 0, 0, error=str(exc))
    ctx = PipelineContext()
    # Set as the real load sets them, before the read: a container may consult
    # either while assembling its payload. Both are the *host's* provenance, so
    # they are filtered back out of the hints below — this report is about what
    # the container contributed.
    ctx.set(KEY_SOURCE_PATH, source.path)
    ctx.set(KEY_SOURCE_FILES, files)
    error = ""
    payload = b""
    try:
        payload = plugin.read(source, ctx)
    except Exception as exc:  # noqa: BLE001 - a plugin may raise anything at all
        error = f"{type(exc).__name__}: {exc}"
    return ContainerReport(
        container_id=cfg.container_id,
        container_name=name,
        paths=paths,
        source_size=len(source.data),
        payload_offset=int(ctx.get(KEY_SOURCE_OFFSET, 0) or 0),
        payload_size=len(payload),
        fields=_described_fields(plugin, source, ctx),
        hints=_hint_fields(ctx),
        notices=notices(ctx),
        error=error,
    )


def _described_fields(
    plugin: object, source: ReadSource, ctx: PipelineContext
) -> tuple[ContainerField, ...]:
    """``plugin.describe(...)``, or ``()`` — the method is optional and untrusted.

    Reached by ``getattr`` for the reason every optional plugin method is: a
    container written before it existed, or one with nothing to report, is not
    missing anything. A plugin that raises here (or hands back something that
    isn't a field) loses its rows rather than the popup: the read has already
    succeeded by this point, and a display-only method must not be able to
    retract that.
    """
    describe = getattr(plugin, "describe", None)
    if not callable(describe):
        return ()
    try:
        return tuple(f for f in describe(source, ctx) if isinstance(f, ContainerField))
    except Exception:  # noqa: BLE001 - see the docstring
        return ()


# What the host put on the context itself, which is not this container's doing —
# and the notices, which the report carries whole rather than as name/value rows.
_HOST_KEYS = frozenset({KEY_SOURCE_PATH, KEY_SOURCE_FILES, KEY_NOTICES})


def _hint_fields(ctx: PipelineContext) -> tuple[ContainerField, ...]:
    """Everything the container published, as labelled rows.

    Enumerated off the context rather than asked for, so a **plugin's own** key
    shows up too — labelled with the bare key, which is still evidence that
    something was published and something downstream may be reading it.
    """
    rows = []
    for key, value in sorted(ctx.items().items()):
        if key in _HOST_KEYS:
            continue
        label, detail = hint_info(key)
        # The key itself in the tooltip: it is what a plugin author reads the
        # value with, and the only identifier a hint nobody has labelled has.
        detail = f"{detail}\n\nContext key: {key}" if detail else f"Context key: {key}"
        rows.append(ContainerField(label, _hint_value(key, value), detail))
    return tuple(rows)


def _hint_value(key: str, value: object) -> str:
    """A context value as one short line.

    An offset is quoted in hex, as every address in the app is — the one place a
    key's *meaning* changes how its value reads. Three shapes then read badly
    under ``str``: a side table is interesting for its size rather than its
    contents, a pair of ints is always a width and a height, and a bool is a
    yes/no answer rather than Python.
    """
    if key == KEY_SOURCE_OFFSET and isinstance(value, int):
        return format_hex(value)
    if isinstance(value, (bytes, bytearray)):
        return f"{len(value)} bytes"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and all(isinstance(part, int) for part in value)
    ):
        return f"{value[0]} x {value[1]}"
    if key == KEY_TILEMAP_ANIMATIONS and isinstance(value, tuple):
        live = [sequence for sequence in value if sequence]
        steps = sum(len(sequence.steps) for sequence in live)
        return f"{len(live)} of {len(value)} sequences, {steps} steps"
    # A backstop for anything else that is big: this lands in one cell of a table
    # whose first column sizes to its contents, so a value that reprs to
    # kilobytes does not just read badly, it drags every other row's layout with
    # it. Better a truncated answer than a window shaped by one of them.
    text = str(value)
    return f"{text[:57]}..." if len(text) > 60 else text
