"""Simple capture mode: watch → stabilize → assign a name → move the RAW.

A thin `CaptureScreen` subclass, not a parallel reimplementation — the
active campaign's `mode == "simple"` already keeps `framing.enabled` and
every `exports.*.enabled` off (`Campaign.validate()`), so there is never a
frame to edit or a master preview to render: the inherited ingestion loop,
live view, accept/reject/rename, name-conflict panel, and warning/critical
banners already behave correctly with no code of their own. This class
only removes the controls that would otherwise sit there doing nothing
(crop editing, master preview, manual rotation, white balance picking) and
adds the one thing full mode doesn't need on a dedicated key: renaming the
current image on the fly.
"""

from __future__ import annotations

from PySide6.QtGui import QKeyEvent

from scanassistant.gui.screens.capture import CaptureScreen
from scanassistant.gui.shortcuts import CAPTURE, matches


class SimpleCaptureScreen(CaptureScreen):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        # Always empty (no frame is ever detected in this mode) — hidden
        # rather than left showing nothing, so the header reads as
        # deliberately simpler, not as a confidence score that never appears.
        self.confidence_label.setVisible(False)

    # --- crop/master/rotation/white-balance: nothing to do in this mode ---

    def toggle_master_preview(self) -> None:
        pass

    def cycle_preview_action(self, *, direction: int = 1) -> None:
        pass

    def recompute_frame(self) -> None:
        pass

    def rotate_image_action(self, *, direction: int = 1) -> None:
        pass

    def toggle_white_balance_picker(self) -> None:
        pass

    # --- rename on a dedicated key (full mode keeps it menu-only) ----------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (
            not (self.go_to_name_edit.isVisible() and self.go_to_name_edit.hasFocus())
            and not (self.rename_edit.isVisible() and self.rename_edit.hasFocus())
            and self._pending_conflict is None
            and not self._suppress_next_capture_key
            and matches(event, self._shortcuts[CAPTURE]["rename_current"])
        ):
            self.rename_current_image()
            event.accept()
            return
        super().keyPressEvent(event)
