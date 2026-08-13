"""Preview area for the current image.

Background `#171310` (never pure black); states: waiting, copy in
progress, image displayed (with a frame overlay colored by confidence
level), message (preview unavailable). The image is scaled to fit the
area, aspect ratio preserved; the frame is composed into the
full-resolution pixmap before scaling, so it stays proportionally
correct regardless of window size.

The frame overlay is also a live drag handle: an edge/corner resizes
that side (rotation-aware — dragging accounts for the frame's own
deskew angle), the interior (border included) moves the whole frame.
No separate "edit mode" — `frame_dragged` fires continuously while
dragging (display-only, cheap), `frame_drag_finished` once, on release
(the caller debounces the actual `apply_frame`/re-export from there,
the same pattern as the `V`-key rotation debounce).
"""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QResizeEvent,
)
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
_GUIDE_COLOR = QColor(255, 255, 255, 215)
_GUIDE_HALO_COLOR = QColor(0, 0, 0, 160)

_EDGE_TOLERANCE_SCREEN_PX = 10
_MIN_FRAME_SIZE_PX = 20
_CURSOR_BY_ZONE = {
    "n": Qt.CursorShape.SizeVerCursor,
    "s": Qt.CursorShape.SizeVerCursor,
    "e": Qt.CursorShape.SizeHorCursor,
    "w": Qt.CursorShape.SizeHorCursor,
    "ne": Qt.CursorShape.SizeBDiagCursor,
    "sw": Qt.CursorShape.SizeBDiagCursor,
    "nw": Qt.CursorShape.SizeFDiagCursor,
    "se": Qt.CursorShape.SizeFDiagCursor,
    "move": Qt.CursorShape.SizeAllCursor,
}


class PreviewArea(QWidget):
    frame_dragged = Signal(object)  # FrameResult, continuously while dragging
    frame_drag_finished = Signal()  # once, on release — caller debounces the commit
    point_picked = Signal(QPointF)  # pixmap-space point, while picking mode is on

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("previewArea")
        self.setMinimumHeight(320)

        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setProperty("role", "secondary")
        self._label.setWordWrap(True)
        # Mouse events go to this widget, not the label sitting on top of it.
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)

        self._base_pixmap: QPixmap | None = None
        self._frame: FrameResult | None = None
        self._pixmap: QPixmap | None = None
        self._guides_visible = False
        self._drag_zone: str | None = None
        self._drag_start_point: QPointF | None = None
        self._drag_start_frame: FrameResult | None = None
        self._picking_enabled = False
        self.setMouseTracking(True)
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

    def show_processing(self, name: str) -> None:
        """Between ingestion and the RAW decode + auto-detection finishing
        (`_load_preview`'s `PreviewWorker`, a few seconds) — distinct from
        `show_stabilizing`'s file-transfer wait, so an operator working a
        long session can tell the two apart and knows not to trigger the
        next shot yet."""
        self._base_pixmap = None
        self._frame = None
        self._set_text(t("capture.preview_processing", name=name))

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

    def set_picking_enabled(self, enabled: bool) -> None:
        """White balance picker (`W` key): while on, the next left click
        anywhere on the image emits `point_picked` instead of drag-editing
        the crop, and the cursor becomes a crosshair."""
        self._picking_enabled = enabled
        self.setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor)

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

    # --- drag-to-crop: edge/corner resizes, interior (border included) moves ---

    def _draggable_frame(self) -> FrameResult | None:
        if self._frame is None or self._frame.level == IMPOSSIBLE:
            return None
        return self._frame

    def _scale_and_offset(self) -> tuple[float, float, float] | None:
        if self._pixmap is None:
            return None
        pixmap_w, pixmap_h = self._pixmap.width(), self._pixmap.height()
        if pixmap_w <= 0 or pixmap_h <= 0:
            return None
        widget_size = self.size()
        scale = min(widget_size.width() / pixmap_w, widget_size.height() / pixmap_h)
        if scale <= 0:
            return None
        offset_x = (widget_size.width() - pixmap_w * scale) / 2
        offset_y = (widget_size.height() - pixmap_h * scale) / 2
        return scale, offset_x, offset_y

    def _to_pixmap_point(self, pos: QPointF) -> QPointF | None:
        """Widget coordinates → the full-resolution composited pixmap's own
        coordinate space (same space as `FrameResult.x/y/width/height`)."""
        geo = self._scale_and_offset()
        if geo is None:
            return None
        scale, offset_x, offset_y = geo
        return QPointF((pos.x() - offset_x) / scale, (pos.y() - offset_y) / scale)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._picking_enabled:
            point = self._to_pixmap_point(event.position())
            if event.button() == Qt.MouseButton.LeftButton and point is not None:
                self.point_picked.emit(point)
                event.accept()
                return
            super().mousePressEvent(event)
            return
        frame = self._draggable_frame()
        point = self._to_pixmap_point(event.position()) if frame is not None else None
        if event.button() != Qt.MouseButton.LeftButton or frame is None or point is None:
            super().mousePressEvent(event)
            return
        geo = self._scale_and_offset()
        assert geo is not None
        zone = _hit_zone(point, frame, tolerance=_EDGE_TOLERANCE_SCREEN_PX / geo[0])
        if zone is None:
            super().mousePressEvent(event)
            return
        self._drag_zone = zone
        self._drag_start_point = point
        self._drag_start_frame = frame
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_zone is None:
            if not self._picking_enabled:
                self._update_hover_cursor(event.position())
            super().mouseMoveEvent(event)
            return
        point = self._to_pixmap_point(event.position())
        if point is None or self._drag_start_point is None or self._drag_start_frame is None:
            return
        new_frame = _drag_frame(
            self._drag_start_frame,
            self._drag_zone,
            self._drag_start_point,
            point,
            constrain_axis=bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier),
        )
        self._frame = new_frame
        self._compose()
        self.frame_dragged.emit(new_frame)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_zone is not None and event.button() == Qt.MouseButton.LeftButton:
            self._drag_zone = None
            self._drag_start_point = None
            self._drag_start_frame = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.frame_drag_finished.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _update_hover_cursor(self, pos: QPointF) -> None:
        frame = self._draggable_frame()
        point = self._to_pixmap_point(pos) if frame is not None else None
        geo = self._scale_and_offset()
        if frame is None or point is None or geo is None:
            self.unsetCursor()
            return
        zone = _hit_zone(point, frame, tolerance=_EDGE_TOLERANCE_SCREEN_PX / geo[0])
        self.setCursor(_CURSOR_BY_ZONE[zone] if zone is not None else Qt.CursorShape.ArrowCursor)


def _to_local_point(point: QPointF, frame: FrameResult) -> QPointF:
    """`point` (pixmap space) relative to the frame's own center, with the
    frame's deskew rotation undone — i.e. in the same axis-aligned space
    `frame.x/y/width/height` already describe."""
    cx, cy = frame.x + frame.width / 2, frame.y + frame.height / 2
    dx, dy = point.x() - cx, point.y() - cy
    angle = -math.radians(frame.angle_deg)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    return QPointF(dx * cos_a - dy * sin_a, dx * sin_a + dy * cos_a)


def _rotate_vector(dx: float, dy: float, angle_deg: float) -> tuple[float, float]:
    angle = math.radians(angle_deg)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    return dx * cos_a - dy * sin_a, dx * sin_a + dy * cos_a


def _hit_zone(point: QPointF, frame: FrameResult, *, tolerance: float) -> str | None:
    """Which part of `frame` a (pixmap-space) point is over: `None` (outside,
    beyond tolerance), an edge/corner code ("n"/"se"/…) within `tolerance` of
    that edge, or `"move"` for the interior."""
    local = _to_local_point(point, frame)
    half_w, half_h = frame.width / 2, frame.height / 2
    if not (
        -half_w - tolerance <= local.x() <= half_w + tolerance
        and -half_h - tolerance <= local.y() <= half_h + tolerance
    ):
        return None
    near_west = abs(local.x() - (-half_w)) <= tolerance
    near_east = abs(local.x() - half_w) <= tolerance
    near_north = abs(local.y() - (-half_h)) <= tolerance
    near_south = abs(local.y() - half_h) <= tolerance
    vertical = "n" if near_north else "s" if near_south else ""
    horizontal = "w" if near_west else "e" if near_east else ""
    zone = vertical + horizontal
    return zone or "move"


def _drag_frame(
    start_frame: FrameResult,
    zone: str,
    start_point: QPointF,
    current_point: QPointF,
    *,
    constrain_axis: bool = False,
) -> FrameResult:
    """The frame that results from dragging `zone` from `start_point` to
    `current_point` (both pixmap space), starting from `start_frame`.

    `constrain_axis` (Shift held, "move" only — same convention as other
    image editors): locks the drag to whichever of horizontal/vertical had
    the larger movement since the press, zeroing the other — lets an
    operator nudge a crop along one line without a slightly unsteady hand
    drifting it off-axis."""
    if zone == "move":
        dx = current_point.x() - start_point.x()
        dy = current_point.y() - start_point.y()
        if constrain_axis:
            if abs(dx) >= abs(dy):
                dy = 0.0
            else:
                dx = 0.0
        return replace(start_frame, x=start_frame.x + dx, y=start_frame.y + dy)

    local_start = _to_local_point(start_point, start_frame)
    local_current = _to_local_point(current_point, start_frame)
    ldx = local_current.x() - local_start.x()
    ldy = local_current.y() - local_start.y()

    width, height = start_frame.width, start_frame.height
    center_shift_x, center_shift_y = 0.0, 0.0
    if "w" in zone:
        width = start_frame.width - ldx
        center_shift_x = ldx / 2
    elif "e" in zone:
        width = start_frame.width + ldx
        center_shift_x = ldx / 2
    if "n" in zone:
        height = start_frame.height - ldy
        center_shift_y = ldy / 2
    elif "s" in zone:
        height = start_frame.height + ldy
        center_shift_y = ldy / 2

    width = max(_MIN_FRAME_SIZE_PX, width)
    height = max(_MIN_FRAME_SIZE_PX, height)
    shift_x, shift_y = _rotate_vector(center_shift_x, center_shift_y, start_frame.angle_deg)
    old_center_x = start_frame.x + start_frame.width / 2
    old_center_y = start_frame.y + start_frame.height / 2
    new_center_x = old_center_x + shift_x
    new_center_y = old_center_y + shift_y
    return replace(
        start_frame,
        x=new_center_x - width / 2,
        y=new_center_y - height / 2,
        width=width,
        height=height,
    )


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
    a compositional aid for manual cropping, not a confidence signal. A dark
    halo under the dotted white line keeps it readable against any part of
    the negative, the same reasoning as the frame overlay's own keyline —
    a plain low-alpha line was reported nearly invisible in real use."""
    x1 = rect.left() + rect.width() / 3
    x2 = rect.left() + 2 * rect.width() / 3
    y1 = rect.top() + rect.height() / 3
    y2 = rect.top() + 2 * rect.height() / 3
    lines = (
        (QPointF(x1, rect.top()), QPointF(x1, rect.bottom())),
        (QPointF(x2, rect.top()), QPointF(x2, rect.bottom())),
        (QPointF(rect.left(), y1), QPointF(rect.right(), y1)),
        (QPointF(rect.left(), y2), QPointF(rect.right(), y2)),
    )

    halo_pen = QPen(_GUIDE_HALO_COLOR)
    halo_pen.setWidthF(2.4)
    painter.setPen(halo_pen)
    for start, end in lines:
        painter.drawLine(start, end)

    guide_pen = QPen(_GUIDE_COLOR)
    guide_pen.setWidthF(1.2)
    guide_pen.setStyle(Qt.PenStyle.DotLine)
    painter.setPen(guide_pen)
    for start, end in lines:
        painter.drawLine(start, end)
