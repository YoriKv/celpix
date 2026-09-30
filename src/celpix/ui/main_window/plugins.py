"""Reloading plugins: F5's hot reload, and a project's own plugins folder.

Both rebuild the registry through the reloader the app injects (the window
knows which project is open, the app knows where plugins live), then bring the
preset pickers and the open entry back in step with what the registry now has.
Neither is an edit: the re-decode goes through the application paths, never a
command, so a refresh leaves the undo history alone.

A slice of :class:`~celpix.ui.main_window.window.MainWindow`. It reads
``_registry``, ``_plugin_issues``, ``_reload_plugins`` and
``_codec_faults_seen``, all created in ``MainWindow.__init__``, and refills the
preset combos :meth:`~...interpretation.InterpretationMixin._build_toolbar` and
the palette dock build.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.project.workspace import repair_presets
from celpix.ui.searchable_combo import fill_grouped, preset_rows
from celpix.ui.undo_commands import PaletteState
from celpix.ui.widgets import signals_blocked


class PluginsMixin:
    """The plugin registry's reload, and the pickers that list it.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object; see the module docstring for the state it reads.
    """

    def _refresh_plugins(self) -> None:
        """Developer aid: reload plugins from disk and re-run on the open file.

        Rebuilds the registry from both plugin roots - the user's folder and the
        open project's own (:meth:`_load_project_plugins`) - picking up
        added/changed/removed presets and code plugins (a changed code plugin
        passes the trust gate; one you approved this run reloads without a
        prompt), refreshes the preset menus, and re-decodes the currently open
        pixel/palette through the reloaded plugins.

        The pixel re-run goes back to disk so a reloaded Read/Decompress plugin
        is exercised too - except on an entry with unsaved edits, which live only
        in the loaded bytes and a re-read would throw away. There the refresh
        reinterprets what is in memory, so a changed *codec* still takes effect
        and the edits survive; a re-read happens on the entry's next load.

        **A tilemap is re-read whole instead** (:meth:`~...tilemap_bar.
        TilemapBarMixin._reload_tilemap`), because its pixel half is not its own:
        the tiles come from the entry it is bound to, and running the pixel
        pathway over *this* entry's bytes would decode the map's own cells as
        tiles and draw noise over a picture that was right. That path re-reads
        both halves under the binding, so the reloaded plugins are still
        exercised, and it carries the same unsaved-edit rule.
        """
        if self._reload_plugins is None:
            return
        entry = self._workspace.current
        # With the open project's path, so the scan covers its own plugins/
        # folder too - F5 is the way to pick up an edit there, exactly as it is
        # for the user's folder.
        self._registry, self._plugin_issues = self._reload_plugins(self._project_path)
        # Reloaded code is new code: a crash it still has is news again.
        self._codec_faults_seen.clear()
        # A refresh can *remove* a format as easily as add one — a deleted preset
        # file, a plugin that no longer passes the trust gate — so the open
        # entries are put back in step with the registry before anything decodes
        # through it. Reported at the end, with the load issues.
        missing_presets = repair_presets(self._workspace.entries, self._registry)
        self._repopulate_presets()
        # New code is a new chance: an entry that failed to open is unmarked, so
        # its next activation tries the reloaded plugins - and the one on screen
        # tries them now, since a refresh is usually aimed at exactly it.
        for failed in [e for e in self._workspace.entries if e.load_failure]:
            failed.load_failure = None
            self._files_panel.refresh_entry(failed)
        if self._doc is None and entry is not None and entry.doc is None:
            self._on_current_entry_changed(entry)
        elif self._doc is not None and self._doc.is_tilemap and entry is not None:
            # Cells, bound tiles and palette in one read, under the binding.
            self._reload_tilemap(entry)
        elif self._doc is not None:
            # Re-decode the open file's sources through the new registry - via
            # the application paths, never commands: a plugin refresh isn't an
            # edit and must not pollute the undo history.
            self._apply_pixel_config(
                self._pixel_preset_id(),
                self._recorded_byte_position(),
                reload=entry is None or not entry.pixel_dirty,
            )
            # Only a palette with an external source can be re-decoded; a
            # generated default or a project-stored custom palette has no bytes
            # to re-read (its config points at an empty path).
            if self._palette_mode.has_source:
                result = self._reinterpret_palette()
                if result is not None:
                    loaded, cfg = result
                    self._apply_palette_state(
                        PaletteState(
                            cfg.interpret_preset_id,
                            self._palette_mode,
                            loaded.palette,
                            cfg,
                            loaded.ctx,
                            base_bytes=loaded.data,
                        )
                    )

        parts = ["Plugins refreshed"]
        if self._doc is not None:
            parts.append("re-ran on current file")
        self.statusBar().showMessage("; ".join(parts) + ".")
        # Any plugin that failed the reload is a warning, surfaced modally.
        self._alert_plugin_issues()
        self._alert_missing_presets(missing_presets)

    def _load_project_plugins(self, project_path: str | None) -> None:
        """Rebuild the registry for the project at ``project_path``.

        The ``plugins/`` folder beside a project file belongs to the project, so
        it is scanned as the project opens and dropped again when it closes -
        both ends go through here, and the registry an entry decodes through is
        never one from the project before. Called *before* the workspace is
        replaced: a restored entry may name a preset only the project provides,
        and showing it decodes immediately.

        Its code plugins pass the same trust gate as the user's own, so opening
        a project that carries one asks first (:mod:`celpix.plugins.trust`).
        """
        if self._reload_plugins is None:
            return
        self._registry, self._plugin_issues = self._reload_plugins(project_path)
        # The one widget that keeps a reference of its own rather than reading
        # the window's live: its rows name each entry's container and which of
        # the three tilemap layouts it holds, both off the registry, and this is
        # a *different object* from the one it was built with. Left behind, every
        # row whose format the project itself provides reads as having none.
        self._files_panel.set_registry(self._registry)
        self._repopulate_presets()
        self._alert_plugin_issues()

    def _repopulate_presets(self) -> None:
        """Rebuild the preset combos from the (reloaded) registry, keeping the
        current selection when it still exists."""
        # The pixel combo goes through the filter (a refresh keeps hidden formats
        # hidden and lets newly added ones through); the selection is preserved.
        self._fill_pixel_combo(self._pixel_preset.currentData())
        current = self._palette_preset.currentData()
        # Block signals so repopulating doesn't fire a reload per item; the
        # refresh does one explicit reload afterwards.
        with signals_blocked(self._palette_preset):
            fill_grouped(
                self._palette_preset,
                preset_rows(self._registry.presets(Stage.INTERPRET_PALETTE)),
                current,
            )
        # The compression combo lists Decompress *plugins*, not presets, but
        # refreshes the same way (keep the selection when it survives the reload).
        with signals_blocked(self._compression):
            self._populate_compression(self._compression.currentData())
        # And the swatch view's format picker, which lists the palette presets
        # a second time: same list, same rule.
        with signals_blocked(self._palette_view_preset):
            fill_grouped(
                self._palette_view_preset,
                preset_rows(self._registry.presets(Stage.INTERPRET_PALETTE)),
                self._palette_view_preset.currentData(),
            )
