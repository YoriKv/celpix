"""The `.celpix` schema, restated.

This is a deliberate second copy of what ``docs/design/project-format.md`` §4
specifies and ``celpix/project/projectfile.py`` reads. The linter does not
import that reader, for two reasons:

- It has to run where celPix is not installed, which is most of what it is for.
- More importantly, **the reader cannot be used to lint**. It is tolerant by
  design: an unknown ``kind`` becomes ``"file"``, a malformed glyph is skipped,
  a bad ``entry_index`` becomes an unbound map. By the time it hands back an
  ``Entry`` the evidence of the mistake is gone — the very thing a linter is
  supposed to report. Checking the *document* rather than the parse result is
  the only way to see what was written as opposed to what was understood.

The cost of the copy is drift, and it is paid down where it can be: the plugin
ids live in a generated snapshot with a test behind it (``data/registry.json``),
and the enumerations below carry the source they were taken from so a reviewer
can check a row without hunting for it.
"""

from __future__ import annotations

#: The reader's own :data:`celpix.project.projectfile.PROJECT_VERSION`. A file
#: claiming more than this is one a newer celPix wrote.
KNOWN_PROJECT_VERSION = 1

# -- enumerations (celpix.project.workspace, celpix.core) ------------------
#: ``EntryKind`` — how an entry is *bounded*.
KINDS = ("file", "slice", "bookmark", "palette", "composite")
#: The kinds that can be shown, and so can be ``current`` (``EntryKind.has_document``).
KINDS_WITH_DOCUMENT = ("file", "slice", "composite")
#: ``ContentKind`` — what an entry's bytes *are*.
CONTENT_KINDS = ("pixels", "tilemap", "palette")
#: ``PaletteMode``.
PALETTE_MODES = ("default", "file", "offset", "emulator", "custom")
#: ``TileMode``. ``none`` means unbound and the writer never emits it.
TILE_MODES = ("none", "entry")
#: ``SlotFill``. ``ff`` is the default and is omitted when written.
SLOT_FILLS = ("ff", "zero", "keep")
DEFAULT_SLOT_FILL = "ff"
#: ``celpix.core.arrangement.BLOCK_ORDERS``.
BLOCK_ORDERS = ("row", "column", "row-interleave")
#: ``GlyphRole``.
GLYPH_ROLES = ("text", "dict", "break", "control")
#: ``celpix.core.errors.Stage``, as the snapshot keys them.
STAGES = (
    "container",
    "reshape",
    "compression",
    "interpret-pixel",
    "interpret-palette",
    "interpret-tilemap",
)

#: The pass-through / plain-bytes ids each stage omits when writing.
PASSTHROUGH = {
    "container": "container.raw-file",
    "reshape": "reshape.none",
    "compression": "compression.none",
}

# -- key inventories -------------------------------------------------------
TOP_LEVEL_KEYS = frozenset(
    {"version", "current", "entries", "hidden_pixel_presets", "pixel_aspect"}
)

#: Every key an entry may legitimately carry, in any combination the rules
#: below allow. Anything outside this is silently ignored on load, which is
#: worth saying: a misspelled key reads as an absent one.
ENTRY_KEYS = frozenset(
    {
        "kind",
        "name",
        "path",
        "extra_paths",
        "container_id",
        "reshape_id",
        "slice_offset",
        "slice_length",
        "compression_id",
        "slot_fill",
        "offset",
        "palette_preset_id",
        "pieces",
        "content_kind",
        "tilemap_preset_id",
        "tile_source",
        "sprite_size_pair",
        "palette_row_base",
        "font",
        "session",
        "view",
        "palette",
        # Read but never written — the older spelling of `tile_rearrangement`'s
        # owner. Tolerated at the entry level for the same reason the view
        # tolerates the key itself.
        "alphabet_preset_id",
        "alphabet_base",
    }
)

#: Keys only meaningful on certain **kinds**. A key used outside its kinds is
#: not an error the loader reports — it simply never reads it — so the value
#: the author wrote does nothing.
KIND_ONLY = {
    "path": ("file", "slice", "bookmark", "palette"),
    "extra_paths": ("file", "slice", "bookmark", "palette"),
    "container_id": ("file", "palette"),
    "reshape_id": ("file", "slice"),
    "slice_offset": ("slice",),
    "slice_length": ("slice",),
    "compression_id": ("slice",),
    "slot_fill": ("slice",),
    "offset": ("bookmark",),
    "palette_preset_id": ("palette",),
    "pieces": ("composite",),
    # A palette entry is a reference plus how to read it, and carries no
    # session, view or palette of its own.
    "session": ("file", "slice", "bookmark", "composite"),
    "view": ("file", "slice", "bookmark", "composite"),
    "palette": ("file", "slice", "bookmark", "composite"),
}

#: Keys the loader needs, by kind. Absent means the entry is skipped entirely
#: (``path``) or opens on a default that is unlikely to be what was meant.
REQUIRED = {
    "file": ("path",),
    "slice": ("path", "slice_offset"),
    "bookmark": ("path", "offset"),
    "palette": ("path",),
    "composite": ("pieces",),
}

#: Keys read only from a **tilemap** entry, whatever its kind.
TILEMAP_ONLY = ("tilemap_preset_id", "tile_source", "sprite_size_pair")

SESSION_KEYS = frozenset(
    {"pixel_preset_id", "palette_preset_id", "palette_mode", "compression_id"}
)

VIEW_KEYS = frozenset(
    {
        "columns",
        "rows",
        "zoom",
        "subpalette_row",
        "offset",
        "byte_nudge",
        "block_columns",
        "block_rows",
        "block_order",
        "two_dimensional",
        "bitmap_width",
        "pages_across",
        "show_all_frames",
        "transparent_zero",
        "tile_rearrangement",
        "tile_orientations",
        "show_rearranged",
        "palette_regions",
        # Read but never written: the key `tile_rearrangement` was stored under
        # before the type was renamed.
        "tile_map",
    }
)

#: View keys that must be non-negative integers, with the minimum each accepts.
VIEW_INT_MINIMUMS = {
    "columns": 1,
    "rows": 1,
    "subpalette_row": 0,
    "offset": 0,
    "byte_nudge": 0,
    "block_columns": 1,
    "block_rows": 1,
    "bitmap_width": 0,
    "pages_across": 0,
}
VIEW_BOOL_KEYS = (
    "two_dimensional",
    "show_all_frames",
    "transparent_zero",
    "show_rearranged",
)

FONT_KEYS = frozenset({"use", "base", "prepend", "append", "chars", "codes"})
GLYPH_KEYS = frozenset({"code", "text", "name", "role", "description", "params"})
TILE_SOURCE_KEYS = frozenset({"mode", "entry_index", "base_index"})
PIECE_KEYS = frozenset({"entry_index", "offset", "length", "measured"})
PALETTE_KEYS = frozenset({"colors", "path", "offset"})

#: Which key a palette mode needs in the entry's ``palette`` block. ``default``
#: needs none — it *is* the absence of a source.
PALETTE_MODE_NEEDS = {
    "custom": "colors",
    "file": "path",
    "emulator": "path",
    "offset": "offset",
}

#: The stage each id-bearing key is looked up in.
ID_STAGES = {
    "container_id": "container",
    "reshape_id": "reshape",
    "compression_id": "compression",
    "tilemap_preset_id": "interpret-tilemap",
    "palette_preset_id": "interpret-palette",
    "pixel_preset_id": "interpret-pixel",
}

#: Whether a stage's ids name plugins or presets. The interpret stages are
#: picked as presets (a preset names its engine); the byte stages as plugins.
PRESET_STAGES = ("interpret-pixel", "interpret-palette", "interpret-tilemap")


def is_int(value: object) -> bool:
    """A JSON integer — ``bool`` excluded, since it is an ``int`` subclass and a
    stray ``true`` must not read as a count of 1 (the reader's own rule)."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
