"""The Edit Palette window: a live preview that Cancel takes back, and OK
commits as one undo step through the ordinary colour-edit path.

The preview is a palette swapped in under the view with no record of it, so
the regression risks are the two ends of that: a cancelled preview that stays
on screen, and a commit that lands on what was previewed rather than on the
original. Both are checked from inside the modal, where the preview is up.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QFileDialog

from celpix.core.color_ops import BlendMode, OpKind, PaletteOp
from celpix.project.workspace import EntryKind
from celpix.ui.main_window import MainWindow
from celpix.ui.palette_edit_dialog import EditScope, PaletteEditDialog

ACCEPTED = QDialog.DialogCode.Accepted.value
REJECTED = QDialog.DialogCode.Rejected.value


def _open(qtbot, tmp_path, monkeypatch, tiles: int = 8) -> MainWindow:
    """A ``tiles``-tile SNES-4bpp file, on the generated default palette."""
    px = tmp_path / "p.4bpp.sfc"
    px.write_bytes(bytes((i * 13 + 1) & 0xFF for i in range(32 * tiles)))
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(px), ""))
    )
    window = MainWindow()
    qtbot.addWidget(window)
    window._open_pixel()
    return window


def _open_in_offset_mode(qtbot, tmp_path, monkeypatch, tiles: int = 8) -> MainWindow:
    """...with its palette read from tile 1 of its own bytes, edited in place."""
    window = _open(qtbot, tmp_path, monkeypatch, tiles)
    window._on_slots_selected(1, 1)
    window._load_palette_from_selection()
    assert window._palette_mode == "offset"
    return window


# -- the dialog on its own --------------------------------------------------
def test_the_dialog_shows_only_what_the_operation_reads(qtbot) -> None:
    dialog = PaletteEditDialog(selected_color=0xFF123456)
    qtbot.addWidget(dialog)
    seen: list[int] = []
    dialog.changed.connect(lambda: seen.append(1))

    def shown(name: str) -> bool:
        label, holder = dialog._rows[name]
        return not label.isHidden() and not holder.isHidden()

    # Invert opens the list: nothing to set but the scope.
    assert dialog.op() == PaletteOp(OpKind.INVERT, color=0xFF123456)
    assert not shown("mode") and not shown("color") and not shown("amount")
    dialog.set_op(OpKind.BLEND)
    assert shown("mode") and shown("color") and shown("amount")
    dialog.set_op(OpKind.GRAYSCALE)
    assert not shown("mode") and not shown("color") and shown("amount")

    dialog.set_op(OpKind.BLEND)
    dialog.set_mode(BlendMode.SCREEN)
    dialog.set_amount(40)
    dialog._hex.setText("#abcdef")
    dialog._hex.commit()
    dialog.set_scope(EditScope.ALL)
    assert dialog.op() == PaletteOp(
        OpKind.BLEND, color=0xFFABCDEF, amount=0.4, mode=BlendMode.SCREEN
    )
    assert dialog.scope() is EditScope.ALL
    assert dialog._hex.text() == "#ABCDEF"  # normalised spelling
    # One announcement per change: op, op, op, mode, amount, colour, scope.
    assert len(seen) == 7

    # Use Selected hands the swatch grid's colour back to the field.
    dialog.set_color(0xFF000000)
    dialog._use_selected.click()
    assert dialog.op().color == 0xFF123456


def test_hsv_shift_shows_its_three_inputs_and_reads_them_back(qtbot) -> None:
    dialog = PaletteEditDialog(selected_color=None)
    qtbot.addWidget(dialog)
    dialog.set_op(OpKind.HSV_SHIFT)
    hidden = [
        name
        for name in ("color", "amount", "mode")
        if not dialog._rows[name][1].isHidden()
    ]
    assert hidden == []
    assert all(
        not dialog._rows[n][1].isHidden() for n in ("hue", "saturation", "value")
    )
    dialog.set_hsv_shift(45, -20, 10)
    assert dialog.op() == PaletteOp(
        OpKind.HSV_SHIFT, color=0xFFFFFFFF, hue=45.0, saturation=-0.2, value=0.1
    )
    # The spin follows the slider and the slider the spin, one announcement each.
    seen: list[int] = []
    dialog.changed.connect(lambda: seen.append(1))
    dialog._hue_spin.setValue(-90)
    assert dialog._hue.value() == -90 and seen == [1]


def test_clicking_the_swatch_opens_the_picker_whose_cancel_leaves_the_color(
    qtbot, monkeypatch
) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QColorDialog

    dialog = PaletteEditDialog(selected_color=0xFF123456)
    qtbot.addWidget(dialog)
    dialog.set_op(OpKind.SET)
    asked: list[QColor] = []

    def pick(initial, *_args, **_kwargs):  # noqa: ANN001, ANN202 — the Qt static
        asked.append(initial)
        return QColor(0x80, 0xA0, 0xC0, 0x40)  # alpha is not an input: dropped

    monkeypatch.setattr(QColorDialog, "getColor", staticmethod(pick))
    qtbot.mouseClick(dialog._swatch, Qt.MouseButton.LeftButton)
    assert asked[0].rgb() == 0xFF123456  # opened on the current colour
    assert dialog.op().color == 0xFF80A0C0
    assert dialog._hex.text() == "#80A0C0"

    monkeypatch.setattr(
        QColorDialog, "getColor", staticmethod(lambda *a, **k: QColor())
    )
    qtbot.mouseClick(dialog._swatch, Qt.MouseButton.LeftButton)
    assert dialog.op().color == 0xFF80A0C0


def test_a_direct_color_view_offers_no_palette_row(
    qtbot, tmp_path, monkeypatch
) -> None:
    """View as Palette indexes nothing: its geometry would size a 2-colour
    "row", so the scope is withheld and the whole palette is the default."""
    window = _open_in_offset_mode(qtbot, tmp_path, monkeypatch)
    combo = window._pixel_preset
    combo.setCurrentIndex(combo.findData("preset.pixel.view-as-palette"))
    seen: dict[str, object] = {}

    def exec_(dialog: PaletteEditDialog) -> int:
        seen["row_enabled"] = dialog._scopes[EditScope.ROW].isEnabled()
        seen["scope"] = dialog.scope()
        return REJECTED

    monkeypatch.setattr(PaletteEditDialog, "exec", exec_)
    window._edit_palette_action.click()
    assert seen == {"row_enabled": False, "scope": EditScope.ALL}


def test_with_nothing_selected_the_selection_scope_is_not_offered(qtbot) -> None:
    dialog = PaletteEditDialog(selected_color=None)
    qtbot.addWidget(dialog)
    assert not dialog._scopes[EditScope.SELECTED].isEnabled()
    assert not dialog._use_selected.isEnabled()
    assert dialog.op().color == 0xFFFFFFFF


# -- preview and cancel ------------------------------------------------------
def test_cancel_takes_the_preview_back_down(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_in_offset_mode(qtbot, tmp_path, monkeypatch)
    window._palette_row.setValue(0)
    original = window._doc.palette
    inverted_row = [PaletteOp(OpKind.INVERT).apply(c) for c in original.colors[:16]]
    seen: dict[str, object] = {}

    def exec_(dialog: PaletteEditDialog) -> int:
        # The window opened on Invert over the palette row: that is what is on
        # screen, on the document and on the swatch grid alike.
        seen["doc"] = list(window._doc.palette.colors)
        seen["panel"] = list(window._palette_panel._colors)
        return REJECTED

    monkeypatch.setattr(PaletteEditDialog, "exec", exec_)
    steps = window._undo_stack.count()
    window._edit_palette_action.click()

    assert seen["doc"][:16] == inverted_row
    assert seen["doc"][16:] == original.colors[16:]
    assert seen["panel"] == seen["doc"]
    # Cancelled: the very palette is back, nothing is recorded, nothing is dirty.
    assert window._doc.palette is original
    assert window._palette_panel._colors == original.colors
    assert window._undo_stack.count() == steps
    assert not window._workspace.current.palette_dirty


def test_ok_commits_the_previewed_result_as_one_undo_step(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_in_offset_mode(qtbot, tmp_path, monkeypatch)
    original = window._doc.palette
    op = PaletteOp(OpKind.GRAYSCALE, amount=0.5)
    expected = op.apply_all(original.colors)
    assert expected != original.colors

    def exec_(dialog: PaletteEditDialog) -> int:
        dialog.set_op(OpKind.GRAYSCALE)
        dialog.set_amount(50)
        dialog.set_scope(EditScope.ALL)
        return ACCEPTED

    monkeypatch.setattr(PaletteEditDialog, "exec", exec_)
    steps = window._undo_stack.index()
    window._edit_palette_action.click()

    assert window._doc.palette.colors == expected
    assert window._undo_stack.index() == steps + 1  # the whole run, one step
    assert window._workspace.current.palette_dirty
    assert "Grayscale at 50%" in window.statusBar().currentMessage()
    window._undo_stack.undo()
    assert window._doc.palette.colors == original.colors
    window._undo_stack.redo()
    assert window._doc.palette.colors == expected


def test_the_selected_color_scope_touches_one_entry(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_in_offset_mode(qtbot, tmp_path, monkeypatch)
    window._palette_panel._select(3)
    original = window._doc.palette

    def exec_(dialog: PaletteEditDialog) -> int:
        dialog.set_scope(EditScope.SELECTED)
        return ACCEPTED  # Invert

    monkeypatch.setattr(PaletteEditDialog, "exec", exec_)
    window._edit_palette_action.click()

    changed = [
        i
        for i, (a, b) in enumerate(
            zip(original.colors, window._doc.palette.colors, strict=True)
        )
        if a != b
    ]
    assert changed == [3]


def test_editing_the_default_palette_forks_custom_in_the_same_step(
    qtbot, tmp_path, monkeypatch
) -> None:
    """The fork a read-only source needs rides inside the edit's macro, so one
    undo takes the palette back to Default with its colours untouched."""
    window = _open(qtbot, tmp_path, monkeypatch)
    assert window._palette_mode == "default"
    original = list(window._doc.palette.colors)

    monkeypatch.setattr(PaletteEditDialog, "exec", lambda self: ACCEPTED)
    steps = window._undo_stack.index()
    window._edit_palette_action.click()

    assert window._palette_mode == "custom"
    assert window._doc.palette.colors[:16] == [
        PaletteOp(OpKind.INVERT).apply(c) for c in original[:16]
    ]
    assert window._undo_stack.index() == steps + 1
    window._undo_stack.undo()
    assert window._palette_mode == "default"
    assert list(window._doc.palette.colors) == original


def test_a_file_palettes_preview_and_edit_reach_the_graphic_showing_it(
    qtbot, tmp_path, monkeypatch
) -> None:
    """In File mode the colours belong to the PALETTE entry and the graphic only
    mirrors them, so both the preview and the commit have to cross over."""
    window = _open(qtbot, tmp_path, monkeypatch)
    pal = tmp_path / "colors.pal"
    pal.write_bytes(bytes((i * 7 + 2) & 0xFF for i in range(2 * 16)))
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(pal), ""))
    )
    window._open_palette()
    assert window._palette_mode == "file"
    owner = window._palette_owner_entry()
    assert owner.kind is EntryKind.PALETTE
    graphic = window._workspace.current
    original = list(owner.doc.palette.colors)
    inverted = [PaletteOp(OpKind.INVERT).apply(c) for c in original]
    seen: dict[str, object] = {}

    def exec_(dialog: PaletteEditDialog) -> int:
        dialog.set_scope(EditScope.ALL)
        seen["graphic"] = list(graphic.doc.palette.colors)
        return seen.pop("answer")

    monkeypatch.setattr(PaletteEditDialog, "exec", exec_)
    seen["answer"] = REJECTED
    window._edit_palette_action.click()
    assert seen["graphic"] == inverted  # the preview was mirrored onto the graphic
    assert list(graphic.doc.palette.colors) == original  # ...and taken back

    seen["answer"] = ACCEPTED
    window._edit_palette_action.click()
    assert list(owner.doc.palette.colors) == inverted
    assert list(graphic.doc.palette.colors) == inverted
    assert owner.palette_dirty and not graphic.palette_dirty
    window._undo_stack.undo()
    assert list(owner.doc.palette.colors) == original
    assert list(graphic.doc.palette.colors) == original


def test_with_nothing_open_the_button_is_dead(qtbot, monkeypatch) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    assert not window._edit_palette_action.isEnabled()
    opened: list[int] = []
    monkeypatch.setattr(PaletteEditDialog, "exec", lambda self: opened.append(1) or 0)
    window._edit_palette()  # the idle default has nowhere to put an edit
    assert not opened


# -- the toolbar's window: the swatches on the canvas ---------------------
SWATCHES = "preset.pixel.view-as-palette"


def _as_swatches(window: MainWindow) -> None:
    combo = window._pixel_preset
    combo.setCurrentIndex(combo.findData(SWATCHES))


def _changed(before: list[int], after: list[int]) -> list[int]:
    return [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]


def _swatch_colors(window: MainWindow, first: int, count: int) -> list[int]:
    """Each tile's first swatch colour, decoded fresh from the bytes."""
    return [tile.get(0, 0) for tile in window._decode_run(first, count)]


def test_the_toolbar_edit_palette_recolors_every_swatch_in_view_as_one_step(
    qtbot, tmp_path, monkeypatch
) -> None:
    """A 64-tile file is more than a page of swatches, so "entire view" and
    "the whole file" differ: the window's swatches change, the rest do not."""
    window = _open_in_offset_mode(qtbot, tmp_path, monkeypatch, tiles=64)
    assert not window._palette_view_action.isVisible()
    _as_swatches(window)
    assert window._palette_view_action.isVisible()
    assert window._edit_view_palette_action.isEnabled()
    total = window._doc.tile_count
    shown = min(window._window_slots(), total)
    assert 0 < shown < total
    before = _swatch_colors(window, 0, total)
    painted = window._canvas._image.copy()
    seen: dict[str, object] = {}

    def exec_(dialog: PaletteEditDialog) -> int:
        seen["scopes"] = list(dialog._scopes)
        seen["selection_offered"] = dialog._scopes[EditScope.SELECTION].isEnabled()
        dialog.set_op(OpKind.SET)
        dialog.set_color(0xFFFF0000)
        dialog.set_scope(EditScope.ENTIRE_VIEW)
        seen["previewed"] = window._canvas._image != painted
        return seen.pop("answer")

    monkeypatch.setattr(PaletteEditDialog, "exec", exec_)
    seen["answer"] = REJECTED
    window._edit_view_palette_action.click()
    assert seen["scopes"] == [EditScope.ENTIRE_VIEW, EditScope.SELECTION]
    assert seen["selection_offered"] is False
    assert seen["previewed"] is True
    assert window._canvas._image == painted  # Cancel took the preview down
    assert _swatch_colors(window, 0, total) == before

    seen["answer"] = ACCEPTED
    steps = window._undo_stack.index()
    window._edit_view_palette_action.click()
    after = _swatch_colors(window, 0, total)
    red = after[0]
    assert red != before[0] and (red >> 16) & 0xFF > 0xF0  # BGR555's red
    assert after[:shown] == [red] * shown
    assert after[shown:] == before[shown:]
    assert window._undo_stack.index() == steps + 1
    window._undo_stack.undo()
    assert _swatch_colors(window, 0, total) == before


def test_the_toolbar_edit_palette_can_recolor_only_the_selected_swatches(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_in_offset_mode(qtbot, tmp_path, monkeypatch, tiles=64)
    _as_swatches(window)
    window._on_slots_selected(2, 3)
    total = window._doc.tile_count
    before = _swatch_colors(window, 0, total)

    def exec_(dialog: PaletteEditDialog) -> int:
        assert dialog.scope() is EditScope.SELECTION  # the default with one
        dialog.set_op(OpKind.SET)
        dialog.set_color(0xFFFF0000)
        return ACCEPTED

    monkeypatch.setattr(PaletteEditDialog, "exec", exec_)
    window._edit_view_palette_action.click()

    after = _swatch_colors(window, 0, total)
    assert _changed(before, after) == [2, 3]
    assert after[2] == after[3]
