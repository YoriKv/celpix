"""The `font` block — a tile sheet's alphabet.

The odd one out in the schema: it names no plugin, so nothing here can be
"missing". What it can be is *skipped*. A record in `codes` that does not parse
is dropped silently, one line at a time, on the deliberate principle that one
bad line must not cost the user the rest of the table — which is right for a
loader and is exactly why the lines want a linter.

The other trap is the run. `chars` is **positional**: character *i* is what tile
*i* draws, and ``\\u0000`` is a slot the sheet draws nothing for, present only to
keep the letters after it on the right tiles. Editing it as though it were a
label rather than a grid is how a table ends up one tile out.
"""

from __future__ import annotations

from celpix_lint.context import Context, EntryView
from celpix_lint.schema import FONT_KEYS, GLYPH_KEYS, GLYPH_ROLES, is_int

HOLE = "\u0000"


def check(ctx: Context) -> None:
    for entry in ctx.entries:
        if not entry.raw or "font" not in entry.raw:
            continue
        font = entry.raw["font"]
        if not isinstance(font, dict):
            ctx.error(
                "E800",
                f"`font` is {type(font).__name__}, not an object — the entry has no "
                "alphabet",
                pointer=entry.at("font"),
                entry=entry,
                detail="The reader then looks for the superseded `alphabet_preset_id` "
                "form instead, and finding neither leaves the text reading as hex.",
            )
            continue
        _keys(ctx, entry, font)
        _numbers(ctx, entry, font)
        _chars(ctx, entry, font)
        _codes(ctx, entry, font)


def _keys(ctx: Context, entry: EntryView, font: dict) -> None:
    for key in font:
        if key not in FONT_KEYS:
            ctx.warn(
                "W801",
                f"unknown key {key!r} in `font` — the reader ignores it",
                pointer=entry.at("font", key),
                entry=entry,
                detail=f"The block holds: {', '.join(sorted(FONT_KEYS))}.",
            )
    use = font.get("use")
    if "use" in font and not isinstance(use, bool):
        ctx.warn(
            "W802",
            f"`font.use` is {use!r}, not true or false",
            pointer=entry.at("font", "use"),
            entry=entry,
            detail="It is read for truthiness, so it works by accident.",
        )
    if not use and (font.get("chars") or font.get("codes")):
        ctx.warn(
            "W803",
            "the alphabet is not read: `font.use` is off",
            pointer=entry.at("font", "use"),
            entry=entry,
            detail="**Use as Font** is the declaration as well as the gate — an "
            "unticked entry's table is kept but never consulted, so any fontmap drawn "
            "through these tiles reads as hex.",
        )


def _numbers(ctx: Context, entry: EntryView, font: dict) -> None:
    base = font.get("base")
    if "base" in font and not is_int(base):
        ctx.error(
            "E810",
            f"`font.base` is {base!r}, not an integer — it reads as 0",
            pointer=entry.at("font", "base"),
            entry=entry,
            detail="`base` is the code tile 0 draws, and it slides the entire "
            "positional run: every character in `chars` lands on a different code.",
        )
    elif is_int(base) and base < 0:
        # A legal state — the Base code spin dials below the origin — but the
        # low end of the run then sits on codes no stream can hold.
        chars = font.get("chars")
        lost = min(-base, len(chars)) if isinstance(chars, str) else -base
        ctx.warn(
            "W812",
            f"`font.base` is {base}, so the first {lost} character(s) of the run sit "
            "below code 0",
            pointer=entry.at("font", "base"),
            entry=entry,
            detail="Codes below zero are dropped rather than clamped — nothing can be "
            "stored there. Only the run moves with the base; named `codes` do not.",
        )
    for key in ("prepend", "append"):
        value = font.get(key)
        if key in font and (not is_int(value) or value < 0):
            ctx.warn(
                "W811",
                f"`font.{key}` is {value!r} — it must be a row count from 0",
                pointer=entry.at("font", key),
                entry=entry,
                detail="Anything else reads as 0.",
            )


def _chars(ctx: Context, entry: EntryView, font: dict) -> None:
    if "chars" not in font:
        return
    chars = font["chars"]
    if not isinstance(chars, str):
        ctx.error(
            "E820",
            f"`font.chars` is {type(chars).__name__}, not a string — the run is empty",
            pointer=entry.at("font", "chars"),
            entry=entry,
            detail="`chars` is positional: character i is what tile i draws.",
        )
        return
    if chars.endswith(HOLE):
        ctx.info(
            "I821",
            "`font.chars` ends in holes, which say nothing",
            pointer=entry.at("font", "chars"),
            entry=entry,
            detail="A hole (\\u0000) keeps the letters *after* it on the right tiles, "
            "and there are none after the last one. celPix trims them when writing.",
        )


def _codes(ctx: Context, entry: EntryView, font: dict) -> None:
    if "codes" not in font:
        return
    codes = font["codes"]
    if not isinstance(codes, list):
        ctx.error(
            "E830",
            f"`font.codes` is {type(codes).__name__}, not an array — no codes are "
            "named",
            pointer=entry.at("font", "codes"),
            entry=entry,
        )
        return
    seen: dict = {}
    for at, record in enumerate(codes):
        where = entry.at("font", "codes", at)
        code = _one(ctx, entry, at, record, where)
        if code is None:
            continue
        first = seen.setdefault(code, at)
        if first != at:
            ctx.warn(
                "W831",
                f"code {code} is named twice (`codes[{first}]` and `codes[{at}]`)",
                pointer=where,
                entry=entry,
                detail="Both are kept, so which one a reader gets depends on lookup "
                "order rather than on anything written here.",
            )


def _one(
    ctx: Context, entry: EntryView, at: int, record: object, where: str
) -> int | None:
    """One record. Returns its code, or None if the loader will skip the line."""
    if not isinstance(record, dict):
        ctx.error(
            "E832",
            f"`font.codes[{at}]` is not an object and is skipped",
            pointer=where,
            entry=entry,
            detail="A malformed record is dropped one line at a time, so the rest of "
            "the table still loads — and nothing says this one went.",
        )
        return None
    for key in record:
        if key not in GLYPH_KEYS:
            ctx.warn(
                "W833",
                f"unknown key {key!r} in `font.codes[{at}]`",
                pointer=f"{where}/{key}",
                entry=entry,
            )
    if "code" not in record:
        ctx.error(
            "E834",
            f"`font.codes[{at}]` has no `code` and is skipped",
            pointer=where,
            entry=entry,
            detail="`codes` is the absolute half of an alphabet — every record names "
            "the code it is about.",
        )
        return None
    code = record["code"]
    if not is_int(code):
        ctx.error(
            "E835",
            f"`font.codes[{at}].code` is {code!r}, not an integer — the record is "
            "skipped",
            pointer=f"{where}/code",
            entry=entry,
        )
        return None
    if code < 0:
        ctx.error(
            "E836",
            f"`font.codes[{at}].code` is {code}, and no byte reads as a negative code",
            pointer=f"{where}/code",
            entry=entry,
        )
    role = record.get("role")
    if role is not None and role not in GLYPH_ROLES:
        ctx.error(
            "E837",
            f'`font.codes[{at}].role` is {role!r} — it reads as "text"',
            pointer=f"{where}/role",
            entry=entry,
            detail=f"One of: {', '.join(GLYPH_ROLES)}. A command read as a character "
            "is typed into the string verbatim instead of being punctuation.",
        )
        role = "text"
    spells = role in (None, "text", "dict")
    named = record.get("name")
    text = record.get("text")
    if not (isinstance(named, str) and named) and not (isinstance(text, str) and text):
        ctx.error(
            "E838",
            f"`font.codes[{at}]` spells nothing and is skipped",
            pointer=where,
            entry=entry,
            detail="A character carries `text` — what the tile draws. A command "
            "carries `name` — the word a reader types inside [brackets].",
        )
        return None
    if spells and isinstance(named, str) and named:
        # It loads and it spells something, so this is a form question rather
        # than a fault — celPix rewrites it to `text` on the next save. Worth
        # saying only because `name` is spelled down to one word on the way in.
        ctx.info(
            "I839",
            f"`font.codes[{at}]` is a character but carries `name`",
            pointer=f"{where}/name",
            entry=entry,
            detail="A character is `text` alone — what the tile draws. Written as "
            "`name` it is spelled down to one word on the way in, so a spelling with a "
            "space or punctuation in it does not survive.",
        )
    elif not spells and isinstance(text, str) and text and not named:
        ctx.info(
            "I840",
            f"`font.codes[{at}]` is a command with its name under `text`",
            pointer=f"{where}/text",
            entry=entry,
            detail="Read as the older spelling, and converted on re-save. A command's "
            "word belongs under `name`.",
        )
    if "description" in record and not isinstance(record["description"], str):
        ctx.warn(
            "W841",
            f"`font.codes[{at}].description` is not a string",
            pointer=f"{where}/description",
            entry=entry,
            detail="It reaches the insert row's tooltip and never the string itself.",
        )
    if spells and record.get("description"):
        ctx.info(
            "I842",
            f"`font.codes[{at}]` is a character, so its `description` is not shown",
            pointer=f"{where}/description",
            entry=entry,
            detail="A description captions a *command* in the insert row.",
        )
    params = record.get("params")
    if "params" in record and (not is_int(params) or params < 0):
        ctx.warn(
            "W843",
            f"`font.codes[{at}].params` is {params!r} — it reads as 0",
            pointer=f"{where}/params",
            entry=entry,
            detail="`params` is how many cells the command swallows after itself.",
        )
    elif spells and params:
        ctx.warn(
            "W844",
            f"`font.codes[{at}]` is a character but swallows {params} cell(s)",
            pointer=f"{where}/params",
            entry=entry,
            detail="Operands belong to a command; give the record a `role`.",
        )
    return code if is_int(code) else None
