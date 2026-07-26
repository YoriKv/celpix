"""Notices — what a stage wants to tell the user without failing.

A plugin has always had two ways to end: raise, and the whole load fails with a
:class:`~celpix.core.errors.PipelineError`; or return, and say nothing. Plenty of
real cases fall between. An iNES cart with **CHR-RAM** has no CHR ROM at all, so
the reader hands over the bytes after the header — which are program code, not
graphics. A ``.smd`` whose length isn't a whole number of 16 KB blocks has a tail
the deinterleaver cannot place. Neither is a failure; both leave the user looking
at something that isn't what they asked for, with nothing on screen to say why.

A **notice** is that middle ground: the read succeeded, and here is what the
plugin had to assume, drop, or guess along the way.

Notices ride on the :class:`~celpix.core.context.PipelineContext`, which is
already the forward-flowing advisory bag every stage is handed and which the
Document keeps for both pathways. So nothing new is plumbed, every stage can use
it rather than only containers, and — because the context is handed in *before*
the plugin runs — a plugin may record notices and *then* raise, letting a fatal
error carry the observations that led up to it.

Levels are deliberately two. **Warning** means the bytes you are looking at are
not simply the file's contents: something was dropped, substituted or assumed,
and you may be editing the wrong thing. **Info** is a fact worth surfacing that
costs nothing to ignore. A third "error" level would only duplicate raising,
which is how a stage that genuinely cannot proceed reports itself.

Qt-free, like everything in ``core``: a notice is plain data, and how it reaches
the user is the UI's business.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from celpix.core.context import PipelineContext

# tuple[Notice, ...]: everything stages recorded on this pathway, in the order
# they said it. Named here with the notice type rather than alongside the scalar
# keys in `context`, so the whole concept — key, type and both helpers — is one
# import and cannot drift apart.
KEY_NOTICES = "notices"


class NoticeLevel(str, Enum):
    """How much a notice matters. String-valued for readable ids, like Stage."""

    WARNING = "warning"  # the bytes are not simply what the file holds
    INFO = "info"  # worth knowing, safe to ignore


@dataclass(frozen=True)
class Notice:
    """One thing a stage wants the user to know.

    ``summary`` is the single line shown in a list — write it so it still makes
    sense with no context, since that is how it will be read. ``detail`` is the
    fuller explanation; keep it to a few short lines, because the UI shows it in
    a tooltip and Qt will not wrap one
    (``docs/py-qt-reference/pyside6-pitfalls.md``).

    ``source`` names what produced it — a plugin id — so a notice stays
    attributable once several stages have contributed to the same pathway.
    """

    level: NoticeLevel
    summary: str
    detail: str = ""
    source: str = ""

    @property
    def is_warning(self) -> bool:
        return self.level is NoticeLevel.WARNING


def add_notice(
    ctx: PipelineContext,
    level: NoticeLevel,
    summary: str,
    detail: str = "",
    source: str = "",
) -> None:
    """Append a notice to ``ctx``.

    Appending rather than replacing is the point: a pathway runs several stages
    and each may have something to say, so the container's observation and the
    decompressor's must both survive.
    """
    existing = notices(ctx)
    ctx.set(KEY_NOTICES, (*existing, Notice(level, summary, detail, source)))


def warn(
    ctx: PipelineContext, summary: str, detail: str = "", source: str = ""
) -> None:
    """Record a :attr:`NoticeLevel.WARNING` — the common case, spelled short."""
    add_notice(ctx, NoticeLevel.WARNING, summary, detail, source)


def inform(
    ctx: PipelineContext, summary: str, detail: str = "", source: str = ""
) -> None:
    """Record a :attr:`NoticeLevel.INFO`."""
    add_notice(ctx, NoticeLevel.INFO, summary, detail, source)


def notices(ctx: PipelineContext) -> tuple[Notice, ...]:
    """Everything recorded on ``ctx``, oldest first; empty when there is nothing.

    Defensive about the value's shape: the context is an open bag any plugin can
    write to, so a plugin that sets :data:`KEY_NOTICES` to something else must not
    take the UI down with it.
    """
    value = ctx.get(KEY_NOTICES)
    if not isinstance(value, tuple):
        return ()
    return tuple(item for item in value if isinstance(item, Notice))


def worst_level(items: tuple[Notice, ...]) -> NoticeLevel | None:
    """The most severe level present, or None for an empty list."""
    if not items:
        return None
    if any(item.is_warning for item in items):
        return NoticeLevel.WARNING
    return NoticeLevel.INFO
