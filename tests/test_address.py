"""Address-format math: flat hex and the parameterized bank:offset layouts."""

from __future__ import annotations

import pytest

from celpix.core.address import BANK_PRESETS, BankLayout, format_hex, parse_hex


def _layout(preset_id: str) -> BankLayout:
    return next(p.layout for p in BANK_PRESETS if p.id == preset_id)


def test_hex_parse_accepts_prefixed_and_bare() -> None:
    p = parse_hex
    assert p("0x400") == 0x400
    assert p("400") == 0x400
    assert p("0X1fe000") == 0x1FE000
    assert p("$400") == 0x400  # ROM-hacking $ prefix
    assert p("  0x400  ") == 0x400  # surrounding whitespace tolerated
    assert p("") is None
    assert p("nonsense") is None


def test_lorom_maps_32k_banks_at_8000() -> None:
    lo = _layout("snes-lorom")
    assert lo.format(0x000000) == "$00:8000"
    assert lo.format(0x000200) == "$00:8200"
    assert lo.format(0x008000) == "$01:8000"  # next 32K bank
    assert lo.parse("$01:8123") == 0x8123
    assert lo.parse("018123") == 0x8123  # bare six-digit form
    assert lo.parse("$00:7FFF") is None  # $0000-$7FFF isn't ROM under LoROM


def test_hirom_maps_64k_banks_at_c0() -> None:
    hi = _layout("snes-hirom")
    assert hi.format(0x000000) == "$C0:0000"
    assert hi.format(0x012345) == "$C1:2345"
    assert hi.parse("$C1:2345") == 0x12345
    assert hi.parse("c12345") == 0x12345
    assert hi.parse("$41:2345") is None  # below bank_base; use a custom layout


def test_gb_maps_16k_banks_at_4000() -> None:
    gb = _layout("gb")
    assert gb.format(0x4123) == "$01:4123"
    assert gb.parse("$02:5678") == 0x9678
    # Three-parameter limitation: the fixed home bank displays at $4000+ too.
    assert gb.format(0x0123) == "$00:4123"


def test_gba_flat_mapping_uses_six_digit_addresses() -> None:
    gba = _layout("gba")
    assert gba.format(0x123456) == "$08:123456"
    assert gba.parse("08123456") == 0x123456  # bare form, wide address


def test_pce_maps_8k_banks() -> None:
    pce = _layout("pce")
    assert pce.format(0x1ABC) == "$00:1ABC"
    assert pce.format(0x2ABC) == "$01:0ABC"
    assert pce.parse("$01:0ABC") == 0x2ABC


def test_custom_layout_covers_mirror_conventions() -> None:
    # SuperFX-style HiROM docs cite banks $40+; expressed by pointing bank_base
    # there rather than by special-casing mirrors in the parse.
    fx = BankLayout(bank_size=0x10000, addr_base=0x0000, bank_base=0x40)
    assert fx.parse("$41:2345") == 0x12345
    assert fx.format(0x12345) == "$41:2345"


@pytest.mark.parametrize("preset_id", [p.id for p in BANK_PRESETS])
def test_bank_parse_rejects_malformed(preset_id: str) -> None:
    p = _layout(preset_id).parse
    assert p("") is None
    assert p("nonsense") is None
    assert p("$C1:1234567") is None  # address wider than any bank here
    assert p("8000") is None  # no bank digits


@pytest.mark.parametrize("preset_id", [p.id for p in BANK_PRESETS])
def test_bank_round_trip_through_format_and_parse(preset_id: str) -> None:
    layout = _layout(preset_id)
    for offset in (0, 0x200, 0x8000, 0x1FE00, 0x123456):
        assert layout.parse(layout.format(offset)) == offset


def test_hex_round_trip() -> None:
    for offset in (0, 0x200, 0x123456):
        assert parse_hex(format_hex(offset)) == offset
