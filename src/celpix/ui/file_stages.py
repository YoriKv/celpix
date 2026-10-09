"""The Container / Reshape / Compression rows two dialogs share.

A whole file is read through three byte stages, and the two dialogs that settle
them — **New File…** for a file about to exist and **Edit File Container…**
for one that does — ask the same three questions in the same three pickers.
The rows live here once, with the rules both dialogs need: which containers
frame this kind of file, which rows a palette file has at all, and whether
every chosen stage can put bytes back the way it reads them.

Two things differ between the dialogs, and both are parameters rather than
subclasses. **What is offered**: a new file can only be made through stages
that write (``writable_only``), since a stage with no save half has nothing to
produce a file with, where an existing file may be read through any stage and
merely opens read-only. **What the rows say**: each dialog tips the container
row in its own words, so the pickers are exposed for it to do so.

A **palette** file has the container row only. Its colours are read without a
reshape or a decompressor — the palette half of a palette document takes the
file plain — so the other two rows are hidden for it rather than offered and
ignored, and hold the pass-through
(:class:`~celpix.project.entry.FileStages`).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QFormLayout, QLabel

from celpix.core.capabilities import ContentKind
from celpix.core.errors import Stage
from celpix.plugins.base import writes_back
from celpix.plugins.detect import (
    container_write_enabled,
    containers_for,
    stage_write_enabled,
)
from celpix.plugins.registry import Registry
from celpix.project.entry import FileStages
from celpix.ui.searchable_combo import (
    SearchableComboBox,
    fill_grouped,
    info_rows,
    plugin_rows,
)
from celpix.ui.widgets import PRESET_COMBO_WIDTH, add_form_row, signals_blocked

__all__ = ["FileStageRows"]

_CONTAINER_TIP = (
    "How the file is unwrapped before decoding: a header\n"
    "to skip, an interleave to undo, a wrapper to strip\n"
    "Raw binary file passes every byte through"
)

_RESHAPE_TIP = (
    "Byte reordering undone after the container, e.g. a\n"
    "plane-per-chip split or a ROM-pair word interleave\n"
    "Addresses and slices then count in the reordered bytes"
)

_COMPRESSION_TIP = (
    "Decompress the whole file with this scheme on load, and\n"
    "re-pack it on save - for a compressed blob extracted from\n"
    "a ROM entire. A structure inside a larger file is a slice"
)

# Why a file read through a stage with no save half opens read-only, per stage.
_VIEW_ONLY_NOTES = {
    Stage.CONTAINER: (
        "This container has no writer, so the file opens read-only.\n"
        "Saving plain bytes back would undo the unwrapping rather\n"
        "than reverse it, leaving the file corrupt."
    ),
    Stage.RESHAPE: (
        "This reshape has no unshape half, so the file opens\n"
        "read-only: without the inverse, saved bytes could not be\n"
        "returned to the places they came from."
    ),
    Stage.COMPRESSION: (
        "This compression has no compressor, so the file opens\n"
        "read-only: edited bytes could not be packed back into\n"
        "the stream they were unpacked from."
    ),
}

# The same fact, said of the size row it also fixes.
_SIZE_FIXED_REASONS = {
    Stage.CONTAINER: "This container cannot write, so the file's size is fixed.",
    Stage.RESHAPE: "This reshape cannot be undone, so the file's size is fixed.",
    Stage.COMPRESSION: "This compression cannot re-pack, so the file's size is fixed.",
}


class FileStageRows:
    """Three pickers over a file's byte stages, laid into a dialog's form.

    Not a widget: the dialog adds the rows where it wants them
    (:meth:`add_to`), fills them for a kind of file (:meth:`fill`), and reads
    the answer back as one value (:meth:`stages`). ``on_change`` is called on
    every move of any picker, which is when the dialog restates whatever
    depends on the stages — a read-only note, a byte size.
    """

    def __init__(
        self,
        registry: Registry,
        *,
        writable_only: bool,
        on_change: Callable[..., None],
    ) -> None:
        self._registry = registry
        self._writable_only = writable_only
        self.container = SearchableComboBox(PRESET_COMBO_WIDTH)
        self.container.setToolTip(_CONTAINER_TIP)
        self.reshape = SearchableComboBox(PRESET_COMBO_WIDTH)
        self.reshape.setToolTip(_RESHAPE_TIP)
        self.compression = SearchableComboBox(PRESET_COMBO_WIDTH)
        self.compression.setToolTip(_COMPRESSION_TIP)
        # Names of the containers on offer, kept unadorned so a dialog that
        # decorates a row (the container dialog's "(detected)" marker) can
        # re-apply the mark to a different row later.
        self.container_names: dict[str, str] = {}
        self._form: QFormLayout | None = None
        self._region_rows: list[QLabel] = []
        for combo in self.pickers:
            combo.currentIndexChanged.connect(on_change)

    @property
    def pickers(self) -> tuple[SearchableComboBox, ...]:
        return (self.container, self.reshape, self.compression)

    def add_to(self, form: QFormLayout) -> None:
        """Lay the three rows into ``form``, in pipeline order."""
        self._form = form
        add_form_row(form, "Container:", self.container)
        for caption, combo in (
            ("Reshape:", self.reshape),
            ("Compression:", self.compression),
        ):
            self._region_rows.append(add_form_row(form, caption, combo))

    def fill(self, kind: ContentKind, stages: FileStages) -> None:
        """Offer the stages that frame a ``kind`` of file, selecting ``stages``.

        Only the containers that frame this kind: offering a palette the
        wrappers that unwrap ROMs would be inviting a choice that cannot come out
        well, and the two sets do not overlap. A palette gets the container row
        alone (module docstring); the other two rows are hidden, holding the
        pass-through so :meth:`stages` reads as it would for shown ones.

        Signals stay blocked across the refill, and the dialog refreshes once
        after it: a fill emits a change per item, and a heading is briefly
        current before the first real row displaces it, so a live handler would
        be asked to size a file against a category name.
        """
        offered = [
            info
            for info in containers_for(self._registry, kind)
            if not self._writable_only
            or container_write_enabled(self._registry, info.id)
        ]
        self.container_names = {info.id: info.name for info in offered}
        with signals_blocked(self.container):
            fill_grouped(self.container, info_rows(offered), stages.container_id)
        region = kind is not ContentKind.PALETTE
        for stage, combo, wanted in (
            (Stage.RESHAPE, self.reshape, stages.reshape_id),
            (Stage.COMPRESSION, self.compression, stages.compression_id),
        ):
            plugins = [
                plugin
                for plugin in self._registry.plugins(stage)
                if not self._writable_only or writes_back(plugin, stage)
            ]
            with signals_blocked(combo):
                fill_grouped(combo, plugin_rows(plugins), wanted)
        if self._form is not None:
            for row in self._region_rows:
                self._form.setRowVisible(row, region)

    def stages(self) -> FileStages:
        """What the pickers hold, as one value."""
        return FileStages(
            self.container.currentData(),
            self.reshape.currentData(),
            self.compression.currentData(),
        )

    def view_only_stage(self) -> Stage | None:
        """The first chosen stage with no save half, or ``None`` when every one
        can put bytes back the way it reads them.

        A missing reshape or compression answers as writable here, where a
        missing *container* does not (:func:`container_write_enabled`): an id
        this registry lacks isn't this note's problem, and the entry's open
        reports it. Always ``None`` under ``writable_only``, since nothing else
        was offered.
        """
        chosen = self.stages()
        if not container_write_enabled(self._registry, chosen.container_id):
            return Stage.CONTAINER
        for stage, plugin_id in (
            (Stage.RESHAPE, chosen.reshape_id),
            (Stage.COMPRESSION, chosen.compression_id),
        ):
            if not stage_write_enabled(self._registry, stage, plugin_id, missing=True):
                return stage
        return None

    def view_only_note(self) -> str:
        """Why the file would open read-only, or ``""`` where it would not."""
        stage = self.view_only_stage()
        return _VIEW_ONLY_NOTES[stage] if stage is not None else ""

    def size_fixed_reason(self) -> str:
        """Why the file's size could not be changed through these stages, or
        ``""`` where it could — the same fact as :meth:`view_only_note`, said
        of the size row."""
        stage = self.view_only_stage()
        return _SIZE_FIXED_REASONS[stage] if stage is not None else ""
