"""Ids a plugin or preset used to be called, and what it is called now.

A plugin id is a **compatibility surface**, not an implementation detail. Three
things outside this codebase hold one: a saved project names the container,
compression, reshape and interpret presets each of its entries was opened with; a
user's own preset TOML names the ``engine_id`` it is parameters for; and local
preferences remember a few. Rename a plugin without a forwarding address and all
three silently stop resolving — a project opens with its formats reset to
pass-through, which reads as data loss even though the bytes are untouched.

So a rename is a **two-part change**: the new id, and a row here. Nothing else
has to know, because every lookup goes through :meth:`Registry.plugin` /
:meth:`Registry.preset`, and those consult this table when a name misses.

**Aliases are permanent.** They cost one dict entry and a failed lookup that was
already failing; retiring one only breaks files that still work today. Add rows,
do not prune them.

Two rules keep the table honest, both checked by the tests:

- **No alias may name an id that exists.** A live plugin shadowing an alias means
  the alias never fires, and the same string meaning two things at once is how a
  rename quietly half-lands.
- **Every target must resolve, and to a live id rather than to another retired
  one.** Rename something twice and the first row has to be re-pointed at the
  final name, not left aimed at the middle one. Both halves are one rule: a
  target that is itself a key here still resolves — :func:`current_id` walks the
  chain — but it reads as a pattern to copy, and it is the one shape a
  single-hop reader of this dict would get wrong. So the table stays **flat**,
  and the walk below is a backstop rather than the mechanism.

Project files are also rewritten as they are re-saved
(:func:`celpix.project.projectfile.current_ids`), so a project touched after an
upgrade stops depending on this table. One that is never re-saved keeps working
through it indefinitely, which is the point.
"""

from __future__ import annotations

# old id -> the id that behaviour has now. Grouped by the change that caused it,
# newest last, because the reason is the only thing that makes a row reviewable.
RENAMED: dict[str, str] = {
    # v0.4.4 — interpret engines gained the pathway every other stage's ids
    # already carried, so `codec.packed` no longer has to be told apart from
    # `codec.tilemap-packed` by remembering which one omits its stage.
    "codec.planar": "codec.pixel.planar",
    "codec.packed": "codec.pixel.packed",
    "codec.nibble-planar": "codec.pixel.nibble-planar",
    "codec.linear-bespoke": "codec.pixel.packed-straddling",
    "codec.direct-color": "codec.pixel.direct-color",
    "codec.color-mask": "codec.palette.mask",
    "codec.color-indexed": "codec.palette.indexed",
    "codec.tilemap-packed": "codec.tilemap.packed",
    # The three sprite records were renamed a second time in v0.5.10 (below), so
    # these name what ended up with the behaviour rather than what v0.4.4 called
    # it. Kept in this group because the *reason* they are here is still v0.4.4's
    # — a row is reviewable by the change that retired its left-hand side.
    "codec.scgcad-object": "format.tilemap.scgcad-object",
    "codec.scgcad-obz": "format.tilemap.scgcad-obz",
    "codec.ys-spr": "format.tilemap.ys-spr",
    # v0.4.4 — seven palette ids named a channel order their own masks
    # contradicted. Their filenames and display names already agreed with the
    # masks and only the id dissented, so the id is what moved: `argb8888` is
    # ABGR in value order and R,G,B,A in bytes, which is what `rgba8888.toml`
    # and "RGBA8888 (bytes R,G,B,A)" had said all along. Two of these were not
    # a spelling difference but a wrong answer.
    "preset.palette.argb8888": "preset.palette.rgba8888",
    "preset.palette.argb8888-be": "preset.palette.abgr8888",
    "preset.palette.r3g3b3": "preset.palette.grb333",
    "preset.palette.r3g3b3-be": "preset.palette.grb333-be",
    "preset.palette.r4g4b4": "preset.palette.bgr444",
    "preset.palette.r4g4b4-be": "preset.palette.bgr444-be",
    "preset.palette.r5g5b5-split-be": "preset.palette.rgb555-split-be",
    # v0.4.4 — the pixel presets that name no platform read
    # `<bpp>-<layout>[-<size>][-<qualifier>]`, the shape `3bpp-planar` and
    # `4bpp-linear-16x16-lsb` already had. These seven led with something else,
    # spelled a tile size two ways, or called `codec.pixel.packed` "chunky"
    # where its other ten presets call it "linear".
    "preset.pixel.chunky-8bpp": "preset.pixel.8bpp-linear",
    "preset.pixel.chunky-8bpp-wide": "preset.pixel.8bpp-linear-256x128",
    "preset.pixel.generic-3bpp": "preset.pixel.3bpp-planar-separate",
    "preset.pixel.1bpp8": "preset.pixel.1bpp-8x8",
    "preset.pixel.1bpp16": "preset.pixel.1bpp-16x16",
    "preset.pixel.1bpp16-ff5": "preset.pixel.1bpp-16x12-ff5",
    "preset.pixel.1bpp16-ff6": "preset.pixel.1bpp-16x11-ff6",
    # v0.5.10 — four bespoke codecs moved from the engine tier to the format
    # tier, which the id spells: an engine is parameterised and serves many
    # presets, a format is one codec written in code
    # (`docs/design/plugin-system.md`). Each was an engine with exactly one
    # preset, so both ids forward to the single name that replaced the pair.
    #
    # The `codec.` half forwards a name and nothing more. A format takes no
    # parameters, so a user's own preset written for one of these engines cannot
    # be *served* by what the name now reaches — it is refused at load instead,
    # loudly, rather than decoded with the format's answers in place of its own
    # (`discovery.check_engine_takes_params`).
    "preset.tilemap.scgcad-object": "format.tilemap.scgcad-object",
    "preset.tilemap.scgcad-obz": "format.tilemap.scgcad-obz",
    "preset.tilemap.ys-spr": "format.tilemap.ys-spr",
    "codec.tilemap.scgcad-object": "format.tilemap.scgcad-object",
    "codec.tilemap.scgcad-obz": "format.tilemap.scgcad-obz",
    "codec.tilemap.ys-spr": "format.tilemap.ys-spr",
    # v0.5.16 — "bespoke" named the engine tier this codec sits in rather than
    # anything about the bytes, and it was the only word telling it apart from
    # `codec.pixel.packed`. The real difference is the depth: a 3- or 6-bit field
    # does not divide a byte, so pixels straddle one. The row above for
    # `codec.linear-bespoke` was re-pointed here in the same change, the table
    # naming live ids only.
    "codec.pixel.linear-bespoke": "codec.pixel.packed-straddling",
    # v0.5.16 — "CG2" and "SG" were another editor's format codes, not PC Engine
    # vocabulary, so neither told a reader what the bytes do nor could be searched
    # for. Named by mechanism and by use instead.
    "preset.pixel.pce-cg2-4bpp": "preset.pixel.pce-4bpp-flat-planes",
    "preset.pixel.pce-sg-4bpp": "preset.pixel.pce-4bpp-sprite",
}


def current_id(plugin_id: str) -> str:
    """The id ``plugin_id`` is known by now, following a chain of renames.

    Unknown ids come back unchanged: this answers "has this been renamed?", not
    "does this exist?". A plugin the registry genuinely hasn't got still has to
    degrade to its stage's pass-through, and telling the two apart is the
    caller's job.

    The table is kept **flat** — every row names a live id, which is checked —
    so in practice this walks one hop. It walks at all, and with a seen-set
    rather than trusting the table to terminate, because the table is hand-edited
    and a build between the wrong edit and the test that catches it should open
    files rather than hang on a lookup.
    """
    seen = {plugin_id}
    while (nxt := RENAMED.get(plugin_id)) is not None:
        if nxt in seen:
            # A cycle is a bug in the table, not in the file being opened, so the
            # file still opens — with the last id the walk reached.
            break
        plugin_id = nxt
        seen.add(nxt)
    return plugin_id
