"""Application bootstrap: construct the QApplication and show the main window."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QMessageBox

from celpix import __version__, resources
from celpix.plugins.discovery import FOLDER_STAGES, load_user_plugins, seed_examples
from celpix.plugins.registry import default_registry
from celpix.plugins.trust import PendingCodePlugin, TrustStore
from celpix.ui.main_window import MainWindow

# Application identifier — backs QSettings and the platform data location. We set
# *only* the application name (no organization name): QStandardPaths appends both
# organizationName and applicationName, so setting an org equal to the app would
# nest the data dir as Celpix/Celpix. Celpix is a single app with no separate org.
APP_NAME = "Celpix"


def _app_data_dir() -> Path:
    """The platform application-data location (e.g. ``~/.local/share/Celpix`` on
    Linux, ``%APPDATA%\\Celpix`` on Windows). Choosing paths is a Qt concern and
    lives here; the plugin scan itself is Qt-free (``celpix.plugins.discovery``)."""
    return Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    )


def _confirm_plugin(pending: PendingCodePlugin) -> bool:
    """Ask the user whether to run a not-yet-approved code plugin. Default: No."""
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Celpix — load code plugin?")
    box.setText("A code plugin wants to load and will run with Celpix's privileges.")
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
    # Pre-create the typed subfolders so opening the folder shows where each
    # kind of plugin goes, and seed them with inert _example.* reference files.
    for sub in FOLDER_STAGES:
        (plugin_dir / sub).mkdir(exist_ok=True)
    seed_examples(str(plugin_dir))
    trust = TrustStore(str(data_dir / "trusted-plugins.json"))

    def reload_plugins():
        """Build a fresh registry from built-ins + the plugin folder. Reused for
        the initial load and for the window's Refresh action, so both go through
        the same trust gate."""
        reg = default_registry()
        load_issues = load_user_plugins(
            reg, [str(plugin_dir)], trust=trust, confirm=_confirm_plugin
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
