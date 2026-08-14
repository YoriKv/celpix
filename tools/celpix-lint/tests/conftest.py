"""Fixtures for the linter's tests.

The tests write real project files to a temp directory rather than feeding
dicts to the checks, because half of what is being tested is the reading —
path resolution, JSON tolerance, the file-size arithmetic. A check that passes
on a hand-built dict and fails on a file it had to parse has not been tested.
"""

from __future__ import annotations

import json

import pytest

from celpix_lint.known import KnownIds
from celpix_lint.linter import lint


@pytest.fixture
def ids() -> KnownIds:
    """A small stand-in registry, so the tests do not move when celPix ships a
    new preset. The real snapshot is checked separately, by celPix's own suite."""
    return KnownIds(
        plugins={
            "container": {
                "container.raw-file": ["pixels", "tilemap", "palette"],
                "container.ines": ["pixels", "tilemap"],
                "container.scgcad-col": ["palette"],
            },
            "reshape": {"reshape.none": [], "reshape.split-planes-2": []},
            "compression": {"compression.none": [], "compression.lz2": []},
        },
        presets={
            "interpret-pixel": {"preset.pixel.snes-4bpp", "preset.pixel.nes-2bpp"},
            "interpret-palette": {"preset.palette.bgr555", "preset.palette.bgr444"},
            "interpret-tilemap": {"preset.tilemap.snes-bg"},
        },
        renamed={"preset.palette.r4g4b4": "preset.palette.bgr444"},
        source="test registry",
        authoritative=True,
    )


@pytest.fixture
def project(tmp_path, ids):
    """Write a project and lint it — returns the codes it produced.

    ``write(document, files={"rom.sfc": 4096})`` creates the named files at the
    given sizes beside the project, so the on-disk checks have something real to
    measure.
    """

    def write(document: dict, files: dict | None = None, **kwargs) -> list:
        for name, size in (files or {}).items():
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x00" * size)
        path = tmp_path / "test.celpix"
        path.write_text(json.dumps(document), encoding="utf-8")
        report = lint(str(path), ids, **kwargs)
        return [d.code for d in report.diagnostics]

    return write


@pytest.fixture
def entry():
    """A minimal, clean file entry the tests bend one key at a time."""

    def build(**overrides) -> dict:
        base = {
            "kind": "file",
            "name": "rom",
            "path": "rom.sfc",
            "session": {
                "pixel_preset_id": "preset.pixel.snes-4bpp",
                "palette_preset_id": "preset.palette.bgr555",
                "palette_mode": "default",
                "compression_id": "compression.none",
            },
        }
        base.update(overrides)
        return base

    return build
