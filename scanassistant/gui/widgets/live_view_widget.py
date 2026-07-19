"""Live view overlay: remote framing aid for a tethered camera (D750, PTP/USB).

The camera's own rear screen goes dark as soon as it is connected in
PTP/USB mode — this widget is the replacement viewfinder. Sits over
`PreviewArea` as a small picture-in-picture vignette by default; clicking
it expands it to check focus in detail (mouse wheel to zoom, drag to
pan). `CaptureScreen` owns the `CameraController` and feeds frames/state
into this widget through plain method calls — this widget only reports
UI-originated intent (opacity/fps changed, toggle requested) back out
through signals, the same shape as `SliderField`/`ToggleSwitch`.
"""

from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QComboBox,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from scanassistant.camera.backend import LiveViewFrame
from scanassistant.config import LIVE_VIEW_FPS_CHOICES, CameraConfig
from scanassistant.gui.theme import BORDER, TEXT_SECONDARY
from scanassistant.gui.widgets.slider_field import SliderField
from scanassistant.i18n import t

_COLLAPSED_WIDTH_FRACTION = 0.32
_EXPANDED_SIZE_FRACTION = 0.9
_ASPECT = 4 / 3
_MEASURED_FPS_WINDOW = 8  # rolling average over this many inter-frame gaps
_ZOOM_MIN, _ZOOM_MAX = 1.0, 6.0
_ZOOM_STEP = 1.15
_LIVE_DOT_COLOR = QColor("#e2685c")
_MIN_OPACITY_PERCENT = 15


def image_from_live_view_frame(frame: LiveViewFrame) -> QImage:
    """`QImage` copy of a `LiveViewFrame` — copied immediately: `rgb_bytes`
    is not guaranteed to outlive the frame it arrived with, and a `QImage`
    built straight from raw data only ever references that buffer."""
    image = QImage(
        frame.rgb_bytes, frame.width, frame.height, frame.width * 3, QImage.Format.Format_RGB888
    )
    return image.copy()


class _MeasuredFps:
    """Rolling average of real inter-frame arrival gaps — never the
    configured target, which the camera/USB link may not actually reach."""

    def __init__(self, window: int = _MEASURED_FPS_WINDOW) -> None:
        self._gaps: deque[float] = deque(maxlen=window)
        self._last_at: float | None = None

    def record(self, now: float) -> None:
        if self._last_at is not None and now > self._last_at:
            self._gaps.append(now - self._last_at)
        self._last_at = now

    def reset(self) -> None:
        self._gaps.clear()
        self._last_at = None

    def value(self) -> float | None:
        if not self._gaps:
            return None
        average_gap = sum(self._gaps) / len(self._gaps)
        return 1.0 / average_gap if average_gap > 0 else None


class _LiveViewStage(QWidget):
    """The image itself: displays the latest frame, handles expanded-mode
    zoom (wheel) and pan (drag) — collapsed mode is display-only."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self.expanded = False
        self._zoom = 1.0
        self._pan = QPointF(0.5, 0.5)  # normalized center of the visible crop
        self._drag_start: QPointF | None = None
        self._drag_start_pan: QPointF | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_image(self, image: QImage) -> None:
        self._pixmap = QPixmap.fromImage(image)
        self.update()

    def reset_view(self) -> None:
        self._zoom = 1.0
        self._pan = QPointF(0.5, 0.5)

    def zoom_in(self) -> None:
        self._set_zoom(self._zoom * _ZOOM_STEP)

    def zoom_out(self) -> None:
        self._set_zoom(self._zoom / _ZOOM_STEP)

    def _set_zoom(self, zoom: float) -> None:
        self._zoom = max(_ZOOM_MIN, min(_ZOOM_MAX, zoom))
        self._clamp_pan()
        self.update()

    def _clamp_pan(self) -> None:
        half = 0.5 / self._zoom
        x = min(max(self._pan.x(), half), 1 - half) if half < 0.5 else 0.5
        y = min(max(self._pan.y(), half), 1 - half) if half < 0.5 else 0.5
        self._pan = QPointF(x, y)

    def source_rect(self) -> QRectF:
        """Visible crop of `_pixmap`, in its own pixel coordinates."""
        assert self._pixmap is not None
        width, height = self._pixmap.width(), self._pixmap.height()
        crop_w, crop_h = width / self._zoom, height / self._zoom
        cx, cy = self._pan.x() * width, self._pan.y() * height
        return QRectF(cx - crop_w / 2, cy - crop_h / 2, crop_w, crop_h)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if self.expanded and self._zoom > 1.0:
            self._drag_start = event.position()
            self._drag_start_pan = QPointF(self._pan)
            event.accept()
            return
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_start is None or self._drag_start_pan is None or self._pixmap is None:
            super().mouseMoveEvent(event)
            return
        delta = event.position() - self._drag_start
        # Dragging right/down should reveal what's to the left/above —
        # panning the crop the opposite way of the mouse motion.
        dx = -delta.x() / max(1, self.width()) / self._zoom
        dy = -delta.y() / max(1, self.height()) / self._zoom
        self._pan = QPointF(self._drag_start_pan.x() + dx, self._drag_start_pan.y() + dy)
        self._clamp_pan()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        was_dragging = self._drag_start is not None
        self._drag_start = None
        self._drag_start_pan = None
        if event.button() == Qt.MouseButton.LeftButton and not was_dragging:
            self.clicked.emit()
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self.expanded:
            super().wheelEvent(event)
            return
        if event.angleDelta().y() > 0:
            self.zoom_in()
        else:
            self.zoom_out()
        event.accept()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(BORDER).darker(160))
        if self._pixmap is None or self._pixmap.isNull():
            return
        target = QRectF(self.rect())
        if self.expanded and self._zoom > 1.0:
            painter.drawPixmap(target, self._pixmap, self.source_rect())
        else:
            scaled = self._pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) / 2
            y = (self.height() - scaled.height()) / 2
            painter.drawPixmap(int(x), int(y), scaled)


class LiveViewWidget(QWidget):
    toggleRequested = Signal()  # L key / on-widget toggle button
    opacityChanged = Signal(float)  # 0.0-1.0, already persisted to `camera_config`
    fpsChanged = Signal(object)  # int | None, already persisted to `camera_config`
    # Expand/collapse changes this widget's own preferred geometry
    # (`size_for()`), but resizing an absolutely-positioned overlay is the
    # parent's job (`CaptureScreen._reposition_live_view`) — it only ever
    # gets called on the parent's own resize otherwise, so without this
    # signal, expanding never actually grows the widget until some
    # unrelated resize happens to trigger one.
    expandedChanged = Signal(bool)
    closeRequested = Signal()  # × button — panel hidden until View menu / shortcut

    def __init__(self, camera_config: CameraConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._camera_config = camera_config
        self._measured_fps = _MeasuredFps()
        self._live = False
        self._capturing = False

        self.stage = _LiveViewStage()
        self.stage.setToolTip(t("live_view.expand_tooltip"))
        self.stage.clicked.connect(self._on_stage_clicked)
        # Applied to the whole widget, not just `stage`: this is a plain
        # child overlay sitting on top of `PreviewArea`, not a top-level
        # window (`setWindowOpacity` wouldn't apply here) — fading the
        # whole thing is what actually lets the still preview underneath
        # show through, controls included.
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)

        self.live_badge = QLabel(f"● {t('live_view.live_badge')}")
        self.live_badge.setStyleSheet("color: #e2685c; font-weight: bold; font-size: 9pt;")
        self.live_badge.setVisible(False)

        self.capturing_label = QLabel(t("live_view.capturing"))
        self.capturing_label.setStyleSheet(
            "color: white; background: rgba(0, 0, 0, 160); font-weight: bold;"
        )
        self.capturing_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.capturing_label.setVisible(False)

        self.fps_measured_label = QLabel(t("live_view.fps_measured_pending"))
        self.fps_measured_label.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 8pt;")

        self.fps_combo = QComboBox()
        self.fps_combo.addItem(t("live_view.fps_unlimited"), None)
        for choice in LIVE_VIEW_FPS_CHOICES:
            self.fps_combo.addItem(str(choice), choice)
        self._select_fps_combo(camera_config.live_view_fps)
        self.fps_combo.currentIndexChanged.connect(self._on_fps_combo_changed)

        # Floor above 0: at 0 the *whole* widget fades out (see
        # `_apply_opacity`), controls included — with nothing left visible
        # to click, there would be no way back short of editing
        # config.json by hand.
        self.opacity_slider = SliderField(_MIN_OPACITY_PERCENT, 100, decimals=0, default=100)
        self.opacity_slider.setValue(round(camera_config.live_view_opacity * 100))
        self.opacity_slider.committed.connect(self._on_opacity_committed)

        self.toggle_button = QPushButton("●")
        self.toggle_button.setToolTip(t("live_view.toggle_tooltip"))
        self.toggle_button.setProperty("role", "live-view-icon")
        self.toggle_button.setFixedWidth(28)
        self.toggle_button.clicked.connect(self.toggleRequested.emit)

        self.shrink_button = QPushButton("⤡")
        self.shrink_button.setToolTip(t("live_view.collapse_tooltip"))
        self.shrink_button.setProperty("role", "live-view-icon")
        self.shrink_button.setFixedWidth(28)
        self.shrink_button.clicked.connect(self.collapse)
        self.shrink_button.setVisible(False)

        self.close_button = QPushButton("×")
        self.close_button.setToolTip(t("live_view.close_tooltip"))
        self.close_button.setProperty("role", "live-view-icon")
        self.close_button.setFixedWidth(28)
        self.close_button.clicked.connect(self.closeRequested.emit)

        top_row = QHBoxLayout()
        top_row.addWidget(self.live_badge)
        top_row.addStretch(1)
        top_row.addWidget(self.fps_measured_label)
        top_row.addWidget(self.shrink_button)
        top_row.addWidget(self.close_button)

        controls_row = QHBoxLayout()
        controls_row.addWidget(QLabel(t("live_view.fps_label")))
        controls_row.addWidget(self.fps_combo)
        controls_row.addWidget(QLabel(t("live_view.opacity_label")))
        controls_row.addWidget(self.opacity_slider, 1)
        controls_row.addWidget(self.toggle_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addLayout(top_row)
        layout.addWidget(self.stage, 1)
        layout.addLayout(controls_row)

        # Without this, a plain `QWidget` subclass never paints its own
        # stylesheet background/border (only its children) — same fix as
        # `conflict_panel`/`critical_banner` in `capture.py`.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"LiveViewWidget {{ background: #14171c; border: 1px solid {BORDER}; }}")
        self.expanded = False
        self._apply_opacity()

    # --- fed by CaptureScreen from CameraController callbacks --------------

    def show_frame(self, frame: LiveViewFrame, *, now: float | None = None) -> None:
        image = image_from_live_view_frame(frame)
        if self._camera_config.live_view_rotate_180:
            image = image.mirrored(True, True)
        self.stage.set_image(image)
        self._measured_fps.record(time.monotonic() if now is None else now)
        measured = self._measured_fps.value()
        self.fps_measured_label.setText(
            t("live_view.fps_measured", fps=measured)
            if measured is not None
            else t("live_view.fps_measured_pending")
        )

    def set_live(self, live: bool) -> None:
        self._live = live
        self.live_badge.setVisible(live)
        if not live:
            self._measured_fps.reset()
            self.fps_measured_label.setText(t("live_view.fps_measured_pending"))
            # Nothing left to check focus against once the feed itself
            # stops — an expanded panel showing a frozen last frame would
            # otherwise just sit there with no way to tell it's stale.
            if self.expanded:
                self.collapse()

    def is_live(self) -> bool:
        return self._live

    def show_capturing(self) -> None:
        self._capturing = True
        self.capturing_label.setVisible(True)
        self.capturing_label.raise_()

    def clear_capturing(self) -> None:
        self._capturing = False
        self.capturing_label.setVisible(False)

    # --- expand/collapse ----------------------------------------------------

    def _on_stage_clicked(self) -> None:
        # Only while actually live: a click on a static (non-streaming)
        # vignette has nothing to check focus against, and would otherwise
        # risk misfiring during normal capture-screen interaction near it.
        if not self._live:
            return
        if self.expanded:
            self.collapse()
        else:
            self.expand()

    def expand(self) -> None:
        self.expanded = True
        self.stage.expanded = True
        self.shrink_button.setVisible(True)
        self._apply_opacity()
        self.expandedChanged.emit(True)

    def collapse(self) -> None:
        self.expanded = False
        self.stage.expanded = False
        self.stage.reset_view()
        self.shrink_button.setVisible(False)
        self._apply_opacity()
        self.expandedChanged.emit(False)

    def size_for(self, container: QRectF) -> QRectF:
        """This widget's target geometry within `container` (the preview
        area's rect, in the same parent coordinate space): a small
        bottom-right vignette when collapsed, most of the area when
        expanded — both aspect-ratio-correct."""
        fraction = _EXPANDED_SIZE_FRACTION if self.expanded else _COLLAPSED_WIDTH_FRACTION
        width = container.width() * fraction
        height = width / _ASPECT
        if height > container.height() * fraction:
            height = container.height() * fraction
            width = height * _ASPECT
        if self.expanded:
            x = container.x() + (container.width() - width) / 2
            y = container.y() + (container.height() - height) / 2
        else:
            margin = 10
            x = container.right() - width - margin
            y = container.bottom() - height - margin
        return QRectF(x, y, width, height)

    # --- controls ------------------------------------------------------------

    def _select_fps_combo(self, fps: int | None) -> None:
        index = self.fps_combo.findData(fps)
        self.fps_combo.setCurrentIndex(max(0, index))

    def _on_fps_combo_changed(self, _index: int) -> None:
        fps = self.fps_combo.currentData()
        self._camera_config.live_view_fps = fps
        self.fpsChanged.emit(fps)

    def _on_opacity_committed(self, value: float) -> None:
        opacity = value / 100
        self._camera_config.live_view_opacity = opacity
        self._apply_opacity()
        self.opacityChanged.emit(opacity)

    def _effective_opacity(self) -> float:
        # Expanded is meant to fully replace the still preview, not layer
        # translucently over it — the configured opacity only applies to
        # the small picture-in-picture vignette.
        if self.expanded:
            return 1.0
        # Floored here too, not just on the slider: a `config.json` written
        # before the floor existed could still carry a stored 0.0.
        return max(_MIN_OPACITY_PERCENT / 100, self._camera_config.live_view_opacity)

    def _apply_opacity(self) -> None:
        self._opacity_effect.setOpacity(self._effective_opacity())
