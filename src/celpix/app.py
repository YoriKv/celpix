"""Application bootstrap: the command line, the QApplication, the main window."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QMessageBox

from celpix import APP_NAME, __version__, resources
from celpix.core.capabilities import ContentKind
from celpix.plugins.discovery import (
    FOLDER_STAGE,
    load_user_plugins,
    project_plugin_dir,
    seed_examples,
)
from celpix.plugins.registry import default_registry
from celpix.plugins.trust import PendingCodePlugin, TrustStore
from celpix.project.projectfile import PROJECT_EXTENSION
from celpix.ui.main_window import MainWindow
from celpix.ui.main_window.palette_source import PALETTE_EXTENSIONS
from celpix.ui.theme import THEME_KEY, Theme, apply_theme
from celpix.ui.widgets import load_enum_setting

# The application name is the *only* identity set on the QApplication (no
# organization name): QStandardPaths appends both organizationName and
# applicationName, so setting an org equal to the app would nest the data dir as
# celPix/celPix. celPix is a single app with no separate org — which is also why
# the preference store names its organization explicitly rather than relying on
# this (:func:`celpix.ui.widgets.settings`).


def _app_data_dir() -> Path:
    """The platform application-data location (e.g. ``~/.local/share/celPix`` on
    Linux, ``%APPDATA%\\celPix`` on Windows). Choosing paths is a Qt concern and
    lives here; the plugin scan itself is Qt-free (``celpix.plugins.discovery``)."""
    return Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    )


def _confirm_plugin(pending: PendingCodePlugin, *, from_project: bool = False) -> bool:
    """Ask the user whether to run a not-yet-approved code plugin. Default: No.

    ``from_project`` marks a plugin that came with the opened project rather than
    from the user's own plugin folder - the same gate, but it says so, because a
    project is something you can be *sent* and its author is not necessarily the
    person answering this dialog.
    """
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("celPix - load code plugin?")
    box.setText(
        "A code plugin that came with this project wants to load and will run "
        "with celPix's privileges."
        if from_project
        else "A code plugin wants to load and will run with celPix's privileges."
    )
    box.setInformativeText(
        f"{pending.path}\n\nSHA-256: {pending.digest[:16]}…\n\n"
        "Only load plugins you trust. Load it?"
    )
    box.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def _is_project(path: Path) -> bool:
    return path.suffix.lower() == PROJECT_EXTENSION


def _build_parser() -> argparse.ArgumentParser:
    """celPix's own command line, in the open gestures' vocabulary.

    Qt's options (``-platform``, ``-style``, …) are not in here and do not need
    to be: the QApplication is constructed first and removes the ones it knows,
    so what :meth:`QApplication.arguments` hands back is ours alone.
    """
    parser = argparse.ArgumentParser(
        prog="celpix",
        description="A graphics and palette editor for retro-game data.",
    )
    parser.add_argument(
        "--version", action="version", version=f"{APP_NAME} {__version__}"
    )
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        metavar="FILE",
        help=f"what to open: one {PROJECT_EXTENSION} project, or any number of "
        "data files - a ROM, a dump, a palette - as File > Open would",
    )
    parser.add_argument(
        "-t",
        "--type",
        dest="content_kind",
        choices=[kind.value for kind in ContentKind],
        help="what the data files hold, said rather than guessed from their "
        "signature: the command line's File > Open pixel/tilemap data. Detection "
        "can only recognise a format it knows, so a raw region of a ROM has no "
        "way to announce itself as a map or a palette. Applies to every FILE",
    )
    return parser


def _parse_args(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace:
    """``argv`` as validated options — or exit 2, having said why on the terminal.

    Everything refusable is refused here, before a window exists: a launch that
    was never going to work should say so where it was typed, rather than come up
    as an empty editor with a dialog in front of it.
    """
    args = parser.parse_args(argv)
    projects = [p for p in args.files if _is_project(p)]
    if projects and len(projects) != len(args.files):
        # A project is a whole session and opening one *replaces* the workspace,
        # so it claims the command line exactly as it claims a drop: anything
        # named beside it would either be discarded by the replace or land in the
        # new project as an unsaved edit nobody asked for.
        parser.error(f"a {PROJECT_EXTENSION} project opens on its own")
    if len(projects) > 1:
        parser.error(f"only one {PROJECT_EXTENSION} project can be open at a time")
    if args.content_kind and projects:
        # A project states each of its entries' kinds itself.
        parser.error("--type applies to data files, not to a project")
    images = [str(p) for p in args.files if p.suffix.lower() == ".png"]
    if images:
        # A PNG is *imported into* an open graphic rather than read as bytes
        # (there is nothing to interpret it as), and a launch has nothing open
        # yet - so File ▸ Import is the only gesture for it.
        parser.error("a PNG is imported into an open graphic, not opened: " + images[0])
    missing = [str(p) for p in args.files if not p.is_file()]
    if missing:
        parser.error("no such file: " + ", ".join(missing))
    return args


def _open_arguments(window: MainWindow, args: argparse.Namespace) -> None:
    """Open what the command line named, through the gestures the GUI uses.

    Nothing here decides anything a drop does not: the paths go to the same
    funnel, so the container detection, the palette suffixes, the "already open"
    shortcut and the single undo step are the ones the user already has.
    """
    for path in args.files:
        if _is_project(path):
            window._load_project(str(path))
            return
    forced = ContentKind(args.content_kind) if args.content_kind else None

    def kind(path: Path) -> ContentKind | None:
        if forced is not None:
            return forced
        if path.suffix.lower() in PALETTE_EXTENSIONS:
            # A palette suffix is palette data, as it is on a drop - it lands in
            # the Palettes section. --type says otherwise, in either direction.
            return ContentKind.PALETTE
        return None  # the container's own answer

    window._open_dropped([(str(path), kind(path)) for path in args.files])


def main(argv: list[str] | None = None) -> int:
    """Entry point for both ``celpix`` and ``python -m celpix``."""
    raw = list(argv if argv is not None else sys.argv)
    parser = _build_parser()
    # --help and --version are answered before Qt starts, so asking what the
    # options are works on a machine with no display to open a window on.
    # parse_known_args, because a Qt option sitting next to --help is not an
    # error - it is simply not ours to report on.
    if any(arg in ("-h", "--help", "--version") for arg in raw[1:]):
        parser.parse_known_args(raw[1:])
    app = QApplication(raw)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    # Qt has removed the options it recognized, so what is left over is ours.
    # Parsed here, at the top, so a command line that cannot work costs nothing:
    # no plugin scan, no window, and the reason on the terminal.
    args = _parse_args(parser, app.arguments()[1:])
    # Style and palette both come from the theme, before the window is built: a
    # widget that bakes a palette color into a pixmap should rasterize it once,
    # in the color it will be shown in (View ▸ Theme switches it live afterwards).
    apply_theme(load_enum_setting(THEME_KEY, Theme.LIGHT))
    # The window/taskbar/dock icon while running. Loaded from bytes (not a file
    # path) so it resolves the same in a source checkout and a frozen build,
    # where resources live inside the bundle. The packaged executables also
    # embed platform icons at build time (see packaging/ and the release
    # workflow); this covers every platform's live window and Linux, which has
    # no build-time icon.
    icon = QPixmap()
    icon.loadFromData(resources.read_bytes("icons", "app.png"))
    app.setWindowIcon(QIcon(icon))

    # Built-ins first, then whatever the user has dropped into the plugin folder
    # (plus any CELPIX_PLUGIN_PATH dirs). Code plugins are gated by a confirm
    # dialog and remembered in the trust store; load failures are reported, not
    # fatal.
    data_dir = _app_data_dir()
    plugin_dir = data_dir / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    # Pre-create the typed subfolders so opening the folder shows where each kind
    # of plugin goes, then seed the README and the inert `_`-prefixed reference
    # files — refreshed each launch so they match this build.
    for sub in FOLDER_STAGE:
        (plugin_dir / sub).mkdir(exist_ok=True)
    seed_examples(str(plugin_dir))
    trust = TrustStore(str(data_dir / "trusted-plugins.json"))

    def reload_plugins(project_path: str | None = None):
        """Build a fresh registry from built-ins + the plugin folders. Reused for
        the initial load, for opening a project and for the window's Refresh
        action, so all three go through the same trust gate.

        ``project_path`` is the open ``.celpix`` file, whose folder may carry a
        ``plugins/`` root of its own - scanned **after** the user's, so a project
        plugin colliding with one of theirs is reported rather than quietly
        taking its id.
        """
        reg = default_registry()
        dirs = [str(plugin_dir)]
        project_dir = project_plugin_dir(project_path)
        if project_dir is not None:
            dirs.append(project_dir)

        def confirm(pending: PendingCodePlugin) -> bool:
            return _confirm_plugin(
                pending,
                from_project=project_dir is not None
                and Path(pending.path).is_relative_to(project_dir),
            )

        load_issues = load_user_plugins(
            reg, dirs, project_dir=project_dir, trust=trust, confirm=confirm
        )
        return reg, load_issues

    registry, issues = reload_plugins()

    window = MainWindow(
        registry=registry,
        plugin_dir=str(plugin_dir),
        plugin_issues=issues,
        reload_plugins=reload_plugins,
    )
    window.show()
    # After show(), so anything the open asks about - a project's own code
    # plugins, a file that has moved since the project was saved - has a window
    # to sit in front of rather than a bare dialog.
    if args.files:
        _open_arguments(window, args)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
