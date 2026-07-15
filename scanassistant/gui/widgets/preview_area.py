"""Preview area for the current image.

Background `#171310` (never pure black); states: waiting, copy in
progress, image displayed (with a frame overlay colored by confidence
level), message (preview unavailable). The image is scaled to fit the
area, aspect ratio preserved; the frame is composed into the
full-resolution pixmap before scaling, so it stays proportionally
correct regardless of window size.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QResizeEvent
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from scanassistant.gui.theme import ACCENT_CRITICAL, ACCENT_OK, ACCENT_WARNING
from scanassistant.i18n import t
from scanassistant.imaging.framing import IMPOSSIBLE, RELIABLE, FrameResult

_OVERLAY_COLORS = {
    RELIABLE: ACCENT_OK,
    "review": ACCENT_WARNING,
    IMPOSSIBLE: ACCENT_CRITICAL,
}
_HALO_COLOR = QColor(0, 0, 0, 217)
_GUIDE_COLOR = QColor(255, 255, 255, 110)


class PreviewArea(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("previewArea")
        self.setMinimumHeight(320)

        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setProperty("role", "secondary")
        self._label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)

        self._base_pixmap: QPixmap | None = None
        self._frame: FrameResult | None = None
        self._pixmap: QPixmap | None = None
        self._guides_visible = False
        self.show_waiting("")

    # --- states --------------------------------------------------------------

    def show_waiting(self, next_name: str) -> None:
        self._base_pixmap = None
        self._frame = None
        text = t("capture.preview_ready_next", name=next_name) if next_name else ""
        self._set_text(text)

    def show_stabilizing(self, source_name: str) -> None:
        self._base_pixmap = None
        self._frame = None
        self._set_text(t("capture.preview_copying", name=source_name))

    def show_message(self, text: str) -> None:
        self._base_pixmap = None
        self._frame = None
        self._set_text(text)

    def show_image(self, pixels: np.ndarray) -> None:
        height, width = pixels.shape[:2]
        image = QImage(
            np.ascontiguousarray(pixels).tobytes(),
            width,
            height,
            width * 3,
            QImage.Format.Format_RGB888,
        )
        self._base_pixmap = QPixmap.fromImage(image)
        self._label.setText("")
        self._compose()

    def set_frame_overlay(self, frame: FrameResult | None) -> None:
        """Frame overlay, colored by confidence level."""
        self._frame = frame
        self._compose()

    def set_guides_visible(self, visible: bool) -> None:
        """Rule-of-thirds guide lines within the frame (edit mode, `G` key)."""
        self._guides_visible = visible
        self._compose()

    def toggle_guides(self) -> bool:
        """Flips the guides on/off, returns the new state."""
        self.set_guides_visible(not self._guides_visible)
        return self._guides_visible

    # --- internals -----------------------------------------------------------

    def _set_text(self, text: str) -> None:
        self._pixmap = None
        self._label.setPixmap(QPixmap())
        self._label.setText(text)

    def _compose(self) -> None:
        if self._base_pixmap is None:
            return
        pixmap = self._base_pixmap
        # IMPOSSIBLE: no frame applied — nothing drawn.
        if self._frame is not None and self._frame.level != IMPOSSIBLE:
            pixmap = _draw_overlay(pixmap, self._frame, guides_visible=self._guides_visible)
        self._pixmap = pixmap
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is None:
            return
        scaled = self._pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._label.setPixmap(scaled)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._rescale()


def _draw_overlay(pixmap: QPixmap, frame: FrameResult, *, guides_visible: bool = False) -> QPixmap:
    result = QPixmap(pixmap)
    painter = QPainter(result)
    width = max(2, round(pixmap.width() * 0.003))
    rect = QRectF(-frame.width / 2, -frame.height / 2, frame.width, frame.height)

    painter.translate(frame.x + frame.width / 2, frame.y + frame.height / 2)
    painter.rotate(frame.angle_deg)

    # Dark keyline on both sides of the colored line: a plain amber line
    # nearly disappears against an orange-based colour negative's own
    # cast, so legibility can't rely on hue alone.
    halo_pen = QPen(_HALO_COLOR)
    halo_pen.setWidth(width + 2)
    painter.setPen(halo_pen)
    painter.drawRect(rect)

    pen = QPen(QColor(_OVERLAY_COLORS.get(frame.level, ACCENT_CRITICAL)))
    pen.setWidth(width)
    painter.setPen(pen)
    painter.drawRect(rect)

    if guides_visible:
        _draw_thirds_guides(painter, rect)

    painter.end()
    return result


def _draw_thirds_guides(painter: QPainter, rect: QRectF) -> None:
    """Two evenly-spaced vertical and horizontal lines within the frame —
    a compositional aid for manual cropping, not a confidence signal: kept
    unsaturated and dotted so it never competes with the frame overlay."""
    pen = QPen(_GUIDE_COLOR)
    pen.setWidth(1)
    pen.setStyle(Qt.PenStyle.DotLine)
    painter.setPen(pen)
    x1 = rect.left() + rect.width() / 3
    x2 = rect.left() + 2 * rect.width() / 3
    y1 = rect.top() + rect.height() / 3
    y2 = rect.top() + 2 * rect.height() / 3
    painter.drawLine(QPointF(x1, rect.top()), QPointF(x1, rect.bottom()))
    painter.drawLine(QPointF(x2, rect.top()), QPointF(x2, rect.bottom()))
    painter.drawLine(QPointF(rect.left(), y1), QPointF(rect.right(), y1))
    painter.drawLine(QPointF(rect.left(), y2), QPointF(rect.right(), y2))
