"""Preview extraction + frame detection off the main thread.

`imaging.preview.extract_preview()` (disk I/O, JPEG decoding) and
`imaging.framing.detect_frame()` (OpenCV) both touch disk or burn CPU:
calling them from the Qt thread would violate the "never blocked > 100 ms"
budget. `PreviewWorker` runs them one after the other in a dedicated
`QThread` and delivers the result via signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from scanassistant.imaging.framing import FrameResult, budget_to_params, detect_frame
from scanassistant.imaging.preview import Preview, extract_preview
from scanassistant.imaging.raw import RawDecoder
from scanassistant.project.campaign import FramingConfig


@dataclass(frozen=True)
class PreviewResult:
    preview: Preview
    frame: FrameResult | None  # None if automatic framing is disabled (campaign.framing.enabled)


# Detection (the GrabCut-based `detect_frame`, several seconds depending on
# `framing.detection_budget_s`) can outlive the widget that started it — the
# operator moves to the next image, or closes the app, while one is still
# running. Nothing else may then hold a reference to the worker; if Python
# garbage-collects it while its thread is still running, Qt aborts the whole
# process rather than raising a catchable error. Kept alive here,
# independent of any widget's lifetime, until `finished` fires.
_running_workers: set[PreviewWorker] = set()


class PreviewWorker(QThread):
    succeeded = Signal(object)  # PreviewResult
    failed = Signal(str)

    def __init__(
        self,
        path: Path,
        decoder: RawDecoder,
        framing_config: FramingConfig,
        parent: QObject | None = None,
        *,
        skip_detection: bool = False,
        user_wb: list[float] | None = None,
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._decoder = decoder
        self._framing_config = framing_config
        # Reopening an already-finalized image for correction (history side
        # panel): its frame is already known (session history) and must not
        # be silently overwritten by a fresh, possibly different detection.
        self._skip_detection = skip_detection
        self._user_wb = user_wb

    def start(self, *args, **kwargs) -> None:
        _running_workers.add(self)
        self.finished.connect(lambda: _running_workers.discard(self))
        super().start(*args, **kwargs)

    def run(self) -> None:
        try:
            preview: Preview = extract_preview(self._path, self._decoder, user_wb=self._user_wb)
            if self._skip_detection or not self._framing_config.enabled:
                frame = None
            else:
                frame = self._detect_frame(preview)
        except Exception as exc:  # unreadable/corrupt RAW (E-05)
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(PreviewResult(preview=preview, frame=frame))

    def _detect_frame(self, preview: Preview) -> FrameResult:
        config = self._framing_config
        working_long_edge_px, grabcut_iters = budget_to_params(config.detection_budget_s)
        return detect_frame(
            preview.pixels,
            margin_pct=config.margin_pct,
            max_deskew_deg=config.max_deskew_deg,
            reliable_threshold=config.reliable_threshold,
            review_threshold=config.review_threshold,
            working_long_edge_px=working_long_edge_px,
            grabcut_iters=grabcut_iters,
        )
