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

from scanassistant.imaging.framing import (
    IMPOSSIBLE,
    FrameResult,
    detect_frame,
    rescue_impossible_frame,
)
from scanassistant.imaging.preview import Preview, extract_preview
from scanassistant.imaging.raw import RawDecoder
from scanassistant.project.campaign import FramingConfig


@dataclass(frozen=True)
class PreviewResult:
    preview: Preview
    frame: FrameResult | None  # None if automatic framing is disabled (campaign.framing.enabled)
    rescued: bool = False  # `frame` came from rescue_impossible_frame, not detect_frame —
    # traceability only (journal `FRAMING` detail), never changes how the frame is applied.


# Detection (rescue in particular) can outlive the widget that started it —
# the operator moves to the next image, or closes the app, while a GrabCut
# rescue is still running. Nothing else may then hold a reference to the
# worker; if Python garbage-collects it while its thread is still running,
# Qt aborts the whole process rather than raising a catchable error. Kept
# alive here, independent of any widget's lifetime, until `finished` fires.
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
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._decoder = decoder
        self._framing_config = framing_config
        # Reopening an already-finalized image for correction (history side
        # panel): its frame is already known (session history) and must not
        # be silently overwritten by a fresh, possibly different detection.
        self._skip_detection = skip_detection

    def start(self, *args, **kwargs) -> None:
        _running_workers.add(self)
        self.finished.connect(lambda: _running_workers.discard(self))
        super().start(*args, **kwargs)

    def run(self) -> None:
        try:
            preview: Preview = extract_preview(self._path, self._decoder)
            if self._skip_detection or not self._framing_config.enabled:
                frame, rescued = None, False
            else:
                frame, rescued = self._detect_frame(preview)
        except Exception as exc:  # unreadable/corrupt RAW (E-05)
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(PreviewResult(preview=preview, frame=frame, rescued=rescued))

    def _detect_frame(self, preview: Preview) -> tuple[FrameResult, bool]:
        config = self._framing_config
        primary = detect_frame(
            preview.pixels,
            margin_pct=config.margin_pct,
            max_deskew_deg=config.max_deskew_deg,
            reliable_threshold=config.reliable_threshold,
            review_threshold=config.review_threshold,
            threshold_bias=config.threshold_bias,
        )
        if primary.level != IMPOSSIBLE:
            return primary, False
        # Last resort, never in place of the primary detector: a severely
        # underexposed negative can have near-zero brightness contrast with
        # the light table, leaving detect_frame's Otsu threshold nothing to
        # split on — GrabCut's colour-distribution model can sometimes still
        # separate the two.
        rescued = rescue_impossible_frame(
            preview.pixels,
            margin_pct=config.margin_pct,
            max_deskew_deg=config.max_deskew_deg,
            reliable_threshold=config.reliable_threshold,
            review_threshold=config.review_threshold,
        )
        return (rescued, True) if rescued is not None else (primary, False)
