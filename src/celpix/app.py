"""Application bootstrap: construct the QApplication and show the main window."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QMessageBox, QProxyStyle, QStyle

from celpix import APP_NAME, __version__, resources
from celpix.plugins.discovery import FOLDER_STAGE, load_user_plugins, seed_examples
from celpix.plugins.registry import default_registry
from celpix.plugins.trust import PendingCodePlugin, TrustStore
from celpix.ui.main_window import MainWindow

# The application name is the *only* identity set on the QApplication (no
# organization name): QStandardPaths appends both organizationName and
# applicationName, so setting an org equal to the app would nest the data dir as
# celPix/celPix. celPix is a single app with no separate org — which is also why
# the preference store names its organization explicitly rather than relying on
# this (:func:`celpix.ui.widgets.settings`).


class _UnderlinedMnemonics(QProxyStyle):
    """Show every menu's mnemonic underline without holding Alt down.

    Windows (and any desktop whose theme asks for it) hides the underlines until
    Alt is pressed - the platform's "underline access keys" setting - so a menu
    looks as though it has no keyboard route at all until you already know to
    reach for one. celPix's menus are built around those letters, so the hint is
    forced on and they are drawn from the moment a menu opens. Everything else is
    delegated: this changes no other part of the platform's look.
    """

    def styleHint(  # noqa: N802 - Qt override
        self,
        hint: QStyle.StyleHint,
        option=None,  # noqa: ANN001 - QStyleOption
        widget=None,  # noqa: ANN001 - QWidget
        returnData=None,  # noqa: ANN001, N803 - QStyleHintReturn
    ) -> int:
        if hint == QStyle.StyleHint.SH_UnderlineShortcut:
            return 1
        return super().styleHint(hint, option, widget, returnData)


def _app_data_dir() -> Path:
    """The platform application-data location (e.g. ``~/.local/share/celPix`` on
    Linux, ``%APPDATA%\\celPix`` on Windows). Choosing paths is a Qt concern and
    lives here; the plugin scan itself is Qt-free (``celpix.plugins.discovery``)."""
    return Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    )


def _confirm_plugin(pending: PendingCodePlugin) -> bool:
    """Ask the user whether to run a not-yet-approved code plugin. Default: No."""
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("celPix - load code plugin?")
    box.setText("A code plugin wants to load and will run with celPix's privileges.")
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
    # Wraps the platform's own style (QProxyStyle with no base takes whatever
    # QApplication just picked), so it only overrides the one hint.
    app.setStyle(_UnderlinedMnemonics())
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
