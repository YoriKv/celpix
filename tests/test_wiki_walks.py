"""Walk every tutorial page of the wiki through the app, as its screenshots are made.

The scripts under ``local-tools/wiki/`` drive the real main window offscreen
through its own menus, pickers and dialogs — the gestures each wiki page
describes — and grab a picture at every step (``local-tools/wiki/README.md``).
A script ends the run when a menu label it was given is gone, a dialog it did
not arrange for comes up, a dialog refuses OK, or a slot raises: every one of
those is a change a reader of that page would meet. So each page is run here,
as a subprocess, and has to finish and produce exactly the images its page
references. It is the one test that crosses every surface the tutorials touch
in the order a user does, which no suite of one window at a time can.

Seconds per page, and the walks need what the README lists — the Super Mario
World ROM beside the checkout, the private ``sample-projects/``, Windows' fonts
— so the module is in the ``samples`` marker, out of the inner loop and
skipped where those are missing.

The pages after Getting Started open the project that page leaves behind
(``tmp/wiki-tutorial/``), so that walk is a module fixture the others ask for.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.samples

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "local-tools" / "wiki"
PAGES = ROOT / "wiki"
SAMPLES = ROOT / "sample-projects"
ROM = Path(os.environ.get("SMW_ROM") or ROOT.parent / "Super Mario World (USA).sfc")
FONTS = Path("/mnt/c/Windows/Fonts")

# Script stem, page, and the private sample folders the walk opens.
WALKS = [
    ("vram_windows", "VRAM-Windows", ()),
    ("palette_rows", "Pinned-Palette-Rows", ()),
    ("plugin_inputs", "Plugin-Inputs", ("smw",)),
    ("compress_reshape", "Compress-and-Reshape", ("smw",)),
    ("text_and_fonts", "Text-and-Fonts", ("smw",)),
    ("sprites", "Sprites", ("tilemaps",)),
]


def _needs(*samples: str) -> None:
    for what, there in (
        ("the wiki scripts", SCRIPTS.is_dir()),
        ("the SMW ROM", ROM.is_file()),
        ("Windows' fonts", FONTS.is_dir()),
        *((f"sample-projects/{name}", (SAMPLES / name).is_dir()) for name in samples),
    ):
        if not there:
            pytest.skip(f"{what} not present - see local-tools/wiki/README.md")


def _walk(stem: str, out: Path) -> set[str]:
    """Run one page's script with its grabs sent to ``out``; the images it made."""
    run = subprocess.run(
        [sys.executable, str(SCRIPTS / f"{stem}.py")],
        cwd=ROOT,
        env={**os.environ, "WIKI_SHOTS_OUT": str(out)},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert run.returncode == 0, (
        f"{stem}.py stopped (exit {run.returncode}):\n{run.stderr[-3000:]}"
    )
    return {p.name for p in out.glob("*.png")}


def _referenced(page: str) -> set[str]:
    text = (PAGES / f"{page}.md").read_text(encoding="utf-8")
    return set(re.findall(r"images/([A-Za-z0-9_.-]+\.png)", text))


@pytest.fixture(scope="module")
def tutorial(tmp_path_factory):
    """Getting Started's walk, run once: the pictures it made, and the project
    under ``tmp/wiki-tutorial/`` every later page opens."""
    _needs("smw")  # it takes a save state from the sample project
    out = tmp_path_factory.mktemp("getting-started")
    return _walk("getting_started", out)


def test_getting_started_walks_through_and_makes_every_picture_its_page_shows(
    tutorial,
) -> None:
    assert tutorial == _referenced("Getting-Started")


@pytest.mark.parametrize(("stem", "page", "samples"), WALKS, ids=[w[0] for w in WALKS])
def test_a_page_walks_through_and_makes_every_picture_it_shows(
    tutorial, tmp_path, stem, page, samples
) -> None:
    _needs(*samples)
    assert _walk(stem, tmp_path) == _referenced(page)


def test_the_sample_projects_page_walks_through_every_public_sample(
    tutorial, tmp_path
) -> None:
    # Each public sample is opened beside the cart from its private twin, and
    # its picture is the one embedded from the repo rather than from
    # ``wiki/images/`` (``sample-<folder>`` here, moved to ``screenshot.png``
    # beside the sample on a real run), so the page's references say nothing
    # about this walk; one picture per public sample does.
    text = (PAGES / "Sample-Projects.md").read_text(encoding="utf-8")
    names = set(re.findall(r"sample-project-public/([^/)]+)/screenshot\.png", text))
    assert names, "the page shows no sample"
    _needs(*names)
    assert _walk("sample_projects", tmp_path) == {f"sample-{n}.png" for n in names}
