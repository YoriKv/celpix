#!/usr/bin/env python3
"""Render one entry of a ``.celpix`` project to PNG, exactly as the canvas draws it.

A development tool, not part of the shipped app. It exists so a change to the
render can be *looked at* without a human driving the GUI — and so what is looked
at is the real thing rather than a second implementation that could drift.

**It drives celPix itself.** A ``MainWindow`` is built on Qt's ``offscreen``
platform, the project is opened through ``MainWindow._load_project``, the entry is
activated through ``_activate_entry``, and the image handed to the canvas by
``_refresh_view`` is written out. Every subtlety of what the canvas shows —
the view window (columns/rows/offset/nudge), block and 2D arrangements, tile
rearrangement, pinned palette regions, a tilemap's binding to its tile bank, a
fontmap's glyph layout, a sprite object's frame strip, the palette that entry
loaded — is therefore whatever the app itself decided, because it *is* the app
deciding it.

What comes out is the canvas's **image**, so the canvas's own overlays are not in
it: no grid, no selection ring, no tile-ID or palette-row labels, and none of the
neutral surround past the last row of data. Those are painted over the image at
display time; the pixels here are the picture. The project's **pixel aspect** is
applied, though, because that one is not an overlay — it is the shape the pixels
themselves are drawn at (``docs/design/pixel-aspect.md``).

Usage (from the repo root; on WSL export ``UV_PROJECT_ENVIRONMENT=.venv-linux``
first, as with any ``uv`` command here)::

    uv run tools/render_canvas.py sample-projects/alttp/alttp.celpix --list
    uv run tools/render_canvas.py alttp.celpix -e "World map" -o map.png
    uv run tools/render_canvas.py alttp.celpix -e "#11" --entire-file --scale 4

The app's own settings are *read* (the theme, View ▸ Entire File, the pinned-row
toggles) so the picture matches the app as that user has it set up, and nothing
is written back — in particular the project is not added to their recent list.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# The tool lives outside the package, so a plain `uv run tools/render_canvas.py`
# has only this folder on the path. Prepend src/ so `celpix` imports from the
# checkout being worked on rather than from whatever is installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="render_canvas",
        description="Render a celPix project entry to PNG through celPix's own canvas.",
    )
    parser.add_argument("project", type=Path, help="the .celpix project file")
    parser.add_argument(
        "-e",
        "--entry",
        help=(
            "which file or slice to render: its name, 'parent/slice' where a name "
            "is ambiguous, or '#N' for the index shown by --list. Defaults to the "
            "entry the project was saved on."
        ),
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        help="where to write the PNG (default: the entry's export name, in the cwd)",
    )
    parser.add_argument(
        "-l",
        "--list",
        action="store_true",
        help="list the project's entries and exit, without rendering anything",
    )
    parser.add_argument(
        "--entire-file",
        action="store_true",
        help=(
            "show every row the data fills, as View ▸ Entire File does, instead of "
            "the project's view window (no effect on a tilemap, always drawn whole)"
        ),
    )
    parser.add_argument(
        "--scale",
        type=int,
        default=1,
        metavar="N",
        help=(
            "magnify the result N times with nearest-neighbour, for eyeballing. "
            "The default of 1 is the canvas's own pixels, which is what to compare"
        ),
    )
    parser.add_argument(
        "--trust-plugins",
        action="store_true",
        help=(
            "approve any code plugin the project or the user's plugin folder "
            "carries, as clicking Yes in the app would — and remember it the same "
            "way. Without this an untrusted plugin is declined and reported."
        ),
    )
    args = parser.parse_args(argv)
    if args.scale < 1:
        parser.error("--scale must be at least 1")
    return args


def _build_registry(trust_plugins: bool):  # noqa: ANN201 - (Registry, issues) factory
    """The app bootstrap's plugin load, minus the dialogs (``celpix.app.main``).

    The user's real plugin folder and trust store, so a format they added in the
    app is available here and one they already approved loads without asking.
    Nothing is seeded into the folder — that is the app's business, and a render
    should not create files.
    """
    from PySide6.QtCore import QStandardPaths

    from celpix.plugins.discovery import load_user_plugins, project_plugin_dir
    from celpix.plugins.registry import default_registry
    from celpix.plugins.trust import PendingCodePlugin, TrustStore

    data_dir = Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    )
    plugin_dir = data_dir / "plugins"
    trust = TrustStore(str(data_dir / "trusted-plugins.json"))

    def reload_plugins(project_path: str | None = None):  # noqa: ANN202
        registry = default_registry()
        dirs = [str(plugin_dir)] if plugin_dir.is_dir() else []
        project_dir = project_plugin_dir(project_path)
        if project_dir is not None:
            dirs.append(project_dir)

        def confirm(pending: PendingCodePlugin) -> bool:
            if trust_plugins:
                return True
            print(
                f"declined an untrusted code plugin: {pending.path}\n"
                "  approve it in celPix, or re-run with --trust-plugins",
                file=sys.stderr,
            )
            return False

        issues = load_user_plugins(
            registry, dirs, project_dir=project_dir, trust=trust, confirm=confirm
        )
        return registry, issues

    return reload_plugins, str(plugin_dir)


def _silence_modals() -> list[tuple[str, str]]:
    """Route the window's three blocking prompts to stderr instead of a dialog.

    Under the offscreen platform a ``QMessageBox.exec()`` never returns, so any
    one of these would wedge the tool with nothing to blame — the same rule the
    test suite follows (``tests/conftest.py``). Reports are printed, because a
    pipeline failure is exactly what a render needs to say; questions answer
    themselves the safe way: no to anything that would change the project, and
    no relocation walk, so a project whose files have moved reports that rather
    than opening a file picker nobody can see.
    """
    from celpix.ui.main_window import MainWindow, entries

    alerts: list[tuple[str, str]] = []

    def alert(self, message: str, *, title: str = "celPix", detail: str = "") -> None:
        alerts.append((title, message))
        print(f"{title}: {message}", file=sys.stderr)
        if detail:
            print(f"  {detail}", file=sys.stderr)

    MainWindow._alert = alert
    MainWindow._confirm = lambda self, message, **_kwargs: False
    MainWindow._relocate_missing = lambda self, *, prompt_summary=False: None
    # Opening a project records it in the app's recent list. Rendering is not a
    # session, so it leaves no trace in the user's settings.
    entries.remember_recent_project = lambda path: None
    return alerts


def _label(workspace, entry) -> str:  # noqa: ANN001 - Workspace, Entry
    """``parent/child`` for a slice or bookmark, the bare name otherwise."""
    parent = workspace.parent_of(entry)
    return f"{parent.name}/{entry.name}" if parent is not None else entry.name


def _list_entries(workspace) -> None:  # noqa: ANN001 - Workspace
    for index, entry in enumerate(workspace.entries):
        kind = entry.kind.name.lower()
        content = entry.content_kind.name.lower()
        where = f" @0x{entry.slice_offset:X}" if entry.kind.name == "SLICE" else ""
        shown = " *" if entry is workspace.current else ""
        name = _label(workspace, entry)
        print(f"#{index:<3} {kind:<9} {content:<7} {name}{where}{shown}")


def _pick_entry(workspace, wanted: str | None):  # noqa: ANN001, ANN201 - Entry|None
    """The entry ``wanted`` names, or the project's own current entry.

    Raises ``SystemExit`` with something actionable on every miss: a name that
    matches nothing, one that matches several rows, or a row that has no view of
    its own (a bookmark is jumped through and a palette is applied — neither is
    ever what the canvas is showing).
    """
    renderable = [e for e in workspace.entries if e.kind.has_document]
    if wanted is None:
        if workspace.current is None:
            raise SystemExit(
                "this project has no current entry - name one with --entry (see --list)"
            )
        return workspace.current
    if wanted.startswith("#"):
        try:
            entry = workspace.entries[int(wanted[1:])]
        except (ValueError, IndexError):
            raise SystemExit(f"no entry {wanted} - see --list") from None
        if not entry.kind.has_document:
            raise SystemExit(
                f"{_label(workspace, entry)} is a {entry.kind.name.lower()}, which "
                "has no view of its own"
            )
        return entry
    folded = wanted.casefold()
    for match in (
        lambda e: _label(workspace, e) == wanted or e.name == wanted,
        lambda e: (
            _label(workspace, e).casefold() == folded or e.name.casefold() == folded
        ),
        lambda e: folded in _label(workspace, e).casefold(),
    ):
        found = [e for e in renderable if match(e)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            rows = "\n".join(
                f"  #{workspace.entries.index(e)} {_label(workspace, e)}" for e in found
            )
            raise SystemExit(f"{wanted!r} matches several entries:\n{rows}")
    raise SystemExit(f"no entry matching {wanted!r} - see --list")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if not args.project.is_file():
        print(f"no such project: {args.project}", file=sys.stderr)
        return 2
    # Before PySide6 is imported anywhere, as the test suite does it: there is no
    # display to open, and the window is never shown in any case.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from celpix import APP_NAME
    from celpix.core.aspect import SQUARE
    from celpix.core.aspect import scale as aspect_scale
    from celpix.project.workspace import export_basename
    from celpix.ui.export import save_png

    app = QApplication([sys.argv[0]])
    # The app data location - and so the plugin folder and the trust store - is
    # named after the application, so this has to be set before either is asked for.
    app.setApplicationName(APP_NAME)

    reload_plugins, plugin_dir = _build_registry(args.trust_plugins)
    registry, issues = reload_plugins()

    from celpix.ui.main_window import MainWindow

    _silence_modals()
    window = MainWindow(
        registry=registry,
        plugin_dir=plugin_dir,
        plugin_issues=issues,
        reload_plugins=reload_plugins,
    )
    window._load_project(str(args.project))
    workspace = window._workspace
    if not workspace.entries:
        print(f"{args.project} opened with no entries", file=sys.stderr)
        return 1
    if args.list:
        _list_entries(workspace)
        return 0

    entry = _pick_entry(workspace, args.entry)
    window._activate_entry(entry)
    if workspace.current is not entry or window._doc is None:
        # _activate_entry declines to switch when the load failed or the entry's
        # file has moved; the reason has already gone to stderr through _alert.
        print(f"could not show {_label(workspace, entry)}", file=sys.stderr)
        return 1
    if args.entire_file:
        # The toggle's own handler writes the choice to the user's settings, so
        # the action is set with its signal blocked and the view refreshed by hand.
        window._entire_file.blockSignals(True)
        window._entire_file.setChecked(True)
        window._entire_file.blockSignals(False)
        window._refresh_view()

    image = window._canvas._image  # what _refresh_view handed the canvas
    if image.isNull():
        print(f"{_label(workspace, entry)} rendered nothing", file=sys.stderr)
        return 1
    size = f"{image.width()}x{image.height()}"
    # The canvas applies the project's pixel aspect at paint time, so the image it
    # was handed is still square-pixelled. Applying it here is what keeps this
    # file the picture on screen rather than the picture before the last step of
    # drawing it (nearest-neighbour, like the canvas's own magnification).
    aspect = workspace.pixel_aspect or SQUARE
    sx, sy = aspect_scale(aspect)
    sx, sy = sx * args.scale, sy * args.scale
    if (sx, sy) != (1.0, 1.0):
        image = image.scaled(
            round(image.width() * sx),
            round(image.height() * sy),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        size += f" at {aspect[0]}:{aspect[1]}" if aspect != SQUARE else ""
    out = args.out or Path(f"{export_basename(entry)}.png")
    if not save_png(image, str(out)):
        print(f"could not write {out}", file=sys.stderr)
        return 1
    scaled = f" at {args.scale}x" if args.scale > 1 else ""
    print(f"{_label(workspace, entry)}: {size}{scaled} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
