"""The conftest's catch-all for modals: one nobody answers fails, never hangs."""

from celpix.ui.main_window import MainWindow


def test_an_unanswered_modal_returns_at_once_and_is_named(
    qtbot, tmp_path, unexpected_modals
):
    """New Slice…, Remove and Open pixel data each open a modal no hatch answers.

    One per form the catch-all patches — a dialog subclass's ``exec`` (Slice), a
    ``QMessageBox`` static (the removal question) and a ``QFileDialog`` static.
    Offscreen, any of them reaching Qt would wedge the run here with no name to
    blame; each returns its cancel answer instead, so nothing is sliced, removed
    or opened, and the list the test fails on names all three.
    """
    pixels = tmp_path / "s.bin"
    pixels.write_bytes(bytes(range(256)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(pixels))
    entries = list(window._workspace.entries)

    window._new_slice_action.trigger()
    window._remove_entry(entries[0])
    window._open_pixel()

    assert window._workspace.entries == entries
    assert [name.split()[0] for name in unexpected_modals] == [
        "SliceDialog.exec",
        "QMessageBox.question",
        "QFileDialog.getOpenFileName",
    ]
    unexpected_modals.clear()  # expected here, so not this test's failure
