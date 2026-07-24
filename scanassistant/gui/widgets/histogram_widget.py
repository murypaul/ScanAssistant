"""Small luminance histogram overlay for the capture screen's main preview.

Purely a diagnostic aid — exposure/contrast at a glance while judging the
light table (no interaction, no data of its own beyond the current image).
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from scanassistant.gui.theme import PREVIEW_BACKGROUND, TEXT_PRIMARY

_BIN_COUNT = 64
# Every Nth pixel in each dimension (a 16x pixel-count reduction at 4) —
# purely a peak-relative bar-height diagnostic, not a value any export
# depends on, so a few thousand sampled pixels already look identical to
# the full array on screen. Recomputed on every render (each navigation,
# each committed tonal setting), on the full multi-megapixel preview
# array in float64, this was real, avoidable per-render cost.
_SAMPLE_STRIDE = 4
_BACKGROUND_COLOR = QColor(PREVIEW_BACKGROUND)
_BACKGROUND_COLOR.setAlpha(120)
_BAR_COLOR = QColor(TEXT_PRIMARY)
_BAR_COLOR.setAlpha(200)


class HistogramWidget(QWidget):
    """Read-only luminance histogram of whatever `set_pixels` was last given."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._bins: np.ndarray | None = None

    def set_pixels(self, pixels: np.ndarray | None) -> None:
        if pixels is None:
            self._bins = None
        else:
            # Subsampled, and Rec.709 luminance in integer arithmetic
            # (54/183/19 out of 256, the standard fixed-point approximation
            # of 0.2126/0.7152/0.0722) rather than a float64 pass over
            # every pixel — see `_SAMPLE_STRIDE`'s own comment.
            sample = pixels[::_SAMPLE_STRIDE, ::_SAMPLE_STRIDE].astype(np.uint32)
            luminance = (sample[:, :, 0] * 54 + sample[:, :, 1] * 183 + sample[:, :, 2] * 19) >> 8
            counts, _ = np.histogram(luminance, bins=_BIN_COUNT, range=(0, 255))
            self._bins = counts.astype(np.float64)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), _BACKGROUND_COLOR)
        if self._bins is not None:
            peak = self._bins.max()
            if peak > 0:
                bar_width = self.width() / _BIN_COUNT
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_BAR_COLOR)
                for i, count in enumerate(self._bins):
                    bar_height = (count / peak) * self.height()
                    painter.drawRect(
                        QRectF(i * bar_width, self.height() - bar_height, bar_width, bar_height)
                    )
        painter.end()
