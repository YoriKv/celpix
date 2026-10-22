"""Edit Subsprites on a ``fixed`` layout: pieces at slots the drawing routine
places, editable in their cell fields alone (``docs/design/sprite-map.md``
§8.4, §10.1, §10.3).

Driven by a test-local format rather than a shipped one, so the test states the
kind's rule and nothing about where a real pose table keeps its bits."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from celpix.core.capabilities import ContentKind
from celpix.core.errors import Stage
from celpix.core.sprite import Subsprite, SubspriteLimits
from celpix.core.sprite_edit import fixed_refusal
from celpix.core.tilemap import Cell
from celpix.pipeline import pipeline
from celpix.plugins import FormatInfo
from celpix.plugins.formats import adapt_format
from celpix.plugins.registry import default_registry
from celpix.project.workspace import TileMode, TileSource
from celpix.ui.main_window import MainWindow
from uihelpers import _make_snes_file

_FIXED_LIMITS = SubspriteLimits("fixed", range(0), range(0))
# The format declares no flip bit, so the refusal must not promise one.
_REFUSAL = fixed_refusal(flips=False)


class _PoseTable:
    """One frame of little-endian tile words, piece ``k`` drawn at ``(8k, 0)``
    by a routine the record does not describe: the word's low byte is the tile,
    its high byte opaque flags."""

    info = FormatInfo(
        id="format.tilemap.test-fixed",
        name="Fixed pose",
        declares={"layout": "sprite", "subsprite_size": "stated"},
    )

    def decode(self, data, ctx):  # noqa: ANN001, ANN201
        words = [
            int.from_bytes(data[at : at + 2], "little")
            for at in range(0, len(data) - 1, 2)
        ]
        return [Cell(index=word & 0xFF, flags=word >> 8) for word in words]

    def encode(self, cells, ctx):  # noqa: ANN001, ANN201
        return b"".join(
            ((c.flags & 0xFF) << 8 | (c.index & 0xFF)).to_bytes(2, "little")
            for c in cells
        )

    def bytes_per_cell(self) -> int:
        return 2

    def cell_tiles(self) -> tuple[int, int]:
        return (1, 1)

    def index_limit(self) -> int:
        return 0xFF

    def cell_fields(self) -> dict[str, int]:
        return {"index": 0xFF}

    def frames(self, cells, ctx):  # noqa: ANN001, ANN201
        return [
            tuple(
                Subsprite(x=8 * k, y=0, index=cell.index, record=k)
                for k, cell in enumerate(cells)
            )
        ]

    def subsprite_limits(self, ctx):  # noqa: ANN001, ANN201
        return _FIXED_LIMITS

    def place_subsprite(self, cell, ctx, *, x=None, y=None, size=None, drawn=None):  # noqa: ANN001, ANN201
        if any(value is not None for value in (x, y, size, drawn)):
            raise ValueError("the drawing routine places these pieces")
        return cell


class _Picky(_PoseTable):
    """Refuses a tile past $08 on encode, as a format whose last usable tile
    depends on its context does — past what ``index_limit`` can say."""

    info = FormatInfo(
        id="format.tilemap.test-fixed-picky",
        name="Picky pose",
        declares={"layout": "sprite", "subsprite_size": "stated"},
    )

    def encode(self, cells, ctx):  # noqa: ANN001, ANN201
        late = next((c.index for c in cells if c.index > 8), None)
        if late is not None:
            raise ValueError(f"tile ${late:X} is past the last")
        return super().encode(cells, ctx)


class _TwiceDrawn(_PoseTable):
    """Each word drawn as two adjacent 8x8 pieces at ``(16k, 0)``, in frame 0
    and again in frame 1 — the shape of a vehicle word drawn as several pieces
    and a pose drawn again in a later frame, at once."""

    info = FormatInfo(
        id="format.tilemap.test-fixed-twice",
        name="Fixed pose, drawn twice",
        declares={"layout": "sprite", "subsprite_size": "stated"},
    )

    def frames(self, cells, ctx):  # noqa: ANN001, ANN201
        frame = tuple(
            Subsprite(x=16 * k + 8 * half, y=0, index=cell.index, record=k)
            for k, cell in enumerate(cells)
            for half in (0, 1)
        )
        return [frame, frame]


def _fixed(qtbot, tmp_path, codec: _PoseTable | None = None) -> MainWindow:
    """A window on a three-piece pose bound to a bank, Edit Subsprites armed."""
    codec = codec or _PoseTable()
    registry = default_registry()
    engine, preset = adapt_format(codec, Stage.INTERPRET_TILEMAP)
    registry.register(engine)
    registry.register_preset(preset)
    window = MainWindow(registry=registry)
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    path = tmp_path / "pose.bin"
    path.write_bytes(_PoseTable().encode([Cell(index=t) for t in (1, 2, 3)], None))
    window._load_pixel(str(path))
    entry = window._workspace.current
    entry.content_kind = ContentKind.TILEMAP
    entry.tilemap_preset_id = codec.info.id
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    entry.doc = None  # re-read as the pose table
    window._activate_entry(window._workspace.entries[0])
    window._activate_entry(entry)
    assert window._doc.is_sprite and window._doc.cells_editable
    window._stamp_action.trigger()
    assert window._stamping
    return window


def _tiles(window) -> list[int]:
    return [sub.index for sub in window._doc.shown_frames[0]]


def _status(window) -> str:
    return window.statusBar().currentMessage()


def test_the_canvas_picks_but_moves_and_adds_nothing(qtbot, tmp_path) -> None:
    """The tool arms on the gate the limits open, under its own tip. A left drag
    on a piece picks it and lifts nothing; the arrows, Ctrl+D and Alt+Up keep
    their keys and say why; a click on empty sheet with a tile held adds
    nothing."""
    window = _fixed(qtbot, tmp_path)
    tip = window._stamp_action.toolTip()
    assert "places them itself" in tip and "flip" not in tip
    cells, steps = list(window._doc.cells), window._undo_stack.count()
    ox, oy = window._slot_origin(window._sprite_sheet(), 0)
    canvas = window._canvas
    canvas.stamp_pixel_pressed.emit(ox + 9, oy + 1, Qt.MouseButton.LeftButton)
    assert window._picked_records == [1] and window._subsprite_drag is None
    window._on_subsprite_moved(ox + 20, oy + 3, Qt.KeyboardModifier.NoModifier)
    window._on_subsprite_released(ox + 20, oy + 3, Qt.KeyboardModifier.NoModifier)
    assert window._picked_records == [1] and window._doc.cells == cells

    window._sync_subsprite_actions()
    assert window._subsprite_forward_action.isVisible()
    assert not window._subsprite_forward_action.isEnabled()
    for key, mods in (
        (Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier),
        (Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier),
        (Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier),
    ):
        window.statusBar().clearMessage()
        event = QKeyEvent(QEvent.Type.KeyPress, key, mods)
        assert window._handle_nav_key(event)
        assert _status(window) == _REFUSAL
    window._mirror_about_origin(horizontal=True)
    assert _status(window) == _REFUSAL
    assert window._doc.cells == cells and window._undo_stack.count() == steps

    window._set_source_tile(5, None)
    window._set_picked_records([])
    canvas.stamp_pixel_pressed.emit(ox + 4, oy + 12, Qt.MouseButton.LeftButton)
    assert _status(window) == _REFUSAL and window._doc.cells == cells


def test_the_row_retiles_and_the_clipboard_keeps_only_copy(qtbot, tmp_path) -> None:
    """No X/Y/Size/Drawn controls grow; the Cell spin retiles the picked piece
    and the encode carries it. Copy stays with a pick, while Cut, Clear and
    Paste - which add or remove a piece - are disabled and refuse if reached."""
    window = _fixed(qtbot, tmp_path)
    assert window._record_props_widgets == {}
    window._set_picked_records([2])
    assert window._cell_index.isEnabled() and window._cell_index.value() == 3
    window._cell_index.setValue(9)
    assert _tiles(window) == [1, 2, 9]
    doc = window._doc
    encoded = pipeline.encode_cells(
        doc.settled_cells,
        doc.tilemap_config.interpret_preset_id,
        window._registry,
        doc.tilemap_ctx,
    )
    assert encoded == _PoseTable().encode([Cell(index=t) for t in (1, 2, 9)], None)

    window._sync_edit_actions()
    assert window._copy_action.isEnabled()
    assert window._copy_subsprites() and window._has_cell_clipboard()
    window._sync_edit_actions()
    for action in (window._cut_action, window._clear_action, window._paste_action):
        assert not action.isEnabled()
    cells = list(doc.cells)
    for verb in (window._cut_cells, window._clear_cells, window._paste_cells):
        window.statusBar().clearMessage()
        verb()
        assert _status(window) == _REFUSAL and window._doc.cells == cells


def test_a_record_drawn_in_several_places_reads_where_it_was_clicked(
    qtbot, tmp_path
) -> None:
    """A press on a record's second piece in frame 1 describes frame 1 and says
    where else it is drawn; the outline marks all four places, the Subsprites
    square follows the clicked frame, and the Cell spin retiles the one word."""
    window = _fixed(qtbot, tmp_path, _TwiceDrawn())
    window._show_subsprites()
    try:
        ox, oy = window._slot_origin(window._sprite_sheet(), 1)
        window._canvas.stamp_pixel_pressed.emit(
            ox + 9, oy + 1, Qt.MouseButton.LeftButton
        )
        assert window._picked_records == [0] and window._pick_frame == 1
        status = _status(window)
        assert status.startswith("Frame 1, subsprite 0 of 6")
        assert "one of 2 pieces of this record, also drawn in frame 0" in status
        assert len(window._subsprite_rects()) == 4
        assert window._subsprites._panel._marked == (1, 0)

        window._cell_index.setValue(9)
        assert [cell.index for cell in window._doc.cells] == [9, 2, 3]
        # One record, four pieces on screen: the line says what else moved.
        assert _status(window) == (
            "Pointed 1 subsprite at tile $9 (2 pieces, also drawn in frame 0)."
        )
        assert [sub.index for sub in window._doc.shown_frames[1]] == [9, 9, 2, 2, 3, 3]
        assert window._picked_records == [0] and window._pick_frame == 1
    finally:
        window._subsprites.hide()


def test_a_refused_retile_says_the_codec_s_reason_and_puts_the_spin_back(
    qtbot, tmp_path
) -> None:
    """The trial encode's refusal reaches the status line as the format's own
    sentence, with no stage bracket, and the Cell spin returns to the tile the
    piece still holds."""
    window = _fixed(qtbot, tmp_path, _Picky())
    window._set_picked_records([2])
    cells, steps = list(window._doc.cells), window._undo_stack.count()
    window._cell_index.setValue(9)
    assert _status(window) == "Picky pose: tile $9 is past the last"
    assert window._cell_index.value() == 3
    assert window._doc.cells == cells and window._undo_stack.count() == steps
