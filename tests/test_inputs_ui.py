"""Plugin inputs in the window: the badge, the Inputs window, the apply, and
the gestures around them (``docs/design/plugin-inputs.md`` §6)."""

from __future__ import annotations

from PySide6.QtCore import Qt

from celpix.project import projectfile
from celpix.project.inputs import IntegerFromBytes, RegionBinding
from celpix.ui import clipboard
from celpix.ui.main_window import MainWindow
from modelhelpers import XOR_ID, XorTableCodec, xor_bytes

TABLE = bytes([0x11, 0x22, 0x33, 0x44])
STREAM = bytes(range(64))


def _open(qtbot, tmp_path):
    """A window over a file holding a table at 0x100 and a stream at 0x200,
    with the toy codec registered and listed on the compression picker."""
    rom = tmp_path / "rom.bin"
    body = bytearray(0x400)
    body[0x100 : 0x100 + len(TABLE)] = TABLE
    body[0x200 : 0x200 + len(STREAM)] = STREAM
    rom.write_bytes(bytes(body))
    window = MainWindow()
    qtbot.addWidget(window)
    window._registry.register(XorTableCodec())
    window._populate_compression()
    window._load_pixel(str(rom))
    return window, window._workspace.current


def _pick_preview(window, codec: str) -> None:
    window._compression.setCurrentIndex(window._compression.findData(codec))


def _bound_slice(window, parent, name="stream", offset=0x200):
    entry = window._workspace.add_slice(parent.path, name, offset, len(STREAM), XOR_ID)
    entry.inputs = {XOR_ID: {"table": RegionBinding(offset=0x100, length=len(TABLE))}}
    return entry


def test_the_badge_appears_for_a_codec_with_inputs_and_wears_their_status(
    qtbot, tmp_path
) -> None:
    window, rom = _open(qtbot, tmp_path)
    badge = window._compression_inputs_badge
    assert badge.isHidden()  # the pass-through declares nothing
    assert not window._inputs_action.isEnabled()

    _pick_preview(window, XOR_ID)
    assert not badge.isHidden()
    assert badge.property("problem")  # nothing bound yet
    assert "Nothing is bound" in badge.toolTip()
    assert window._inputs_action.isEnabled()
    # No preview: a scheme that needs a table it has not got shows nothing,
    # rather than a decode against garbage.
    assert not window._overlay.isVisible()

    # Bound through the window: one undoable step, the badge clears, and the
    # preview appears — decoded with the table.
    window._show_inputs(rom)
    form = window._inputs_window
    assert form.isVisible() and form.entry is rom
    form._rows[(XOR_ID, "table")].set_binding(RegionBinding(offset=0x100, length=4))
    form._on_apply()
    assert rom.inputs == {XOR_ID: {"table": RegionBinding(offset=0x100, length=4)}}
    assert not badge.property("problem")
    assert window._overlay.isVisible()
    assert window._undo_stack.undoText() == 'inputs of "rom.bin"'
    window._undo_stack.undo()
    assert rom.inputs == {}
    assert badge.property("problem")


def test_a_slice_carved_under_the_codec_inherits_and_decodes(qtbot, tmp_path) -> None:
    window, rom = _open(qtbot, tmp_path)
    rom.inputs = {XOR_ID: {"table": RegionBinding(offset=0x100, length=4)}}
    carved = window._workspace.add_slice(rom.path, "carved", 0x200, len(STREAM), XOR_ID)
    assert carved.inputs == rom.inputs
    window._activate_entry(carved)
    assert window._doc.pixel_data == xor_bytes(STREAM, TABLE)
    assert window._doc.pixel_config.write_enabled
    # The slice's own codec is not the preview's, so the compression badge is
    # silent here; the File menu row still knows the slice has inputs.
    assert window._compression_inputs_badge.isHidden()
    assert window._inputs_action.isEnabled()


def test_use_selection_binds_the_parents_selection_and_go_to_shows_it(
    qtbot, tmp_path
) -> None:
    window, rom = _open(qtbot, tmp_path)
    entry = _bound_slice(window, rom)
    window._activate_entry(entry)
    window._show_inputs(entry)
    form = window._inputs_window
    row = form._rows[(XOR_ID, "table")]

    # Over to the parent, select tile 8 (byte 0x100 at 32 bytes a tile), and
    # ask the row to take it: an absolute offset in the file, this-file shape.
    window._activate_entry(rom)
    window._select_tiles(8, 8)
    window._on_inputs_use_selection(row)
    assert row.binding() == RegionBinding(offset=0x100, length=32)
    # The window stayed pinned to the slice while the parent was on screen.
    assert form.entry is entry

    # Go to: the parent comes up with the table's bytes in view. The file is
    # smaller than a screen, so "in view" is the whole of it and the origin
    # cannot move — what is asserted is the landing, not a scroll.
    window._activate_entry(entry)
    window._go_to_binding(RegionBinding(offset=0x120, length=4))
    assert window._workspace.current is rom
    start = window._byte_position()
    page = window._columns.value() * window._view_rows() * window._doc.bytes_per_tile
    assert start <= 0x120 < start + page


def test_use_selection_on_a_nested_slice_names_no_file_position(
    qtbot, tmp_path
) -> None:
    """A raw nested slice's positions count in its parent slice's buffer, while
    *This file* resolves against the root file — so its own selection has no
    this-file offset to give, however raw it reads."""
    window, rom = _open(qtbot, tmp_path)
    outer = window._workspace.add_slice(rom.path, "outer", 0x80, 0x200)
    inner = window._workspace.add_slice_under(outer, "inner", 0x80, 0x100)

    window._activate_entry(inner)
    window._select_tiles(0, 0)
    assert window._selection_as_binding(inner) is None
    # Its parent slice on screen is an entry it may read; the root file on
    # screen is its own file, at the file's own offset.
    window._activate_entry(outer)
    window._select_tiles(0, 0)
    assert window._selection_as_binding(inner) == RegionBinding(
        entry=outer, offset=0, length=32
    )
    window._activate_entry(rom)
    window._select_tiles(8, 8)
    assert window._selection_as_binding(inner) == RegionBinding(offset=0x100, length=32)


def test_jump_to_source_carries_the_slices_bindings_to_the_preview(
    qtbot, tmp_path
) -> None:
    window, rom = _open(qtbot, tmp_path)
    entry = _bound_slice(window, rom)
    window._activate_entry(entry)
    window._jump_to_slice_source(entry)
    assert window._workspace.current is rom
    assert window._compression_id() == XOR_ID
    assert rom.inputs[XOR_ID] == entry.inputs[XOR_ID]
    assert window._overlay.isVisible()


def test_applying_to_the_selection_lands_one_step_on_every_slice(
    qtbot, tmp_path
) -> None:
    window, rom = _open(qtbot, tmp_path)
    first = _bound_slice(window, rom, "first")
    second = window._workspace.add_slice(rom.path, "second", 0x210, 32, XOR_ID)
    raw = window._workspace.add_slice(rom.path, "raw", 0x220, 16)
    panel = window._files_panel
    panel._tree.setCurrentItem(panel._items[first])
    for entry in (second, raw):
        panel._items[entry].setSelected(True)
    window._show_inputs(first)
    form = window._inputs_window
    assert not form._scope_row.isHidden()
    form._rows[(XOR_ID, "output_size")].set_binding(16)
    form._selected.setChecked(True)
    form._on_apply()
    assert second.inputs[XOR_ID]["output_size"] == 16
    assert first.inputs[XOR_ID]["output_size"] == 16
    assert raw.inputs == {}  # nothing on it declares the key
    assert window._undo_stack.undoText() == "inputs of 2 entries"
    window._undo_stack.undo()
    assert "output_size" not in first.inputs[XOR_ID]
    assert second.inputs == {}


def test_copy_and_paste_inputs_land_only_what_the_target_declares(
    qtbot, tmp_path
) -> None:
    window, rom = _open(qtbot, tmp_path)
    source = _bound_slice(window, rom, "source")
    source.inputs[XOR_ID]["output_size"] = IntegerFromBytes(offset=0x300, width=2)
    same = window._workspace.add_slice(rom.path, "same", 0x210, 32, XOR_ID)
    raw = window._workspace.add_slice(rom.path, "raw", 0x220, 16)
    window._copy_inputs(source)
    window._paste_inputs([same, raw])
    assert same.inputs == source.inputs
    assert raw.inputs == {}


def test_a_pasted_entry_keeps_a_binding_onto_an_entry_pasted_with_it(
    qtbot, tmp_path
) -> None:
    window, rom = _open(qtbot, tmp_path)
    table = window._workspace.add_slice(rom.path, "table", 0x100, 4)
    stream = window._workspace.add_slice(rom.path, "stream", 0x200, 64, XOR_ID)
    stream.inputs = {XOR_ID: {"table": RegionBinding(entry=table, offset=0, length=4)}}
    # Both slices in one copy, pasted back under the same file: the copied
    # stream points at the copied table, not back at the original.
    clipboard.put_entries(
        projectfile.entries_payload(
            [table, stream], window._workspace.entries, clipboard.SESSION_TOKEN
        ),
        [],
        window._live_bindings([table, stream]),
    )
    window._paste_entries(rom)
    pasted = [e for e in window._workspace.entries if e.name.startswith("stream")]
    assert len(pasted) == 2
    copy = next(e for e in pasted if e is not stream)
    bound = copy.inputs[XOR_ID]["table"].entry
    assert bound is not table and bound.name.startswith("table")
    assert bound in window._workspace.entries


def test_the_slice_dialog_shows_the_codecs_bindings_only_under_that_codec(
    qtbot, tmp_path
) -> None:
    from celpix.plugins.base import NO_COMPRESSION
    from celpix.ui.slice_dialog import SliceDialog

    window, rom = _open(qtbot, tmp_path)
    rom.inputs = {XOR_ID: {"table": RegionBinding(offset=0x100, length=4)}}
    dialog = SliceDialog(
        window._registry,
        paths=rom.paths,
        compression_id=XOR_ID,
        inputs_hint=lambda codec: window._inputs_hint(rom, codec),
        parent=window,
    )
    qtbot.addWidget(dialog)
    assert (
        dialog._inputs.text()
        == "Key table: 0x000100, 4 B; Output size: unbound (optional); Invert: no; "
        "Order: Forward"
    )
    dialog._decompress.setCurrentIndex(dialog._decompress.findData(NO_COMPRESSION))
    assert dialog._inputs.text() == ""


def test_the_slice_dialogs_badge_edits_the_codec_the_dialog_has_picked(
    qtbot, tmp_path
) -> None:
    """The badge binds under the picker's codec, not the entry's current one —
    and the dialog's line re-reads what the apply landed."""
    from celpix.plugins.base import NO_COMPRESSION
    from celpix.ui.slice_dialog import SliceDialog

    window, rom = _open(qtbot, tmp_path)
    entry = _bound_slice(window, rom)
    entry.compression_id = NO_COMPRESSION  # read raw today; the dialog picks the codec
    entry.inputs = {}
    dialog = SliceDialog(
        window._registry,
        paths=rom.paths,
        compression_id=NO_COMPRESSION,
        inputs_hint=lambda codec: window._inputs_hint(entry, codec),
        edit_inputs=lambda d, codec: window._edit_slice_inputs(d, entry, codec),
        parent=window,
    )
    qtbot.addWidget(dialog)
    # No inputs under the pass-through, so neither the line nor its editor.
    assert dialog._inputs_badge.isHidden()

    dialog._decompress.setCurrentIndex(dialog._decompress.findData(XOR_ID))
    assert not dialog._inputs_badge.isHidden()
    dialog._inputs_badge.click()

    form = window._inputs_window
    qtbot.addWidget(form)
    assert form.isVisible() and form.entry is entry
    # The section is the *dialog's* codec, which the entry does not read with.
    assert set(form._rows) == {
        (XOR_ID, "table"),
        (XOR_ID, "output_size"),
        (XOR_ID, "invert"),
        (XOR_ID, "order"),
    }
    # Above the modal dialog, or it would be drawn and then ignore every click.
    assert form.windowModality() == Qt.WindowModality.ApplicationModal

    form._rows[(XOR_ID, "table")].set_binding(RegionBinding(offset=0x100, length=4))
    form._on_apply()
    assert entry.inputs == {XOR_ID: {"table": RegionBinding(offset=0x100, length=4)}}
    # Applying rebuilds the form: the same section, still above the dialog.
    assert set(form._rows) == {
        (XOR_ID, "table"),
        (XOR_ID, "output_size"),
        (XOR_ID, "invert"),
        (XOR_ID, "order"),
    }
    assert form.windowModality() == Qt.WindowModality.ApplicationModal
    dialog.refresh_inputs()  # what returning to the dialog does
    assert dialog._inputs.text().startswith("Key table: 0x000100, 4 B")

    # The next open from the toolbar gives the modal stack back.
    form.hide()
    _pick_preview(window, XOR_ID)
    window._show_inputs(rom)
    assert form.windowModality() == Qt.WindowModality.NonModal


def test_escape_closes_the_inputs_window(qtbot, tmp_path) -> None:
    """A QWidget gets none of QDialog's key handling, so Escape is spelled out —
    and it is the only way out while the window is blocking a Slice dialog."""
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    window, rom = _open(qtbot, tmp_path)
    entry = _bound_slice(window, rom)
    window._show_inputs(entry)
    form = window._inputs_window
    assert form.isVisible()

    # Sent to a *field*, not the window: that is where the keyboard actually is
    # while the form is being filled in, and a child that swallowed the key would
    # leave the window with no way out at all.
    field = form._rows[(XOR_ID, "table")]._offset
    field.setFocus()
    QApplication.sendEvent(
        field,
        QKeyEvent(
            QKeyEvent.Type.KeyPress,
            Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    assert not form.isVisible()
    # Closed, not unpinned: re-opening is what the badge does, and the entry it
    # was on is still the one it answers for.
    assert form.entry is entry


def test_an_edit_to_a_table_in_another_file_re_reads_the_stream_bound_to_it(
    qtbot, tmp_path
) -> None:
    window, rom = _open(qtbot, tmp_path)
    keys = tmp_path / "keys.bin"
    keys.write_bytes(TABLE + bytes(60))
    window._load_pixel(str(keys))
    key_file = window._workspace.current
    stream = window._workspace.add_slice(rom.path, "stream", 0x200, 64, XOR_ID)
    stream.inputs = {
        XOR_ID: {"table": RegionBinding(entry=key_file, offset=0, length=4)}
    }
    window._activate_entry(stream)
    assert window._doc.pixel_data == xor_bytes(STREAM, TABLE)

    # Paint the key file: the stream is clean, so it is re-read against the
    # new table the moment the edit lands.
    window._activate_entry(key_file)
    revision = window._workspace.next_revision()
    window._apply_pixel_bytes(
        [(0, bytes([0xAA, 0xBB, 0xCC, 0xDD]))], revision, entry=key_file
    )
    assert stream.doc is None
    window._activate_entry(stream)
    assert window._doc.pixel_data == xor_bytes(STREAM, bytes([0xAA, 0xBB, 0xCC, 0xDD]))


def test_a_flag_row_binds_only_when_switched(qtbot, tmp_path) -> None:
    window, rom = _open(qtbot, tmp_path)
    entry = _bound_slice(window, rom)
    window._activate_entry(entry)
    window._show_inputs(entry)
    form = window._inputs_window
    row = form._rows[(XOR_ID, "invert")]
    # Showing the default binds nothing; switched, it binds the other value,
    # and the slice is re-read through it.
    assert row.binding() is None
    row._box.setChecked(True)
    assert row.binding() is True
    form._on_apply()
    assert entry.inputs[XOR_ID]["invert"] is True
    assert window._doc.pixel_data == bytes(b ^ 0xFF for b in xor_bytes(STREAM, TABLE))
    window._show_inputs(entry)
    row = window._inputs_window._rows[(XOR_ID, "invert")]
    assert row._box.isChecked()
    row._box.setChecked(False)
    window._inputs_window._on_apply()
    assert "invert" not in entry.inputs[XOR_ID]
    assert window._doc.pixel_data == xor_bytes(STREAM, TABLE)


def test_a_choice_row_stores_keys_and_keeps_a_key_it_does_not_list(
    qtbot, tmp_path
) -> None:
    window, rom = _open(qtbot, tmp_path)
    entry = _bound_slice(window, rom)
    window._activate_entry(entry)
    window._show_inputs(entry)
    form = window._inputs_window
    row = form._rows[(XOR_ID, "order")]
    # Labels shown, keys stored; the default showing binds nothing.
    assert [row._combo.itemText(i) for i in range(row._combo.count())] == [
        "Forward",
        "Reversed",
    ]
    assert row.binding() is None
    row._combo.setCurrentIndex(1)
    assert row.binding() == "reverse"
    form._on_apply()
    assert entry.inputs[XOR_ID]["order"] == "reverse"
    assert window._doc.pixel_data == xor_bytes(STREAM, TABLE)[::-1]

    # A key the plugin does not list (a typo, a newer plugin's option) is shown
    # as itself and marked, not swapped for the default behind the user's back.
    entry.inputs = {XOR_ID: {**entry.inputs[XOR_ID], "order": "sideways"}}
    window._show_inputs(entry)
    row = window._inputs_window._rows[(XOR_ID, "order")]
    assert row._combo.currentText() == "sideways (unknown)"
    assert row.binding() == "sideways"
    assert row.status_widget().text() == "!"
    row._combo.setCurrentIndex(0)
    assert row.status_widget().text() != "!"


def test_a_file_that_decompresses_whole_rereads_when_its_inputs_change(
    qtbot, tmp_path
) -> None:
    """A file on its own scheme reads through that scheme's bindings exactly as
    a slice does, so an apply re-reads it and its undo reads it back."""
    window, rom = _open(qtbot, tmp_path)
    rom.compression_id = XOR_ID
    rom.inputs = {XOR_ID: {"table": RegionBinding(offset=0x100, length=4)}}
    window._reread_entries([rom])
    body = (tmp_path / "rom.bin").read_bytes()
    decoded = xor_bytes(body, TABLE)
    assert rom.doc.pixel_data == decoded

    window._show_inputs(rom)
    form = window._inputs_window
    form._rows[(XOR_ID, "invert")]._box.setChecked(True)
    form._on_apply()
    assert rom.doc.pixel_data == bytes(b ^ 0xFF for b in decoded)
    window._undo_stack.undo()
    assert rom.doc.pixel_data == decoded


def test_go_to_refuses_a_this_file_binding_of_a_file_that_decompresses_whole(
    qtbot, tmp_path
) -> None:
    """The binding's offset is into the packed bytes, which no view shows."""
    window, rom = _open(qtbot, tmp_path)
    rom.compression_id = XOR_ID
    binding = RegionBinding(offset=0x100, length=4)
    rom.inputs = {XOR_ID: {"table": binding}}
    window._reread_entries([rom])
    window._show_inputs(rom)
    landed: list[int] = []
    window._land_on_byte = landed.append
    window._go_to_binding(binding)
    assert "packed bytes" in window.statusBar().currentMessage()
    assert landed == []


def test_re_pointing_a_table_slice_re_reads_the_stream_bound_to_it(
    qtbot, tmp_path
) -> None:
    """A stream reading its table out of another slice decodes with wherever
    that slice now points — after the re-point and after its undo."""
    from dataclasses import replace

    from celpix.project.workspace import SliceParams
    from celpix.ui.undo_commands import SliceEditCommand

    other = bytes([0xA0, 0xB0, 0xC0, 0xD0])
    window, rom = _open(qtbot, tmp_path)
    path = tmp_path / "rom.bin"
    body = bytearray(path.read_bytes())
    body[0x180:0x184] = other
    path.write_bytes(bytes(body))
    keys = window._workspace.add_slice(rom.path, "keys", 0x100, 0x20)
    stream = window._workspace.add_slice(rom.path, "stream", 0x200, 64, XOR_ID)
    stream.inputs = {XOR_ID: {"table": RegionBinding(entry=keys, offset=0, length=4)}}
    window._activate_entry(keys)
    window._activate_entry(stream)
    assert stream.doc.pixel_data == xor_bytes(STREAM, TABLE)

    before = SliceParams(
        keys.name,
        keys.slice_offset,
        keys.slice_length,
        keys.compression_id,
        keys.reshape_id,
        keys.content_kind,
        keys.slot_fill,
        match_parent=keys.match_parent,
    )
    after = replace(before, offset=0x180)
    window._push_command(SliceEditCommand(window, keys, before=before, after=after))
    window._activate_entry(stream)
    assert stream.doc.pixel_data == xor_bytes(STREAM, other)
    window._undo_stack.undo()
    window._activate_entry(stream)
    assert stream.doc.pixel_data == xor_bytes(STREAM, TABLE)


def test_edit_slice_bounds_a_slice_of_a_file_that_decompresses_whole_as_new_slice_does(
    qtbot, tmp_path, monkeypatch
) -> None:
    """Both dialogs bound the offsets by the unpacked stream (shorter here)
    rather than the packed file, and name the file they are counted in."""
    from celpix.ui.slice_dialog import SliceDialog

    window, rom = _open(qtbot, tmp_path)
    rom.compression_id = XOR_ID
    rom.inputs = {
        XOR_ID: {"table": RegionBinding(offset=0x100, length=4), "output_size": 0x300}
    }
    window._reread_entries([rom])
    asked: list[tuple] = []
    monkeypatch.setattr(
        SliceDialog,
        "get_slice",
        staticmethod(lambda *_a, **kw: asked.append((kw["extent"], kw["source"]))),
    )
    window._create_slice_via_dialog(rom)
    cut = window._workspace.add_slice(rom.path, "cut", 0x10, 0x10)
    window._edit_slice(cut)
    assert asked == [(0x300, rom.name), (0x300, rom.name)]
