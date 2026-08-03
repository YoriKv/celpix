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

from PySide6.QtCore import QRect
from PySide6.QtGui import QImage

from celpix.pipeline import pipeline


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
        self._show_animation()
