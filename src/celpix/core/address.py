"""File-offset ⇄ display-address formats for navigation.

Retro documentation rarely gives positions as flat file offsets: docs write ROM
locations as ``bank:offset`` under the console's memory mapping. Nearly every
such mapping reduces to three numbers — the bank size, the in-bank address the
ROM window starts at, and the number of the file's first bank — so a single
parameterized :class:`BankLayout` covers SNES LoROM/HiROM, GB MBC banking, GBA,
PC Engine, and hand-tuned cases (e.g. SuperFX-style ``$40+`` banks via a custom
first-bank). :data:`BANK_PRESETS` names the common layouts; anything else is
reachable by editing the three settings directly. Qt-free, like all of ``core``.
"""

from __future__ import annotations

from dataclasses import dataclass


def parse_hex(text: str) -> int | None:
    """A flat hex offset, accepting ``0x``/``$`` prefixes or a bare number.

    Like :meth:`BankLayout.parse`, returns ``None`` for invalid text so a
    committing input can revert rather than guess.
    """
    t = text.strip().lower().removeprefix("0x").removeprefix("$")
    if not t:
        return None
    try:
        return int(t, 16)
    except ValueError:
        return None


def format_hex(offset: int) -> str:
    return f"0x{offset:06X}"


@dataclass(frozen=True)
class BankLayout:
    """A ``bank:offset`` mapping described by three numbers.

    File offset ⇄ address: ``bank = bank_base + offset // bank_size`` and
    ``addr = addr_base + offset % bank_size``. Parse is strict — the in-bank
    address must fall inside the bank window (so e.g. LoROM ``$0000–$7FFF``,
    which isn't ROM, is rejected) and the bank must be ≥ ``bank_base``. Mirror
    conventions (FastROM ``$80+``, HiROM ``$40–$7D``) aren't special-cased:
    point ``bank_base`` at the convention your docs use instead.
    """

    bank_size: int  # bytes of ROM per bank
    addr_base: int  # in-bank address of a bank's first byte
    bank_base: int  # bank number of the file's first byte

    @property
    def addr_digits(self) -> int:
        """Hex digits of the largest in-bank address (min 4, the common width)."""
        return max(4, -(-(self.addr_base + self.bank_size - 1).bit_length() // 4))

    def format(self, offset: int) -> str:
        bank, in_bank = divmod(offset, self.bank_size)
        addr = self.addr_base + in_bank
        return f"${self.bank_base + bank:02X}:{addr:0{self.addr_digits}X}"

    def parse(self, text: str) -> int | None:
        """Parse ``$BB:AAAA`` / ``BB:AAAA`` / bare ``BBAAAA`` into a file offset.

        In the bare form the last ``addr_digits`` digits are the in-bank
        address — the convention six-digit console addresses are written in.
        """
        t = text.strip().lower().removeprefix("$")
        if ":" in t:
            bank_s, _, addr_s = t.partition(":")
        else:
            bank_s, addr_s = t[: -self.addr_digits], t[-self.addr_digits :]
        try:
            bank, addr = int(bank_s, 16), int(addr_s, 16)
        except ValueError:
            return None
        if bank < self.bank_base:
            return None
        if not (self.addr_base <= addr < self.addr_base + self.bank_size):
            return None
        return (bank - self.bank_base) * self.bank_size + (addr - self.addr_base)


@dataclass(frozen=True)
class BankPreset:
    id: str
    name: str
    layout: BankLayout


# GB note: the fixed home bank really sits at $0000–$3FFF, so this layout shows
# the first 16 KiB as $00:4000+ — the price of staying three-parameter. All
# switchable banks (the ones docs cite) match convention exactly.
BANK_PRESETS: tuple[BankPreset, ...] = (
    BankPreset("snes-lorom", "SNES LoROM", BankLayout(0x8000, 0x8000, 0x00)),
    BankPreset("snes-hirom", "SNES HiROM", BankLayout(0x10000, 0x0000, 0xC0)),
    BankPreset("gb", "GB banked", BankLayout(0x4000, 0x4000, 0x00)),
    BankPreset("gba", "GBA ROM", BankLayout(0x1000000, 0x000000, 0x08)),
    BankPreset("pce", "PCE HuCard", BankLayout(0x2000, 0x0000, 0x00)),
)
