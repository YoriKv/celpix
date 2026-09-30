"""A tilemap cell's bit layout: which fields a preset places, and where.

The layer :mod:`~celpix.plugins.builtins.tilemap_codec` decodes and encodes
through, kept apart from the codec because it is the part a reader has to hold
in their head to write a preset: the field names and their legend letters, the
``fields`` diagram and the ``side_fields`` word that sits above it, and how wide
a cell comes out. The side array lives here with the rest of the layout rather
than on its own — its masks are read off the same combined placement the cell's
own fields are, and that placement reads the side word's text in turn.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from celpix.core.tilemap import Cell
from celpix.plugins._params import byte_order, integer, one_of
from celpix.plugins.builtins._fields import (
    Field,
    byte_width,
    layout_param,
    limit,
    parse_layout,
    resolve_legend,
)
from celpix.plugins.builtins._lead_codes import (
    LeadScheme,
    lead_scheme,
    wants_lead_scheme,
)
from celpix.plugins.builtins._mask import gather

# Where each Cell attribute is read from and written to. Named once so decode
# and encode cannot drift apart, and so an unknown key in a preset is inert
# rather than half-applied.
# ``drawn`` is set where the position IS drawn, which is how the one format that
# has it stores the bit; a preset placing no ``drawn`` describes a format whose
# every position is drawn, and every other format in hand is that.
_FIELDS = (
    "index",
    "palette",
    "priority",
    "flip_h",
    "flip_v",
    "drawn",
    "terminator",
    "flags",
)


# One field's placement in the word — the chunk masks and (shift, width) pairs
# ``_mask.gather`` / ``_mask.scatter`` take (:mod:`~celpix.plugins.builtins._fields`).
_Field = Field

# Which letter of a cell layout names which field. Overridable per preset, so a
# layout can keep the mnemonics of the note it was copied from.
_LEGEND = {
    "i": "index",
    "p": "palette",
    "o": "priority",
    "h": "flip_h",
    "v": "flip_v",
    "d": "drawn",
    "e": "terminator",
    "f": "flags",
}

# What each field is called on a Cell. The engine's field names follow the
# formats' own notes (``drawn``, ``terminator``); the host's vocabulary is the
# Cell's (``visible``, ``ends_line``), and ``cell_fields`` answers in the
# host's so no caller ever learns this module's spelling. Beside _FIELDS so the
# two lists cannot drift apart — a name in one and not the other is a bug here.
_CELL_ATTR = {
    "index": "index",
    "palette": "palette_row",
    "priority": "priority",
    "flip_h": "flip_h",
    "flip_v": "flip_v",
    "drawn": "visible",
    "terminator": "ends_line",
    "flags": "flags",
}


def _layout_text(params: dict[str, Any]) -> str:
    """The preset's ``fields`` layout, which the engine requires.

    A field's own name given as a key is the older, per-field spelling of the
    same thing, so it is named as not read rather than ignored.
    """
    return layout_param(params, what="the cell's fields", retired=_FIELDS)


def _placements(params: dict[str, Any]) -> dict[str, _Field]:
    """Everything the preset's ``fields`` layout places — and ``side_fields``.

    A side word sits **above** the cell's own, so the two layouts read as one
    diagram, side first: a field split across both joins most significant first,
    exactly as a field split within one word does. That is what lets a side byte
    carry the high bits of an index (a Game Boy Color attribute map's bank bit)
    as naturally as a whole palette row.
    """
    legend = resolve_legend(_LEGEND, params.get("legend"), frozenset(_FIELDS))
    bits = _cell_bytes(params) * 8
    side = _side_text(params)
    if side is None:
        return parse_layout(_layout_text(params), legend, bits)
    return parse_layout(
        f"{side} {_layout_text(params)}", legend, bits + _side_bytes(params) * 8
    )


# -- the side array ----------------------------------------------------------
#
# Bits of a cell stored **outside** the cell: a metatile table whose four tile
# numbers are one array and whose palette rows are a fifth (Final Fantasy II,
# Famicom), or a map kept as a tile array plus a parallel attribute array. The
# preset says where those bits sit in one side word (`side_fields`) and how many
# cells one word covers (`side_cells`); the bytes are the entry's `side_array`
# input, bound to wherever the game keeps them. An input is never written back,
# so the side bits are the array's: `settle_cells` re-derives them after every
# edit, and `encode` refuses a list that disagrees rather than dropping the
# difference.
#
# Which word a cell reads is `side_key`. By **position** (the default) word *k*
# covers cells `k * side_cells` onward, in cell order. By **index** it is the
# word the cell's own index selects, `index // side_cells`: a per-tile or
# per-metatile attribute table, where the colour belongs to the *graphic* and
# follows it wherever the map puts it (Final Fantasy VI's field BG3, whose
# palette row is bits 2-4 of a 64-byte table indexed by metatile number). The
# index there is the field as stored — an ordinal preset's metatile number, not
# a tile — and can have no bits in the side word, since it is what picks it.

#: The preset parameter that turns the side array on, and names its bit layout.
SIDE_FIELDS = "side_fields"
#: The input key the side array is bound under — stored in project files.
SIDE_INPUT = "side_array"

_BOOL_ATTRS = frozenset({"flip_h", "flip_v", "visible", "ends_line"})


def _side_text(params: dict[str, Any]) -> str | None:
    """The preset's ``side_fields`` layout, or None where it has no side array."""
    text = params.get(SIDE_FIELDS)
    if text is None:
        return None
    if not isinstance(text, str):
        raise ValueError(f"side_fields must be a bit layout like fields, got {text!r}")
    return text


def _side_bytes(params: dict[str, Any]) -> int:
    """How wide one side word is, in bytes; 0 with no side array."""
    text = _side_text(params)
    if text is None:
        return 0
    return byte_width(text, "a side word", source="side_fields", empty_ok=False)


def _side_cells(params: dict[str, Any]) -> int:
    """How many consecutive cells one side word covers."""
    return integer(params, "side_cells", 1, low=1)


#: ``side_key``'s two words: which side word a cell reads (the comment above).
_SIDE_KEYS = ("position", "index")


def _by_index(params: dict[str, Any]) -> bool:
    """Whether the side word is picked by the cell's index (``side_key``).

    Checked wherever the side array is read, so a preset that cannot work
    refuses its load rather than drawing something: an index with bits in the
    side word would have to be known to find the word that completes it.
    """
    if one_of(params, "side_key", _SIDE_KEYS, "position") != "index":
        return False
    if "index" in _side_masks(params):
        raise ValueError(
            'side_key = "index" picks the side word by the cell\'s index, so '
            "side_fields cannot place bits of the index itself"
        )
    return True


def _side_words(
    params: dict[str, Any], inputs: Mapping[str, Any] | None
) -> tuple[int, ...]:
    """The bound side array as words, in order; empty where nothing is bound.

    A cell past the array's end reads its side bits as zero — the same answer an
    unbound array gives — and a trailing partial word is dropped like a partial
    cell.
    """
    data = (inputs or {}).get(SIDE_INPUT)
    size = _side_bytes(params)
    if not isinstance(data, bytes | bytearray) or not size:
        return ()
    order = byte_order(params, "endian", "little")
    return tuple(
        int.from_bytes(data[at : at + size], order)
        for at in range(0, len(data) - size + 1, size)
    )


def _side_masks(params: dict[str, Any]) -> dict[str, int]:
    """Per field with bits in the side word: which bits of its value those are.

    A palette row stored whole in a side byte masks all of its bits; an index
    whose top bit is a bank flag in the side byte masks that one bit.
    """
    side = _side_bytes(params) * 8
    if not side:
        return {}
    everything = ((1 << side) - 1) << (_cell_bytes(params) * 8)
    return {
        name: mask
        for name, field in _placements(params).items()
        if (mask := gather(everything, *field))
    }


def _side_only(params: dict[str, Any]) -> frozenset[str]:
    """The fields stored **entirely** in the side array, so nothing to edit."""
    placed = _placements(params)
    return frozenset(
        name
        for name, mask in _side_masks(params).items()
        if mask == limit(placed[name])
    )


def _field(params: dict[str, Any], name: str) -> _Field | None:
    """Field ``name``'s chunks, or None when the format lacks it."""
    return _placements(params).get(name)


def _layout(params: dict[str, Any]) -> dict[str, _Field | None]:
    return {name: _field(params, name) for name in _FIELDS}


def _cell_bytes(params: dict[str, Any]) -> int:
    """How wide one cell is — the layout's own answer where it has one.

    ``bytes`` stays accepted so a preset can state the width plainly, but the
    layout is what decides it: a cell whose two statements disagree is a preset
    to fix rather than one to guess at.
    """
    stated = None if params.get("bytes") is None else integer(params, "bytes")
    size = byte_width(_layout_text(params), "a cell")
    if stated is not None and stated != size:
        raise ValueError(
            f"the layout describes a {size}-byte cell and bytes says {stated}"
        )
    if size < 1:
        raise ValueError(f"cell size must be at least one byte, got {size}")
    return size


def _lead_scheme(params: dict[str, Any]) -> LeadScheme | None:
    """The preset's mixed-width text reading, or None on the fixed-width path.

    The gate is a handful of key lookups, so a preset without the parameters —
    every map there is — pays nothing else on the per-edit encode.
    """
    if not wants_lead_scheme(params):
        return None
    return lead_scheme(
        params,
        _cell_bytes(params),
        {
            name: top
            for name, field in _placements(params).items()
            if (top := limit(field)) is not None
        },
        _side_text(params) is not None,
    )


def _get(word: int, field: _Field | None) -> int:
    return gather(word, *field) if field else 0


# Every field a cell can state, with what reads it off one — the writer's half of
# :meth:`TilemapCodec.decode`, in the order the word is assembled. A table rather
# than eight lines of code so the encode loop can be built from the fields a
# format declares instead of asking after all of them per cell.
_CELL_FIELDS: tuple[tuple[str, Callable[[Cell], int]], ...] = (
    ("index", lambda cell: cell.index),
    ("palette", lambda cell: cell.palette_row),
    ("priority", lambda cell: cell.priority),
    ("flip_h", lambda cell: int(cell.flip_h)),
    ("flip_v", lambda cell: int(cell.flip_v)),
    ("drawn", lambda cell: int(cell.visible)),
    ("terminator", lambda cell: int(cell.ends_line)),
    ("flags", lambda cell: cell.flags),
)
