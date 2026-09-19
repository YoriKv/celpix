"""Plugin inputs on the window: the badge, the Inputs window, and the apply.

What a stage plugin needs from outside its own bytes is declared by the plugin
and bound per entry (``docs/design/plugin-inputs.md``); this is the UI
over those bindings. Three surfaces, and one apply:

- **The badge** beside a codec picker on the codecs toolbar, present only when
  that picker's plugin declares inputs, wearing the stage's status — the inputs
  glyph when everything resolves, the warning mark when something does not —
  and opening the window on a click. Beside *Tilemap* it speaks for the entry's
  cell engine. Beside *Compression* it speaks for the **preview** codec, bound
  on the **file** on screen: that is what the overlay decodes with, what Scan
  hunts with, and what a slice carved under that codec inherits.
- **The Inputs window** (:mod:`celpix.ui.inputs_window`), non-modal and pinned
  to the entry it was opened on, reached from the badge, the Files pane and the
  File menu. Its *Use selection* reads the selection of whatever entry is on
  screen, so the user goes to the parent, selects the table, and comes back.
- **The same badge on the Slice dialog**, beside that dialog's own Compression
  picker, so a slice is bound under the codec being chosen for it rather than
  only after it exists. It opens the window blocking, on the dialog's codec
  (:meth:`~InputsMixin._edit_slice_inputs`); *Use selection* there can only read
  the selection already on screen, since the modal dialog blocks the main window.
- **Copy Inputs / Paste Inputs** on the Files pane, for the forty slices that
  share one table.

The apply is one :class:`~celpix.ui.undo_commands.InputsEditCommand` however
many entries it lands on. A slice or map is **re-read** — its inputs decide what
its bytes decode to — after the same unsaved-edits warning Edit Slice gives; a
file's bindings feed only its preview, so that one re-runs the overlay.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import QApplication, QMessageBox, QToolButton

from celpix.core.errors import Stage
from celpix.plugins.base import NO_COMPRESSION
from celpix.project import projectfile
from celpix.project.inputs import (
    Bindings,
    InputBinding,
    IntegerFromBytes,
    RegionBinding,
    ResolvedInputs,
    StageInputs,
    can_supply_input,
    declared_inputs,
    input_specs,
    iter_bindings,
    names_entry,
    resolve_inputs,
    with_bindings,
)
from celpix.project.workspace import Entry, EntryKind
from celpix.ui import clipboard
from celpix.ui.glyphs import Glyph
from celpix.ui.icon_font import glyph_icon
from celpix.ui.inputs_window import InputsWindow, Problems
from celpix.ui.theme import WARNING_INK
from celpix.ui.undo_commands import InputsEditCommand
from celpix.ui.widgets import counted

# The widest integer *Use selection* reads: the resolver's own limit.
_MAX_SELECTED_INT = 8


class InputsMixin:
    """The badge, the window and the apply for plugin inputs.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    # -- construction ------------------------------------------------------------
    def _make_inputs_badge(self, tip: str) -> QToolButton:
        """One badge: a flat tool button wearing the inputs glyph, hidden until
        the picker beside it names a plugin with something to bind."""
        badge = QToolButton()
        badge.setAutoRaise(True)
        badge.setToolTip(tip)
        badge.hide()
        return badge

    def _build_inputs_window(self) -> None:
        self._inputs_window = InputsWindow(self)
        #: What the open window was opened *with* — see the note in
        #: :meth:`_show_inputs` about why an Apply has to rebuild the same form.
        self._inputs_context: tuple[str | None, bool] = (None, False)
        self._inputs_window.apply_requested.connect(self._on_inputs_apply)
        self._inputs_window.use_selection_requested.connect(
            self._on_inputs_use_selection
        )
        self._inputs_window.go_to_requested.connect(self._go_to_binding)

    def _bake_inputs_badges(self) -> None:
        """Stamp both badges in the theme's colours: the inputs glyph in the
        button-text ink, or the warning mark in the warning ink."""
        for badge, problem in (
            (
                self._tilemap_inputs_badge,
                self._badge_problem(self._tilemap_inputs_badge),
            ),
            (
                self._compression_inputs_badge,
                self._badge_problem(self._compression_inputs_badge),
            ),
        ):
            badge.setIcon(
                glyph_icon(
                    Glyph.EXCLAMATION if problem else Glyph.INPUTS,
                    QApplication.palette(),
                    ratio=self.devicePixelRatioF(),
                    color=WARNING_INK if problem else None,
                )
            )

    @staticmethod
    def _badge_problem(badge: QToolButton) -> bool:
        return bool(badge.property("problem"))

    # -- what an entry declares ----------------------------------------------------
    def _preview_codec_inputs(self, entry: Entry) -> StageInputs | None:
        """What the compression *preview* wants from ``entry`` (a file): the
        codec on the toolbar's declarations, or ``None`` for none."""
        if entry.kind is not EntryKind.FILE:
            return None
        codec = self._compression_id()
        if codec == NO_COMPRESSION:
            return None
        specs = input_specs(self._registry, Stage.COMPRESSION, codec)
        return StageInputs(Stage.COMPRESSION, codec, specs) if specs else None

    def _inputs_sections(
        self, entry: Entry, codec: str | None = None
    ) -> list[StageInputs]:
        """Every stage of ``entry`` with inputs to bind: its own codecs', plus
        the preview's on a file that is on screen.

        ``codec`` names a compression scheme to bind **instead** of whatever the
        entry decompresses with today — the Slice dialog's picker, which may have
        been moved and not yet OK'd. Binding under the codec being *chosen* is
        the point of that dialog's badge, so the entry's own compression section
        (and the preview's, which describes the toolbar rather than the dialog)
        steps aside for it.
        """
        if codec is not None:
            sections = [
                s
                for s in declared_inputs(entry, self._registry)
                if s.stage is not Stage.COMPRESSION
            ]
            specs = input_specs(self._registry, Stage.COMPRESSION, codec)
            if specs:
                sections.append(StageInputs(Stage.COMPRESSION, codec, specs))
            return sections
        sections = declared_inputs(entry, self._registry)
        if entry is self._workspace.current:
            preview = self._preview_codec_inputs(entry)
            if preview is not None:
                sections.append(preview)
        return sections

    def _inputs_available(self, entry: Entry | None) -> bool:
        """Whether Inputs… has anything to show for ``entry`` — the gate on
        every way into the window."""
        return entry is not None and bool(self._inputs_sections(entry))

    def _resolved_for(self, entry: Entry, declared: StageInputs) -> ResolvedInputs:
        return resolve_inputs(
            entry, declared.stage, declared.plugin_id, self._registry, self._workspace
        )

    def _preview_inputs(self, entry: Entry, codec: str) -> ResolvedInputs | None:
        """The preview codec's inputs resolved on ``entry``, the file on screen,
        or ``None`` when the codec declares none — the overlay's and the scan's
        one question."""
        specs = input_specs(self._registry, Stage.COMPRESSION, codec)
        if not specs:
            return None
        return resolve_inputs(
            entry, Stage.COMPRESSION, codec, self._registry, self._workspace
        )

    # -- the badges ----------------------------------------------------------------
    def _sync_inputs_badges(self) -> None:
        """Show each badge only when its picker's plugin declares inputs, wearing
        whether they resolve; and arm the File menu's row on the same answer."""
        entry = self._workspace.current
        tilemap = compression = None
        if entry is not None and entry.doc is not None:
            for declared in declared_inputs(entry, self._registry):
                if declared.stage is Stage.INTERPRET_TILEMAP:
                    tilemap = declared
            compression = self._preview_codec_inputs(entry)
        changed = False
        for badge, declared in (
            (self._tilemap_inputs_badge, tilemap),
            (self._compression_inputs_badge, compression),
        ):
            if declared is None:
                badge.hide()
                continue
            resolved = self._resolved_for(entry, declared)
            problem = not resolved.ok
            if self._badge_problem(badge) != problem:
                badge.setProperty("problem", problem)
                changed = True
            badge.setToolTip(
                "\n".join(p.detail for p in resolved.problems)
                if problem
                else f"Inputs for {self._plugin_name(declared)}: all bound\n"
                "Click to edit"
            )
            badge.show()
        if changed:
            self._bake_inputs_badges()
        self._inputs_action.setEnabled(self._inputs_available(entry))
        self._sync_inputs_window()

    def _plugin_name(self, declared: StageInputs) -> str:
        try:
            return self._registry.plugin(declared.stage, declared.plugin_id).info.name
        except KeyError:
            return declared.plugin_id

    # -- the window ----------------------------------------------------------------
    def _inputs_current(self) -> None:
        """File ▸ Inputs…: the window on the entry on screen."""
        entry = self._workspace.current
        if entry is not None:
            self._show_inputs(entry)

    def _edit_slice_inputs(self, _dialog, entry: Entry, codec: str) -> None:  # noqa: ANN001 - a SliceDialog
        """The Slice dialog's inputs badge: the editor, over the modal dialog.

        Opened **blocking** so it sits above the dialog that asked for it, and on
        the dialog's own codec (see :meth:`_inputs_sections`). ``entry`` is what
        the bindings land on: the slice being edited, or — for New Slice — the
        parent file, whose bindings for that codec the new slice inherits
        (:func:`~celpix.project.workspace.slice_of`), which is why binding them
        here and then pressing OK arrives at a slice already bound.
        """
        self._show_inputs(entry, codec=codec, blocking=True)

    def _show_inputs(
        self, entry: Entry, *, codec: str | None = None, blocking: bool = False
    ) -> None:
        """Open (or re-pin) the Inputs window on ``entry``."""
        sections = self._inputs_sections(entry, codec)
        if not sections:
            self.statusBar().showMessage(
                f"{entry.name}: none of its formats needs anything from outside "
                "its bytes."
            )
            return
        selected = [
            e for e in self._files_panel.selected_entries() if self._inputs_available(e)
        ]
        if entry not in selected:
            selected = [entry]
        self._inputs_window.show_for(
            entry,
            [
                (
                    declared,
                    self._plugin_name(declared),
                    entry.inputs.get(declared.plugin_id, {}),
                )
                for declared in sections
            ],
            [e for e in self._workspace.entries if can_supply_input(entry, e)],
            selected_count=len(selected),
            validate=lambda candidate: self._inputs_problems(
                entry, sections, candidate
            ),
            read_values=lambda candidate: self._inputs_read_values(
                entry, sections, candidate
            ),
            blocking=blocking,
        )
        # An Apply rebuilds the form, and has to rebuild *this* one: the codec
        # override and the blocking are the window's, not the entry's, and
        # re-deriving them from the entry would swap the section under the user
        # and drop the window back beneath the dialog it was opened from.
        self._inputs_context = (codec, blocking)

    def _inputs_problems(
        self, entry: Entry, sections: list[StageInputs], candidate: dict[str, Bindings]
    ) -> Problems:
        """Which of ``candidate``'s keys would not resolve on ``entry``, and why.

        Asked of a *copy* of the entry carrying the candidate map, so the live
        entry is not touched while the user types.
        """
        trial = replace(entry, inputs=candidate)
        problems: Problems = {}
        for declared in sections:
            for problem in self._resolved_for(trial, declared).problems:
                problems[(declared.plugin_id, problem.key)] = problem.detail
        return problems

    def _inputs_read_values(
        self, entry: Entry, sections: list[StageInputs], candidate: dict[str, Bindings]
    ) -> dict[tuple[str, str], int]:
        """What each integer bound *from bytes* currently reads, for the hint."""
        trial = replace(entry, inputs=candidate)
        values: dict[tuple[str, str], int] = {}
        for declared in sections:
            resolved = self._resolved_for(trial, declared)
            for key, value in resolved.values.items():
                if isinstance(value, int):
                    values[(declared.plugin_id, key)] = value
        return values

    # -- use selection / go to ------------------------------------------------------
    def _on_inputs_use_selection(self, row) -> None:  # noqa: ANN001 — an inputs_window row
        target = self._inputs_window.entry
        if target is None:
            return
        binding = self._selection_as_binding(target)
        if binding is not None and row.spec.kind.value == "integer":
            binding = IntegerFromBytes(
                entry=binding.entry,
                offset=binding.offset,
                width=max(1, min(_MAX_SELECTED_INT, binding.length)),
            )
        if binding is None:
            self.statusBar().showMessage(
                "Select bytes in the target's own file, or in an entry it may read."
            )
        self._inputs_window.fill_from_selection(row, binding)

    def _selection_as_binding(self, target: Entry) -> RegionBinding | None:
        """The on-screen selection as a region binding for ``target``.

        The entry on screen is either the file ``target``'s bytes are in — then
        the binding is *This file* at the selection's absolute offset, the
        coordinates a slice offset is written in — or some entry ``target`` may
        read under the one-hop rule, then an entry binding into its resolved
        bytes. Anything else, or no selection, is ``None``.
        """
        shown = self._workspace.current
        if shown is None or self._doc is None:
            return None
        span = self._selection_byte_range()
        if span is None:
            return None
        start, length = span
        own_file = shown is target or (
            shown.kind is EntryKind.FILE
            and target.kind is EntryKind.SLICE
            and self._workspace.path_key(shown.path)
            == self._workspace.path_key(target.path)
        )
        if own_file:
            if not self._doc.pixel_config.positions_are_slice_offsets:
                return None  # a decompressed view names no file position
            return RegionBinding(offset=self._anchor_base() + start, length=length)
        if can_supply_input(target, shown):
            return RegionBinding(entry=shown, offset=start, length=length)
        return None

    def _go_to_binding(self, binding: InputBinding) -> None:
        """Show a binding's source with its bytes in view — Jump to Source's
        move, for a table rather than a slice."""
        target = self._inputs_window.entry
        if target is None or not isinstance(binding, RegionBinding | IntegerFromBytes):
            return
        if binding.entry is not None:
            source, position = binding.entry, binding.offset
        elif target.kind is EntryKind.FILE:
            source, position = target, binding.offset
        else:
            source = self._workspace.find_file(target.path)
            position = binding.offset
            if source is None:
                return
        self._activate_entry(source)
        if self._workspace.current is source and self._doc is not None:
            # A named entry's offset is into its buffer from 0; a file's is
            # absolute, which is what _land_on_byte takes.
            if binding.entry is not None:
                position += self._anchor_base()
            self._land_on_byte(position)
            self._refresh_view()

    # -- the apply -------------------------------------------------------------------
    def _on_inputs_apply(
        self, bindings: dict[str, Bindings], to_selection: bool
    ) -> None:
        target = self._inputs_window.entry
        if target is None:
            return
        targets = [target]
        if to_selection:
            targets = [
                e
                for e in self._files_panel.selected_entries()
                if self._inputs_available(e)
            ]
            if target not in targets:
                targets.append(target)
        # The sections the form was *built* from, codec override and all: bound
        # under a codec the Slice dialog has picked but not yet OK'd, the entry
        # does not declare it yet, and re-deriving here would drop the apply on
        # the floor. (A slice that is never given that codec sheds the binding on
        # the next save — :func:`~celpix.project.inputs.prune_bindings`.)
        codec, _blocking = self._inputs_context
        plugins = [d.plugin_id for d in self._inputs_sections(target, codec)]
        after = []
        for entry in targets:
            inputs = entry.inputs
            for plugin_id in plugins:
                if entry is target or plugin_id in {
                    d.plugin_id for d in self._inputs_sections(entry)
                }:
                    inputs = self._merged(
                        inputs, entry, plugin_id, bindings.get(plugin_id, {})
                    )
            after.append((entry, inputs))
        self._push_inputs(target, after)

    @staticmethod
    def _merged(
        inputs: dict[str, Bindings], entry: Entry, plugin_id: str, bindings: Bindings
    ) -> dict[str, Bindings]:
        trial = replace(entry, inputs=inputs)
        return with_bindings(trial, plugin_id, bindings)

    def _push_inputs(self, target: Entry, after: list[tuple[Entry, dict]]) -> None:
        """One command over every ``(entry, new inputs)`` pair that changed."""
        changed = [(entry, inputs) for entry, inputs in after if inputs != entry.inputs]
        if not changed:
            return
        dirty = [
            e for e, _ in changed if e.kind is not EntryKind.FILE and (e.pixel_dirty)
        ]
        if dirty:
            answer = QMessageBox.question(
                self,
                "celPix - inputs",
                f"Changing the inputs re-reads {counted(len(dirty), 'entry')} from "
                "disk, discarding unsaved changes. Continue?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._push_command(
            InputsEditCommand(
                self,
                target,
                before=tuple((entry, entry.inputs) for entry, _ in changed),
                after=tuple(changed),
            )
        )
        self.statusBar().showMessage(
            f"Inputs applied to {counted(len(changed), 'entry')}."
        )

    def _apply_inputs(self, state: tuple[tuple[Entry, dict], ...]) -> None:
        """Land one direction of :class:`InputsEditCommand`.

        A slice or map is re-read, because its inputs decide what its bytes
        decode to; a file's bindings feed the preview alone, so the overlay is
        re-run instead. Entries reading *through* a changed one — a region
        bound to it — are re-read on the same rule as a composite over it.
        """
        reread: list[Entry] = []
        for entry, inputs in state:
            entry.inputs = inputs
            if entry.kind is EntryKind.FILE:
                continue
            reread.append(entry)
        if reread:
            self._reread_entries(reread)
        self._reread_input_dependents([entry for entry, _ in state])
        if (
            self._workspace.current is not None
            and self._workspace.current.doc is not None
        ):
            self._refresh_overlay()
            self._sync_inputs_badges()
        for entry, _inputs in state:
            self._files_panel.refresh_entry(entry)
        window = self._inputs_window
        if window.isVisible() and window.entry is not None:
            codec, blocking = self._inputs_context
            self._show_inputs(window.entry, codec=codec, blocking=blocking)

    def _sync_inputs_window(self) -> None:
        """Close the window when the entry it was pinned to is gone."""
        entry = self._inputs_window.entry
        if entry is not None and entry not in self._workspace.entries:
            self._inputs_window.hide_overlay()

    # -- dependents ------------------------------------------------------------------
    def _input_dependents(self, owners: list[Entry]) -> list[Entry]:
        """Every open entry with a binding naming one of ``owners`` — the
        audience for a change to their bytes, the composite's rule one hop
        over (:meth:`~...session.SessionMixin._reassemble_composites`)."""
        stale = {id(e) for owner in owners for e in self._region_of(owner)}
        return [
            entry
            for entry in self._workspace.entries
            if any(
                names_entry(binding) and id(binding.entry) in stale
                for _plugin, _key, binding in iter_bindings(entry)
            )
        ]

    def _reread_input_dependents(self, owners: list[Entry]) -> None:
        """Re-read the clean entries whose inputs come out of ``owners``.

        Clean only: a dependent with unsaved edits keeps its document — its
        edits stand, and the next explicit re-read picks the new table up —
        where dropping it would cost the edits over a change made elsewhere.
        """
        dependents = [
            e
            for e in self._input_dependents(owners)
            if e.doc is not None and not e.pixel_dirty
        ]
        if dependents:
            self._reread_entries(dependents)

    # -- copy / paste ----------------------------------------------------------------
    def _copy_inputs(self, entry: Entry) -> None:
        """Files pane ▸ Copy Inputs: every binding ``entry`` holds."""
        if not entry.inputs:
            self.statusBar().showMessage(f"{entry.name} binds no inputs.")
            return
        named = [b for _p, _k, b in iter_bindings(entry) if names_entry(b)]
        clipboard.put_inputs(
            projectfile.inputs_payload(
                entry, self._workspace.entries, clipboard.SESSION_TOKEN
            ),
            {n: binding.entry for n, binding in enumerate(named)},
        )
        self.statusBar().showMessage(
            f"Copied {counted(sum(len(b) for b in entry.inputs.values()), 'input')}."
        )

    def _paste_inputs(self, entries: list[Entry]) -> None:
        """Files pane ▸ Paste Inputs onto ``entries``: only the keys each
        target's plugins declare land, as one command."""
        payload = clipboard.take_inputs()
        if payload is None:
            return
        bindings, named = projectfile.inputs_from_payload(payload)
        live = (
            clipboard.take_input_sources()
            if payload.get("session") == clipboard.SESSION_TOKEN
            else {}
        )
        for n, (plugin_id, key, _at) in enumerate(named):
            source = live.get(n)
            if source is None:
                bindings[plugin_id].pop(key, None)
            else:
                bindings[plugin_id][key] = replace(
                    bindings[plugin_id][key], entry=source
                )
        after = []
        for entry in entries:
            inputs = entry.inputs
            for declared in self._inputs_sections(entry):
                pasted = bindings.get(declared.plugin_id)
                if not pasted:
                    continue
                keys = {spec.key for spec in declared.specs}
                merged = dict(inputs.get(declared.plugin_id, {}))
                merged.update({k: v for k, v in pasted.items() if k in keys})
                inputs = self._merged(inputs, entry, declared.plugin_id, merged)
            after.append((entry, inputs))
        target = entries[0] if entries else None
        if target is not None:
            self._push_inputs(target, after)

    # -- the preview -------------------------------------------------------------------
    def _preview_config_inputs(self, codec: str) -> dict[Stage, dict] | None:
        """The preview's resolved inputs as a config would carry them, or
        ``None`` when they do not resolve (the badge says why) — what the
        overlay hands the decoder."""
        entry = self._workspace.current
        if entry is None:
            return {}
        resolved = self._preview_inputs(entry, codec)
        if resolved is None:
            return {}
        if not resolved.ok:
            return None
        return {Stage.COMPRESSION: resolved.values}
