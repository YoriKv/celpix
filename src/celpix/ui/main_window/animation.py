"""The animation player's window side: what it is handed, and when.

The window itself is presentation only (:mod:`celpix.ui.animation_overlay`) — it
holds a composed strip, the rectangles its frames occupy, and the sequences to
step through, and it never reads the model. This is the half that builds those
three from the live document.

Two things it has to get right, and neither is the window's to know:

- **The strip is untrimmed.** The canvas draws frames up to the last one holding
  a drawn subsprite (``Document.shown_frames``), which is the right reading of a
  file whose trailing slots hold a template rather than art. A *sequence* can
  name a frame the trim drops — 349 of the corpus's objects do — so the player is
  given every slot the file has and lets the sequence pick
  (``docs/graphics-formats-reference/scgcad-formats.md`` §8.4).
- **The colour rule is the document's.** The strip goes through the same
  ``_tilemap_grid_image`` the canvas uses, so a frame in the player is the frame
  on the canvas rather than a second rendering that could drift from it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QRect
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFileDialog

from celpix.pipeline import pipeline
from celpix.project.workspace import export_basename
from celpix.ui import export
from celpix.ui.widgets import ask_save_path


class AnimationMixin:
    """Building and gating the animation player.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads the window's own widgets and its single live
    ``_doc``. See the package docstring for why these are mixins.
    """

    def _animation_available(self) -> bool:
        """Whether this document has sequences worth opening a player on.

        Sharper than "is a sprite object", and sharper than the capability table
        can be: a file has room for 16 or 32 sequences and the corpus fills a
        handful, so what decides is whether **any** of them holds a step. An
        object whose table is all terminator would otherwise open a window with
        an empty picker in it.
        """
        doc = self._doc
        return bool(doc is not None and doc.is_sprite and any(doc.animations))

    def _show_animation(self) -> None:
        """View ▸ Animation — compose the strip and hand it to the player."""
        if not self._animation_available():
            self._animation.hide_overlay()
            return
        doc = self._doc
        entry = self._workspace.current
        strip, rects = self._animation_strip()
        name = entry.name if entry is not None else "object"
        self._animation.show_object(
            strip,
            rects,
            doc.animations,
            f"Animation - {name}",
            inferred=doc.animations_inferred,
        )

    def _animation_strip(self) -> tuple[QImage, list[QRect]]:
        """The whole object drawn, and where each frame sits in it.

        Every frame is drawn in one shared bounding box
        (:func:`~celpix.core.sprite.frame_bounds`) so a strip shows the object's
        motion instead of re-centring it frame by frame — which is exactly what
        makes a frame a fixed rectangle here, and playback a blit rather than a
        render.

        The document is copied rather than changed: ``show_all_frames`` is the
        user's view setting and the player needs the other answer, so it asks the
        question of a copy and leaves the entry's own view alone.
        """
        doc = self._doc
        untrimmed = replace(doc, view=replace(doc.view, show_all_frames=True))
        columns = self._tilemap_columns()
        grid, sheet = pipeline.sprite_image(untrimmed, self._registry, columns)
        width, height = sheet.box[2], sheet.box[3]
        rects = [
            QRect(
                (at % sheet.across) * width,
                (at // sheet.across) * height,
                width,
                height,
            )
            for at in range(sheet.frames)
        ]
        return self._tilemap_grid_image(grid), rects

    def _sync_animation(self) -> None:
        """Close the player when the entry it was opened on is no longer showing.

        Called where the document changes rather than on a timer: the window
        holds its own copy of the strip, so one left open over a different entry
        would go on playing a picture that is no longer anywhere on screen.
        """
        if not self._animation.isVisible():
            return
        if not self._animation_available():
            self._animation.hide_overlay()
            return
        # Asked for rather than done: the strip is every frame the file has, and
        # this runs on each refresh of the entry underneath — once per pixel of a
        # stroke. The window coalesces the burst (`request_refresh`), so an open
        # player costs one recompose per burst instead of one per repaint.
        self._animation.request_refresh()

    def _export_animation(self, as_gif: bool, every: bool) -> None:
        """The player's Export menu: sequences to a GIF each, or to numbered PNGs.

        Written from the strip the player holds rather than a fresh compose, so
        the file is the motion on screen. One sequence as a GIF asks for a file;
        anything writing several files asks for a folder and names them
        ``<entry>-seq<N>`` by the sequence's number in the picker, so an "all"
        export and a single one land under the same names.
        """
        strip, rects, chosen, rate = self._animation.export_source(every)
        entry = self._workspace.current
        if not chosen or not rects or entry is None:
            return
        base = export_basename(entry)
        folder_default = self._export_dir(entry)
        if as_gif and not every:
            at, _ = chosen[0]
            path = ask_save_path(
                self._animation,
                "Export Animation as GIF",
                str(Path(folder_default) / f"{base}-seq{at}.gif"),
                "GIF image (*.gif)",
                ".gif",
            )
            if path is None:
                return
            targets = [(path, chosen[0][1])]
        else:
            folder = QFileDialog.getExistingDirectory(
                self._animation, "Export animation to folder", folder_default
            )
            if not folder:
                return
            targets = [
                (str(Path(folder) / f"{base}-seq{at}"), sequence)
                for at, sequence in chosen
            ]
        files = 0
        try:
            for target, sequence in targets:
                if as_gif:
                    if not target.lower().endswith(".gif"):
                        target += ".gif"
                    export.save_sequence_gif(strip, rects, sequence, rate, target)
                    files += 1
                else:
                    files += len(
                        export.save_sequence_pngs(strip, rects, sequence, target)
                    )
        except (OSError, ValueError) as exc:
            self._alert(
                f"Could not export the animation: {exc}", title="celPix - export"
            )
            return
        where = (
            targets[0][0]
            if len(targets) == 1 and as_gif
            else str(Path(targets[0][0]).parent)
        )
        self.statusBar().showMessage(
            f"Exported {len(targets)} sequence{'s' if len(targets) != 1 else ''}"
            f" of {entry.name} ({files} file{'s' if files != 1 else ''}) to {where}."
        )
