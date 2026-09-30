"""LZW — the dictionary coder, with its variant stated as inputs.

Every other codec in this folder is a sliding-window LZ, RLE or Huffman scheme.
LZW is a different kind: each code names an entry of a table the decoder builds
as it reads, so nothing else here reads any part of an LZW stream. Its members
differ in a handful of settings, and they are all that separates the GIF image
data, TIFF's LZW and the fixed-width streams games carry:

- **code width**: ``initial_bits`` for the first code after a (re)start, growing
  one bit at a time to ``max_bits``; equal, the width is fixed;
- **bit order**: codes packed from each byte's most significant bit (TIFF) or
  its least (GIF);
- **literals**: codes ``0 .. 2**literal_bits - 1`` are single output bytes
  (8 almost everywhere; GIF uses its minimum code size);
- **special codes**, each optional: a *clear* code that resets the table and
  the width, and an *end* code that stops the stream;
- **early change**: the width grows one code earlier than the table needs it
  to, as TIFF does.

Everything else follows from those. The first free code is the lowest code
past the literals that is not special; entries are allocated upward while the
next code is below ``2**max_bits`` and not special, and a full table
**freezes** — codes keep naming the entries already made — until a clear code
arrives. Reading a code uses the width the table's size calls for *before*
that code adds its entry, which is where the one-entry lag between encoder
and decoder is settled.

Without an end code the stream has no end of its own: the decode runs to the
end of the data, and the slice's length is the extent. Pad bits that happen to
make up a whole code there decode as one more code.

Not covered: the Unix ``.Z`` format's habit of skipping to the end of a group of
eight codes when the width changes or the table clears, and schemes that reset
the table on their own when it fills, without a clear code.

Format detail and provenance are in
``docs/graphics-formats-reference/implementation-guide.md`` §7.
"""

from __future__ import annotations

from dataclasses import dataclass

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    KEY_INPUTS,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import InputKind, InputSpec, PluginInfo
from celpix.plugins.builtins._lz import corrupt

MAX_WIDTH = 16

INPUT_INITIAL_BITS = "initial_bits"
INPUT_MAX_BITS = "max_bits"
INPUT_LITERAL_BITS = "literal_bits"
INPUT_BIT_ORDER = "bit_order"
ORDER_MSB, ORDER_LSB = "msb", "lsb"
INPUT_CLEAR_CODE = "clear_code"
INPUT_END_CODE = "end_code"
INPUT_EARLY_CHANGE = "early_change"


@dataclass(frozen=True)
class Params:
    """One LZW variant. The defaults are the textbook 9-to-12-bit coder."""

    initial_bits: int = 9
    max_bits: int = 12
    literal_bits: int = 8
    lsb_first: bool = False
    clear_code: int | None = None
    end_code: int | None = None
    early_change: bool = False

    def __post_init__(self) -> None:
        literals = 1 << self.literal_bits
        start = self.initial_bits
        if not 1 <= self.literal_bits <= 8:
            raise ValueError("literal bits must be 1-8: a literal is one output byte")
        if not start <= self.max_bits <= MAX_WIDTH:
            raise ValueError(
                f"widths {start}-{self.max_bits} must rise within 1-{MAX_WIDTH}"
            )
        if (1 << start) <= max((literals - 1, *self.specials)):
            raise ValueError(
                f"{start}-bit codes cannot reach every literal and special code"
            )
        for code in self.specials:
            if not literals <= code < (1 << self.max_bits):
                raise ValueError(
                    f"special code {code:#x} overlaps the literals or exceeds the width"
                )
        if self.clear_code is not None and self.clear_code == self.end_code:
            raise ValueError("the clear and end codes must differ")

    @property
    def specials(self) -> tuple[int, ...]:
        return tuple(c for c in (self.clear_code, self.end_code) if c is not None)

    @property
    def first(self) -> int:
        """The first code a new entry takes."""
        code = 1 << self.literal_bits
        while code in self.specials:
            code += 1
        return code

    def allocatable(self, code: int) -> bool:
        return code < (1 << self.max_bits) and code not in self.specials

    def width(self, next_code: int) -> int:
        """Bits in the next code, for a table whose next free entry is ``next_code``."""
        need = (next_code + self.early_change).bit_length()
        return min(self.max_bits, max(self.initial_bits, need))


_fail = corrupt("LZW")


class _Reader:
    def __init__(self, data: bytes, lsb_first: bool) -> None:
        self.data = data
        self.lsb = lsb_first
        self.bit = 0
        self.total = len(data) * 8

    def read(self, width: int) -> int | None:
        if self.bit + width > self.total:
            return None
        at, shift = divmod(self.bit, 8)
        span = (shift + width + 7) // 8
        chunk = self.data[at : at + span]
        if self.lsb:
            value = (int.from_bytes(chunk, "little") >> shift) & ((1 << width) - 1)
        else:
            value = (int.from_bytes(chunk, "big") >> (span * 8 - shift - width)) & (
                (1 << width) - 1
            )
        self.bit += width
        return value


def decompress(
    data: bytes, params: Params, *, partial: bool = False
) -> tuple[bytes, int, bool]:
    """Unpack one stream from ``data[0]``: ``(output, consumed, complete)``.

    ``consumed`` runs to the byte holding the end code's last bit, or to the
    end of ``data`` when there is no end code (``complete`` is then False: the
    slice, not the stream, says where it stops). With ``partial``, data that
    ends before the end code yields what was decoded so far instead of raising.
    """
    reader = _Reader(data, params.lsb_first)
    first = params.first
    table: list[bytes] = [bytes((i,)) for i in range(1 << params.literal_bits)]
    table += [b""] * (first - len(table))  # the special codes' slots
    out = bytearray()
    old: bytes | None = None
    specials = params.specials
    while True:
        code = reader.read(params.width(len(table)))
        if code is None:
            break
        if code in specials:
            if code == params.end_code:
                return bytes(out), (reader.bit + 7) // 8, True
            del table[first:]  # the clear code
            old = None
            continue
        if old is None:
            if code >= first:
                raise _fail(f"code {code:#x} names an entry before any exist")
            entry = table[code]
        elif code < len(table) and table[code]:  # a special's slot is empty
            entry = table[code]
            if params.allocatable(len(table)):
                table.append(old + entry[:1])
        elif code == len(table) and params.allocatable(len(table)):
            entry = old + old[:1]  # the entry this very code defines
            table.append(entry)
        else:
            raise _fail(f"code {code:#x} is ahead of the table ({len(table):#x})")
        out += entry
        old = entry
    if params.end_code is not None and not partial:
        raise _fail(f"no end code before the data ran out ({len(out):,} bytes decoded)")
    return bytes(out), len(data), False


def compress(data: bytes, params: Params) -> bytes:
    """Pack ``data`` as one stream: greedy longest match, clearing a full table
    when there is a clear code (and opening with one) and freezing it when not."""
    literals = 1 << params.literal_bits
    if any(b >= literals for b in data):
        raise ValueError(f"a byte is past the {params.literal_bits}-bit literals")
    first = params.first
    out = bytearray()
    acc = held = 0

    def put(code: int, width: int) -> None:
        nonlocal acc, held
        if params.lsb_first:
            acc |= code << held
            held += width
            while held >= 8:
                out.append(acc & 0xFF)
                acc >>= 8
                held -= 8
        else:
            acc = (acc << width) | code
            held += width
            while held >= 8:
                held -= 8
                out.append((acc >> held) & 0xFF)
                acc &= (1 << held) - 1

    # the decoder's table size, one entry behind ours: it adds an entry on
    # reading a code after the first, and the width of each code is its call
    table: dict[bytes, int] = {}
    enc_next = dec_next = first
    dec_primed = False

    def emit(code: int) -> None:
        nonlocal dec_next, dec_primed
        put(code, params.width(dec_next))
        if dec_primed and params.allocatable(dec_next):
            dec_next += 1
        dec_primed = True

    def restart(clear: int) -> None:
        nonlocal enc_next, dec_next, dec_primed
        put(clear, params.width(dec_next))
        table.clear()
        enc_next = dec_next = first
        dec_primed = False

    if params.clear_code is not None:
        restart(params.clear_code)
    if data:
        w = data[:1]
        for b in data[1:]:
            wc = w + bytes((b,))
            if wc in table:
                w = wc
                continue
            emit(table[w] if len(w) > 1 else w[0])
            if params.allocatable(enc_next):
                table[wc] = enc_next
                enc_next += 1
            elif params.clear_code is not None:
                restart(params.clear_code)
            w = bytes((b,))
        emit(table[w] if len(w) > 1 else w[0])
    if params.end_code is not None:
        put(params.end_code, params.width(dec_next))
    if held:
        out.append((acc & 0xFF) if params.lsb_first else (acc << (8 - held)) & 0xFF)
    return bytes(out)


def params_from(inputs: dict) -> Params:
    """The variant an entry's bound inputs describe."""
    return Params(
        initial_bits=inputs.get(INPUT_INITIAL_BITS, Params.initial_bits),
        max_bits=inputs.get(INPUT_MAX_BITS, Params.max_bits),
        literal_bits=inputs.get(INPUT_LITERAL_BITS, Params.literal_bits),
        lsb_first=inputs.get(INPUT_BIT_ORDER) == ORDER_LSB,
        clear_code=inputs.get(INPUT_CLEAR_CODE),
        end_code=inputs.get(INPUT_END_CODE),
        early_change=bool(inputs.get(INPUT_EARLY_CHANGE, False)),
    )


def _code_input(key: str, label: str, tooltip: str) -> InputSpec:
    return InputSpec(
        key,
        label,
        InputKind.INTEGER,
        required=False,
        minimum=1,
        maximum=(1 << MAX_WIDTH) - 1,
        tooltip=tooltip,
    )


class LzwCompression:
    info = PluginInfo(
        id="compression.lzw",
        name="LZW (code width, bit order and special codes as inputs)",
        stage=Stage.COMPRESSION,
        # The answer for unbound inputs; the real one is the end code's
        # (``delimits_itself``), since a variant without one has no end of its
        # own — the slice sets the extent and a scan finds nothing to complete.
        self_delimiting=True,
        category="Generic",
        inputs=(
            InputSpec(
                INPUT_INITIAL_BITS,
                "Initial code width",
                InputKind.INTEGER,
                required=False,
                default=Params.initial_bits,
                minimum=2,
                maximum=MAX_WIDTH,
                unit="bits",
                tooltip=(
                    "Bits in the first code after a start or a clear.\n"
                    "GIF: its minimum code size + 1. TIFF: 9."
                ),
            ),
            InputSpec(
                INPUT_MAX_BITS,
                "Maximum code width",
                InputKind.INTEGER,
                required=False,
                default=Params.max_bits,
                minimum=2,
                maximum=MAX_WIDTH,
                unit="bits",
                tooltip=(
                    "Widest a code grows. The table holds at most\n"
                    "2^width codes, then freezes until a clear code.\n"
                    "GIF and TIFF: 12."
                ),
            ),
            InputSpec(
                INPUT_LITERAL_BITS,
                "Literal bits",
                InputKind.INTEGER,
                required=False,
                default=Params.literal_bits,
                minimum=1,
                maximum=8,
                unit="bits",
                tooltip=(
                    "Codes below 2^bits are single output bytes.\n"
                    "8 almost everywhere; GIF uses its minimum\n"
                    "code size (2-8)."
                ),
            ),
            InputSpec(
                INPUT_BIT_ORDER,
                "Bit order",
                InputKind.CHOICE,
                required=False,
                options=((ORDER_MSB, "MSB first"), (ORDER_LSB, "LSB first")),
                tooltip="GIF: LSB first. TIFF: MSB first.",
            ),
            _code_input(
                INPUT_CLEAR_CODE,
                "Clear code",
                "Code that empties the table and resets the width.\n"
                "Unbound: none. GIF: 2^literal bits. TIFF: 256.",
            ),
            _code_input(
                INPUT_END_CODE,
                "End code",
                "Code that ends the stream. Unbound: none, and the\n"
                "slice length is the extent.\n"
                "GIF: 2^literal bits + 1. TIFF: 257.",
            ),
            InputSpec(
                INPUT_EARLY_CHANGE,
                "Early change",
                InputKind.FLAG,
                required=False,
                tooltip=("Grow the width one code early, as TIFF's LZW does."),
            ),
        ),
    )

    @staticmethod
    def delimits_itself(inputs: dict) -> bool:
        """Only a variant with an end code ends where its bytes say
        (:func:`celpix.plugins.base.self_delimiting`)."""
        return params_from(inputs).end_code is not None

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        params = params_from(ctx.get(KEY_INPUTS) or {})
        out, consumed, complete = decompress(
            data, params, partial=bool(ctx.get(KEY_DECOMPRESS_PARTIAL))
        )
        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        ctx.set(KEY_DECOMPRESS_COMPLETE, complete)
        return out

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return compress(data, params_from(ctx.get(KEY_INPUTS) or {}))
