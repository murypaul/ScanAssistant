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

from scanassistant.imaging.framing import FrameResult, detect_frame
from scanassistant.imaging.preview import Preview, extract_preview
from scanassistant.imaging.raw import RawDecoder
from scanassistant.project.campaign import FramingConfig


@dataclass(frozen=True)
class PreviewResult:
    preview: Preview
    frame: FrameResult | None  # None if automatic framing is disabled (campaign.framing.enabled)


class PreviewWorker(QThread):
    succeeded = Signal(object)  # PreviewResult
    failed = Signal(str)

    def __init__(
        self,
        path: Path,
        decoder: RawDecoder,
        framing_config: FramingConfig,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._decoder = decoder
        self._framing_config = framing_config

    def run(self) -> None:
        try:
            preview: Preview = extract_preview(self._path, self._decoder)
            frame = self._detect_frame(preview) if self._framing_config.enabled else None
        except Exception as exc:  # unreadable/corrupt RAW (E-05)
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(PreviewResult(preview=preview, frame=frame))

    def _detect_frame(self, preview: Preview) -> FrameResult:
        config = self._framing_config
        return detect_frame(
            preview.pixels,
            margin_pct=config.margin_pct,
            max_deskew_deg=config.max_deskew_deg,
            reliable_threshold=config.reliable_threshold,
            review_threshold=config.review_threshold,
            threshold_bias=config.threshold_bias,
        )
