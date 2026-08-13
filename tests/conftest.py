"""Shared test setup.

Force Qt's ``offscreen`` platform before PySide6 is imported anywhere, so the
suite runs headless in CI and under WSL without a display server. A developer
can override this by exporting ``QT_QPA_PLATFORM`` themselves.
"""

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# What makes a suite a Qt suite. Any one of these in a module's source means its
# tests need a QApplication: the import itself, the fixture that supplies one, or
# the window every UI suite drives.
_QT_MARKERS = ("PySide6", "qtbot", "MainWindow")


def pytest_collection_modifyitems(items):
    """Mark every test whose module needs Qt, so the model layer can run alone.

    ``-m "not qt"`` is then the whole of the fast loop — a few seconds over the
    Qt-free suites against a minute and a half for everything — and the two halves
    cannot drift apart, because neither is a list anyone maintains.

    **Detected rather than declared**, and that is the point: a list of Qt suites
    written down somewhere goes stale the moment one is added, renamed or split,
    and then quietly runs less than whoever is reading it believes. The source is
    the only description of a suite that cannot fall behind it.
    """
    seen: dict[str, bool] = {}
    for item in items:
        path = str(item.path)
        if path not in seen:
            try:
                text = item.path.read_text(encoding="utf-8")
            except OSError:  # a generated or in-memory module: assume it is not Qt's
                text = ""
            seen[path] = any(marker in text for marker in _QT_MARKERS)
        if seen[path]:
            item.add_marker(pytest.mark.qt)


@pytest.fixture(autouse=True)
def captured_alerts(monkeypatch):
    """Record error/warning alerts instead of showing them, for every test.

    ``MainWindow._alert`` is the single modal surface for failures; a real
    ``exec()`` would block the offscreen event loop and hang the suite. Here it
    appends ``(title, message)`` to a list a test can request by name to assert
    what the user was told. Guarded on the UI module already being imported, so
    the headless model-layer suites never pull Qt in through this fixture.
    """
    module = sys.modules.get("celpix.ui.main_window")
    if module is None:
        return []
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module.MainWindow,
        "_alert",
        lambda self, message, *, title="celPix", detail="": alerts.append(
            (title, message)
        ),
        raising=False,
    )
    return alerts


@pytest.fixture(autouse=True)
def open_as_answer(monkeypatch):
    """Answer the Ctrl-drop open-as prompt instead of showing it, for every test.

    Another ``exec()`` modal, and the same rule as ``captured_alerts``: offscreen
    it never returns, so a test that reaches it wedges the whole run with nothing
    to blame. Defaults to Cancel - the reading a test did not ask for should not
    silently become one it gets - and a test wanting the prompt answered assigns
    to ``.kind``.
    """
    module = sys.modules.get("celpix.ui.main_window")
    if module is None:
        return None

    class Answer:
        kind = None

    answer = Answer()
    monkeypatch.setattr(
        module.MainWindow,
        "_ask_content_kind",
        lambda self, path: answer.kind,
        raising=False,
    )
    return answer


@pytest.fixture(autouse=True)
def confirmations(monkeypatch):
    """Answer ``MainWindow._confirm`` instead of showing it, for every test.

    The third ``exec()`` modal on the same rule as ``captured_alerts``, and the
    one that guards a gesture rather than reports on one. Defaults to **Cancel**,
    like ``open_as_answer``: a prompt a test never arranged for must not silently
    become a Yes and take the action with it. A test that wants it answered
    assigns to ``.yes``, and every question asked is appended to ``.asked`` so it
    can assert the user was the one who decided.
    """
    module = sys.modules.get("celpix.ui.main_window")
    if module is None:
        return None

    class Answer:
        yes = False

        def __init__(self):
            self.asked: list[str] = []

    answer = Answer()

    def confirm(_self, message, **_kwargs):
        answer.asked.append(message)
        return answer.yes

    monkeypatch.setattr(module.MainWindow, "_confirm", confirm, raising=False)
    return answer


_settings_isolated = False


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path_factory):
    """Keep the suite's QSettings writes out of the developer's real config, and
    out of each other's way.

    Opening or saving a project records it in the app-wide recent-projects list,
    so without this a run would litter a developer's own celPix settings with
    temp paths — and read their real preferences back into the tests. The
    redirect is done once per session (the format and path are process-wide Qt
    state) and guarded like :func:`captured_alerts`, so the headless model-layer
    suites stay Qt-free.

    **Emptied before every test**, because a preference is written the moment its
    menu entry is toggled: a test that leaves Show Pinned Palette Colors off would
    otherwise change how every later test in the run renders, and one that asks
    what a fresh window defaults to would be answering for the test before it.
    """
    global _settings_isolated
    qtcore = sys.modules.get("PySide6.QtCore")
    if qtcore is None:
        return
    settings = qtcore.QSettings
    if not _settings_isolated:
        _settings_isolated = True
        # The app's store names its own organization (celpix.ui.widgets.settings),
        # so redirecting the format's user-scope path is all it takes; the
        # application name pytest-qt leaves unset doesn't come into it.
        settings.setDefaultFormat(settings.Format.IniFormat)
        settings.setPath(
            settings.Format.IniFormat,
            settings.Scope.UserScope,
            str(tmp_path_factory.mktemp("settings")),
        )
    from celpix.ui.widgets import settings as app_settings

    app_settings().clear()


@pytest.fixture(autouse=True)
def _destroy_widgets_between_tests():
    """Actually destroy the windows pytest-qt closed, before the next test.

    ``qtbot.addWidget`` cleanup ends in ``deleteLater()``, which only runs the
    destructor once an event loop spins — and these tests never spin one. Left
    alone, every window a run has ever built stays alive to the end of the
    session, and both construction and event delivery get steadily slower as
    they pile up (the tests late in a file paid several times what they cost in
    isolation). Flushing the deferred-delete queue here keeps a test's cost the
    same wherever it sits in the run.
    """
    yield
    qtcore = sys.modules.get("PySide6.QtCore")
    if qtcore is None:
        return
    app = sys.modules["PySide6.QtWidgets"].QApplication.instance()
    if app is not None:
        app.sendPostedEvents(None, qtcore.QEvent.Type.DeferredDelete)


@pytest.fixture(autouse=True)
def _drop_held_modifiers():
    """Let go of Shift/Ctrl/Alt after a test that typed with one held.

    ``QTest.keyClick(widget, key, modifier)`` leaves the modifier *held* as far
    as ``QApplication.keyboardModifiers()`` is concerned — the release it sends
    carries the modifier too — and that state is the application's, so it
    outlives the test and every widget it touched. It is not cosmetic: Qt reads
    the live modifiers wherever a widget is asked to do something with no event
    of its own (``QAbstractItemView.selectRow`` turns into an extend-from-anchor
    under a held Shift), so a later test would select more rows than it asked
    for — passing alone and failing in the suite, which is the worst way to find
    out.

    A release aimed at a throwaway widget, since the one that was typed into is
    gone by now and the state being cleared belongs to nobody in particular.
    """
    yield
    widgets = sys.modules.get("PySide6.QtWidgets")
    if widgets is None or widgets.QApplication.instance() is None:
        return
    qtcore = sys.modules["PySide6.QtCore"]
    if qtcore.QCoreApplication.instance() is None:
        return
    from PySide6.QtTest import QTest

    if (
        widgets.QApplication.keyboardModifiers()
        == qtcore.Qt.KeyboardModifier.NoModifier
    ):
        return
    spare = widgets.QWidget()
    for key in (
        qtcore.Qt.Key.Key_Shift,
        qtcore.Qt.Key.Key_Control,
        qtcore.Qt.Key.Key_Alt,
        qtcore.Qt.Key.Key_Meta,
    ):
        QTest.keyRelease(spare, key)
    spare.deleteLater()


@pytest.fixture(autouse=True)
def _help_dialogs_never_block(monkeypatch):
    """Make the Help dialogs' ``exec()`` return instead of blocking forever.

    They are the only modals a test can reach by triggering a menu action, and
    an ``exec()`` under the offscreen platform never returns — the run would
    wedge with nothing to blame. Construction still happens, so a test can
    assert on what the dialog was built from. Guarded like
    :func:`captured_alerts` so headless suites stay Qt-free.
    """
    module = sys.modules.get("celpix.ui.help_dialogs")
    if module is None:
        return
    for dialog in (module.ShortcutGuide, module.AboutDialog):
        monkeypatch.setattr(dialog, "exec", lambda self: 0, raising=False)


@pytest.fixture(autouse=True)
def _container_dialog_never_blocks(monkeypatch):
    """Make the container dialog's ``exec()`` return Rejected instead of blocking.

    Reachable by triggering File ▸ Edit File Container…, and an ``exec()`` under the
    offscreen platform never returns. Rejected is the safe default: the caller
    reads it as a cancel and changes nothing, so a test that lands here by
    accident does not silently re-read a file through some other container.
    Tests exercising the flow patch ``edit_container`` instead. Guarded like
    :func:`captured_alerts` so headless suites stay Qt-free.
    """
    module = sys.modules.get("celpix.ui.container_dialog")
    if module is None:
        return
    monkeypatch.setattr(module.ContainerDialog, "exec", lambda self: 0, raising=False)


@pytest.fixture(autouse=True)
def _container_info_never_blocks(monkeypatch):
    """Make the container-info popup's ``exec()`` return instead of blocking.

    Reachable by triggering File ▸ Container Info…, and the same rule as
    :func:`_container_dialog_never_blocks`: offscreen, ``exec()`` never returns.
    It is a read-only report, so there is no answer to fake — construction still
    happens, and a test can assert on what the dialog was built from. Guarded
    like :func:`captured_alerts` so headless suites stay Qt-free.
    """
    module = sys.modules.get("celpix.ui.container_info_dialog")
    if module is None:
        return
    monkeypatch.setattr(
        module.ContainerInfoDialog, "exec", lambda self: 0, raising=False
    )


@pytest.fixture(autouse=True)
def _composite_dialog_never_blocks(monkeypatch):
    """Make the composite dialog's ``exec()`` return Rejected instead of blocking.

    Reachable by triggering File ▸ New Composite View… or a composite's Edit…,
    and the same rule as :func:`_container_dialog_never_blocks`: offscreen,
    ``exec()`` never returns. Rejected is the safe default — the caller reads it
    as a cancel and adds nothing — while construction still happens, so a test
    can build the dialog and assert on the list it laid out. Guarded like
    :func:`captured_alerts` so headless suites stay Qt-free.
    """
    module = sys.modules.get("celpix.ui.composite_dialog")
    if module is None:
        return
    monkeypatch.setattr(module.CompositeDialog, "exec", lambda self: 0, raising=False)


@pytest.fixture(autouse=True)
def opened_menus(monkeypatch):
    """Record context menus instead of popping them up, for every test.

    ``QMenu.exec`` blocks exactly like a modal dialog's, and under the offscreen
    platform it never returns — so any test that reaches a right-click menu
    would wedge the run. The class itself can't be patched (Shiboken resolves
    ``exec`` past the Python attribute), so each ``celpix.ui`` module's imported
    ``QMenu`` name is swapped for a subclass that records the popup and returns.
    A test can request this fixture by name to inspect the menu that was built.
    Guarded like :func:`captured_alerts` so headless suites stay Qt-free.
    """
    widgets = sys.modules.get("PySide6.QtWidgets")
    if widgets is None:
        return []
    opened: list = []

    class RecordingMenu(widgets.QMenu):
        def exec(self, *_args, **_kwargs):
            opened.append(self)

    for name, module in list(sys.modules.items()):
        if name.startswith("celpix.ui") and (
            getattr(module, "QMenu", None) is widgets.QMenu
        ):
            monkeypatch.setattr(module, "QMenu", RecordingMenu)
    return opened


@pytest.fixture(autouse=True)
def _close_discards_edits(monkeypatch):
    """Let pytest-qt close windows without the unsaved-changes prompt.

    ``MainWindow.closeEvent`` asks the user to confirm discarding edits. That
    modal can never be answered under the offscreen platform, and pytest-qt
    closes every widget it was handed during teardown — so *any* test that
    leaves an entry dirty would wedge the whole run there, after its own body
    had already passed. Closing therefore always discards here; no test asserts
    on the quit prompt, and a test that wanted to would re-patch it itself.
    Guarded like :func:`captured_alerts` so headless suites stay Qt-free.
    """
    module = sys.modules.get("celpix.ui.main_window")
    if module is None:
        return
    from PySide6.QtWidgets import QMainWindow

    monkeypatch.setattr(
        module.MainWindow,
        "closeEvent",
        lambda self, event: QMainWindow.closeEvent(self, event),
        raising=False,
    )
