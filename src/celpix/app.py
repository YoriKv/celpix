"""Application bootstrap: construct the QApplication and show the main window."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QMessageBox

from celpix import APP_NAME, __version__, resources
from celpix.plugins.discovery import (
    FOLDER_STAGE,
    load_user_plugins,
    project_plugin_dir,
    seed_examples,
)
from celpix.plugins.registry import default_registry
from celpix.plugins.trust import PendingCodePlugin, TrustStore
from celpix.ui.main_window import MainWindow
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


def main(argv: list[str] | None = None) -> int:
    """Entry point for both ``celpix`` and ``python -m celpix``."""
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
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
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
