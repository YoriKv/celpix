"""The Inputs window: what one entry hands its plugins from outside its own bytes.

A stage plugin may declare **inputs** — a code table shared by forty streams, a
sprite mapping's parallel arrays, an output size kept in a length table
(:class:`~celpix.plugins.base.InputSpec`) — and this is where an entry binds
them (``docs/design/plugin-inputs.md`` §6). The form is **generated** from
the declarations: one section per stage of the entry whose plugin declares any,
one row per declared input, built from the spec's kind. A codec with five
region inputs and one with a table and a size get the same editor with no UI
code of their own.

A non-modal ``Qt.Tool`` window, like the font alphabet and subsprite windows,
and **pinned to the entry it was opened for** rather than following the current
one: the point of it being non-modal is that the user goes and selects the
table in the parent while it is open, and *Use selection* reads that selection
back. (The one exception is the Slice dialog's badge, which opens it blocking —
see :meth:`InputsWindow.show_for`.) Escape closes it either way: a ``QWidget``
inherits none of ``QDialog``'s key handling, so the gesture is spelled out. The
main window supplies what the rows cannot know — the entries a region may name,
what the selection on screen is, whether a binding resolves — through the
callbacks handed to :meth:`InputsWindow.show_for`; nothing here reads a file or
touches the workspace.

Offsets and lengths follow the app-wide address-box convention
(:func:`~celpix.core.address.parse_hex`): bare digits are hex, ``$`` and ``0x``
accepted. A row left blank is **unbound**, which for a required input is what
the status mark and the notice then say.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from celpix.core.errors import Stage
from celpix.project.inputs import Bindings, InputBinding, StageInputs
from celpix.project.workspace import Entry
from celpix.ui.input_rows import InputRow, IntegerRow, row_for

__all__ = ["InputsWindow", "Section"]

# What each stage's section is captioned, in the words the toolbar uses.
_STAGE_CAPTIONS = {
    Stage.COMPRESSION: "Compression",
    Stage.INTERPRET_TILEMAP: "Tilemap",
    Stage.INTERPRET_PIXEL: "Pixel",
    Stage.INTERPRET_PALETTE: "Palette",
}

#: One section of the window: a stage's declarations, the plugin's display
#: name, and what the entry currently binds for it.
Section = tuple[StageInputs, str, Bindings]

#: ``(plugin id, key) -> why it does not resolve``; a key that resolves is absent.
Problems = dict[tuple[str, str], str]


class InputsWindow(QWidget):
    """Floating editor for one entry's input bindings."""

    #: ``(plugin id -> bindings, apply to the selection)``: the user pressed Apply.
    apply_requested = Signal(object, bool)
    #: A row asked for the selection on screen; the main window answers by
    #: calling :meth:`fill_from_selection` with what it found.
    use_selection_requested = Signal(object)
    #: A row asked to show its source — a RegionBinding or IntegerFromBytes.
    go_to_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle("Inputs")
        self._entry: Entry | None = None
        self._rows: dict[tuple[str, str], InputRow] = {}
        self._validate: Callable[[dict[str, Bindings]], Problems] | None = None
        self._read_values: Callable[[dict[str, Bindings]], dict] | None = None

        outer = QVBoxLayout(self)
        self._sections = QVBoxLayout()
        outer.addLayout(self._sections)
        self._sections_widgets: list[QWidget] = []

        scope = QHBoxLayout()
        scope_caption = QLabel("Apply to:")
        scope_caption.setToolTip("Which entries Apply writes these inputs to")
        scope.addWidget(scope_caption)
        self._this = QRadioButton("this entry")
        self._this.setChecked(True)
        self._selected = QRadioButton("the selected entries")
        self._selected.setToolTip(
            "Every selected Files entry whose plugin declares\n"
            "the same inputs. Unchanged fields stay as they are\n"
            "on each entry"
        )
        scope.addWidget(self._this)
        scope.addWidget(self._selected)
        scope.addStretch(1)
        self._scope_row = QWidget()
        self._scope_row.setLayout(scope)
        outer.addWidget(self._scope_row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._apply = QPushButton("Apply")
        self._apply.setDefault(True)
        self._apply.clicked.connect(self._on_apply)
        close = QPushButton("Close")
        close.clicked.connect(self.hide)
        buttons.addWidget(self._apply)
        buttons.addWidget(close)
        outer.addLayout(buttons)

    # -- populating ------------------------------------------------------------
    @property
    def entry(self) -> Entry | None:
        """The entry this window is pinned to, or ``None`` while hidden."""
        return self._entry

    def show_for(
        self,
        entry: Entry,
        sections: list[Section],
        sources: list[Entry],
        *,
        selected_count: int,
        validate: Callable[[dict[str, Bindings]], Problems],
        read_values: Callable[[dict[str, Bindings]], dict],
        blocking: bool = False,
    ) -> None:
        """Rebuild the form for ``entry`` and show it.

        ``sections`` is what the entry's stages declare and currently bind;
        ``sources`` the entries a region may name (already filtered by the
        one-hop rule); ``validate`` answers, for a candidate binding map, which
        keys do not resolve and why; ``read_values`` what each From-bytes
        integer currently reads, keyed like the problems.

        ``blocking`` is for the one caller that is itself a modal dialog — the
        Slice dialog's inputs badge. A window merely stacked above a modal one is
        drawn and then ignores every click, so the editor has to take the top of
        the modal stack itself; the price is that *Use selection* can only read
        the selection already on screen, since the main window is blocked too.
        """
        self._set_blocking(blocking)
        self._entry = entry
        self._validate = validate
        self._read_values = read_values
        self.setWindowTitle(f"Inputs - {entry.name}")
        for widget in self._sections_widgets:
            self._sections.removeWidget(widget)
            widget.deleteLater()
        self._sections_widgets = []
        self._rows = {}
        for declared, plugin_name, bindings in sections:
            box = QGroupBox(
                f"{_STAGE_CAPTIONS.get(declared.stage, declared.stage.value)}  "
                f"{plugin_name}"
            )
            grid = QGridLayout(box)
            for line, spec in enumerate(declared.specs):
                row = row_for(spec, sources)
                row.set_binding(bindings.get(spec.key))
                row.changed.connect(self._revalidate)
                row.use_selection_requested.connect(self.use_selection_requested.emit)
                row.go_to_requested.connect(self.go_to_requested.emit)
                label = QLabel(spec.label)
                label.setToolTip(spec.tooltip or spec.label)
                grid.addWidget(label, line, 0)
                cells = QHBoxLayout()
                cells.setContentsMargins(0, 0, 0, 0)
                for widget in row.widgets():
                    cells.addWidget(widget)
                cells.addStretch(1)
                holder = QWidget()
                holder.setLayout(cells)
                grid.addWidget(holder, line, 1)
                grid.addWidget(row.status_widget(), line, 2)
                self._rows[(declared.plugin_id, spec.key)] = row
            self._sections.addWidget(box)
            self._sections_widgets.append(box)
        self._scope_row.setVisible(selected_count > 1)
        self._selected.setText(f"the {selected_count} selected entries")
        self._this.setChecked(True)
        self._revalidate()
        self.show()
        self.raise_()
        self.activateWindow()

    def _set_blocking(self, blocking: bool) -> None:
        """Take (or give back) the top of the modal stack — see :meth:`show_for`.

        Qt only reads a modality change on a window that is hidden, so a window
        already up for another entry is dropped first; it is rebuilt and shown
        again either way.
        """
        wanted = (
            Qt.WindowModality.ApplicationModal
            if blocking
            else Qt.WindowModality.NonModal
        )
        if self.windowModality() != wanted:
            self.hide()
            self.setWindowModality(wanted)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # Qt override
        """Escape closes it, the way it closes every other window in the app.

        A plain ``QWidget`` inherits none of ``QDialog``'s key handling, so the
        one gesture a floating editor has to answer had to be spelled out. It is
        the Close button's move rather than a Cancel: nothing typed here is live
        until Apply, so closing discards the unapplied rows either way — and it
        is the only way out of the window while it is blocking a Slice dialog
        that has taken the keyboard's other exits.
        """
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def hide_overlay(self) -> None:
        """Hide — the entry it was pinned to is gone."""
        self._entry = None
        if self.isVisible():
            self.hide()

    # -- what the form says ------------------------------------------------------
    def bindings(self) -> dict[str, Bindings]:
        """Every bound row, by plugin id then key; unbound rows are absent."""
        out: dict[str, Bindings] = {}
        for (plugin_id, key), row in self._rows.items():
            binding = row.binding()
            if binding is not None:
                out.setdefault(plugin_id, {})[key] = binding
        return out

    def fill_from_selection(self, row: InputRow, binding: InputBinding | None) -> None:
        """Answer a *Use selection* request: ``None`` means nothing usable was
        selected, and the row is left alone."""
        if binding is None:
            return
        row.set_binding(binding)
        self._revalidate()

    def _revalidate(self) -> None:
        # Both set together by :meth:`show_for`; unset only before the first one.
        if self._validate is None or self._read_values is None:
            return
        candidate = self.bindings()
        problems = self._validate(candidate)
        values = self._read_values(candidate)
        for key, row in self._rows.items():
            row.set_problem(problems.get(key))
            if isinstance(row, IntegerRow):
                row.set_read_value(values.get(key))

    def _on_apply(self) -> None:
        self.apply_requested.emit(self.bindings(), self._selected.isChecked())
