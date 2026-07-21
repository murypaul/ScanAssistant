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
    QResizeEvent,
    QShowEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QComboBox,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from scanassistant.camera.backend import LiveViewFrame
from scanassistant.config import LIVE_VIEW_FPS_CHOICES, CameraConfig
from scanassistant.gui.theme import ACCENT, BORDER
from scanassistant.gui.widgets.slider_field import SliderField
from scanassistant.i18n import t


def _floating_text_shadow() -> QGraphicsDropShadowEffect:
    """A soft black shadow behind overlay text/icons — QSS has no
    `text-shadow`, and these sit directly over the live video with no
    backing band, unlike normal chrome elsewhere in the app."""
    effect = QGraphicsDropShadowEffect()
    effect.setBlurRadius(6)
    effect.setOffset(0, 1)
    effect.setColor(QColor(0, 0, 0, 220))
    return effect


_COLLAPSED_WIDTH_FRACTION = 0.32
_EXPANDED_SIZE_FRACTION = 0.9
_ASPECT = 4 / 3
_MEASURED_FPS_WINDOW = 8  # rolling average over this many inter-frame gaps
_FPS_LABEL_UPDATE_INTERVAL_S = 1.0  # display refresh — separate from the averaging window itself
_ZOOM_MIN, _ZOOM_MAX = 1.0, 6.0
_ZOOM_STEP = 1.15
_LIVE_DOT_COLOR = QColor("#e2685c")
_MIN_OPACITY_PERCENT = 15

# Buckets the continuous digital zoom (wheel, `_ZOOM_MIN`-`_ZOOM_MAX`) into
# the camera's own discrete hardware zoom steps (level 0 = unzoomed; see
# `GphotoCameraBackend.set_live_view_zoom_level`) — the same wheel gesture
# drives both at once rather than adding a separate control for the
# hardware side, so the vignette stays exactly as small as before while
# still asking the camera itself for real extra detail as the operator
# zooms in further.
_HARDWARE_ZOOM_THRESHOLDS = (1.0, 2.0, 3.0, 4.5)

# Once a hardware level is engaged, the *additional* digital crop on top of
# it (see `_LiveViewStage._digital_crop_factor`) is capped to this — a
# small in-band fine-tune rather than the full `_ZOOM_MAX` stacked on top of
# an already-tighter camera-side crop, confirmed on the real D750 to bury
# the jump in real detail under a much blurrier digital blow-up otherwise.
# Trade-off accepted (DECISIONS.md I-154): on a body where the hardware
# request silently no-ops, this is less digital zoom range than before.
_LOCAL_DIGITAL_ZOOM_MAX = 1.5


def _hardware_zoom_level(zoom: float) -> int:
    for level, threshold in enumerate(_HARDWARE_ZOOM_THRESHOLDS):
        if zoom <= threshold:
            return level
    return len(_HARDWARE_ZOOM_THRESHOLDS)


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
    hardwareZoomLevelChanged = Signal(int)  # 0 = unzoomed, see `_hardware_zoom_level`
    zoomAreaDragged = Signal(int, int)  # (dx, dy) screen pixels since the last move event

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self.expanded = False
        self._zoom = 1.0
        self._pan = QPointF(0.5, 0.5)  # normalized center of the visible crop
        self._drag_start: QPointF | None = None
        self._drag_start_pan: QPointF | None = None
        self._last_drag_pos: QPointF | None = None
        self._hardware_zoom_level = 0
        self._top_overlay: QWidget | None = None
        self._center_overlay: QWidget | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_top_overlay(self, widget: QWidget) -> None:
        """A control bar (live badge/fps/close) drawn over the top edge of
        the image instead of a row taking up space above it — reparented
        here so it always tracks the stage's own size, the space it used to
        need going to the image itself instead."""
        self._top_overlay = widget
        widget.setParent(self)
        self._position_top_overlay()

    def set_center_overlay(self, widget: QWidget) -> None:
        """The "Capturing…" label, centered over the image — reparented
        here rather than left parentless (its previous state made it an
        unparented top-level widget: `show_capturing()` popped up a stray,
        unstyled little window at an arbitrary screen position instead of
        overlaying the live view)."""
        self._center_overlay = widget
        widget.setParent(self)
        self._position_center_overlay()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_top_overlay()
        self._position_center_overlay()

    def _position_top_overlay(self) -> None:
        if self._top_overlay is None:
            return
        height = self._top_overlay.sizeHint().height()
        self._top_overlay.setGeometry(0, 0, self.width(), height)

    def _position_center_overlay(self) -> None:
        if self._center_overlay is None:
            return
        self._center_overlay.setGeometry(0, 0, self.width(), self.height())

    def set_image(self, image: QImage) -> None:
        self._pixmap = QPixmap.fromImage(image)
        self.update()

    def reset_view(self) -> None:
        self._zoom = 1.0
        self._update_hardware_zoom_level()
        self._pan = QPointF(0.5, 0.5)

    def zoom_in(self) -> None:
        self._set_zoom(self._zoom * _ZOOM_STEP)

    def zoom_out(self) -> None:
        self._set_zoom(self._zoom / _ZOOM_STEP)

    def _set_zoom(self, zoom: float) -> None:
        self._zoom = max(_ZOOM_MIN, min(_ZOOM_MAX, zoom))
        # Level must be current *before* clamping the pan below —
        # `_clamp_pan`/`source_rect` size the crop off `_hardware_zoom_level`
        # (see `_digital_crop_factor`), so clamping against the previous
        # level here would use a stale (about-to-change) band boundary.
        self._update_hardware_zoom_level()
        self._clamp_pan()
        self.update()

    def _update_hardware_zoom_level(self) -> None:
        level = _hardware_zoom_level(self._zoom)
        if level != self._hardware_zoom_level:
            self._hardware_zoom_level = level
            self.hardwareZoomLevelChanged.emit(level)

    def _digital_crop_factor(self) -> float:
        """How much *additional* digital cropping to layer on top of
        whatever the camera itself already delivered — `self._zoom`
        directly at level 0 (nothing requested camera-side yet), otherwise
        just the fraction of progress through the current hardware band,
        capped to `_LOCAL_DIGITAL_ZOOM_MAX` (see its own comment)."""
        level = self._hardware_zoom_level
        if level <= 0:
            return self._zoom
        band_lo = _HARDWARE_ZOOM_THRESHOLDS[level - 1]
        band_hi = (
            _HARDWARE_ZOOM_THRESHOLDS[level]
            if level < len(_HARDWARE_ZOOM_THRESHOLDS)
            else _ZOOM_MAX
        )
        fraction = (self._zoom - band_lo) / (band_hi - band_lo) if band_hi > band_lo else 0.0
        fraction = max(0.0, min(1.0, fraction))
        return 1.0 + fraction * (_LOCAL_DIGITAL_ZOOM_MAX - 1.0)

    def _clamp_pan(self) -> None:
        half = 0.5 / self._digital_crop_factor()
        x = min(max(self._pan.x(), half), 1 - half) if half < 0.5 else 0.5
        y = min(max(self._pan.y(), half), 1 - half) if half < 0.5 else 0.5
        self._pan = QPointF(x, y)

    def source_rect(self) -> QRectF:
        """Visible crop of `_pixmap`, in its own pixel coordinates."""
        assert self._pixmap is not None
        width, height = self._pixmap.width(), self._pixmap.height()
        crop_factor = self._digital_crop_factor()
        crop_w, crop_h = width / crop_factor, height / crop_factor
        cx, cy = self._pan.x() * width, self._pan.y() * height
        return QRectF(cx - crop_w / 2, cy - crop_h / 2, crop_w, crop_h)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if self.expanded and self._zoom > 1.0:
            self._drag_start = event.position()
            self._drag_start_pan = QPointF(self._pan)
            self._last_drag_pos = event.position()
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

        # Hardware pan (see `GphotoCameraBackend.move_live_view_zoom_area`)
        # needs the step since the *last* event, not the cumulative delta
        # from drag start above — it's a nudge on the camera side, not an
        # absolute position. Same "opposite of mouse motion" direction as
        # the digital pan for a consistent feel between the two.
        if self._last_drag_pos is not None and self._hardware_zoom_level > 0:
            step = event.position() - self._last_drag_pos
            if step.x() or step.y():
                self.zoomAreaDragged.emit(round(-step.x()), round(-step.y()))
        self._last_drag_pos = event.position()

        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._last_drag_pos = None
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
    hardwareZoomLevelChanged = Signal(int)  # forwarded from `_LiveViewStage`
    zoomAreaDragged = Signal(int, int)  # forwarded from `_LiveViewStage`

    def __init__(self, camera_config: CameraConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._camera_config = camera_config
        self._measured_fps = _MeasuredFps()
        self._live = False
        self._capturing = False

        self.stage = _LiveViewStage()
        self.stage.setToolTip(t("live_view.expand_tooltip"))
        self.stage.clicked.connect(self._on_stage_clicked)
        self.stage.hardwareZoomLevelChanged.connect(self.hardwareZoomLevelChanged.emit)
        self.stage.zoomAreaDragged.connect(self.zoomAreaDragged.emit)
        # Applied to the whole widget, not just `stage`: this is a plain
        # child overlay sitting on top of `PreviewArea`, not a top-level
        # window (`setWindowOpacity` wouldn't apply here) — fading the
        # whole thing is what actually lets the still preview underneath
        # show through, controls included.
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)

        self.live_badge = QLabel(f"● {t('live_view.live_badge')}")
        self.live_badge.setStyleSheet("color: #e2685c; font-weight: bold; font-size: 9pt;")
        self.live_badge.setGraphicsEffect(_floating_text_shadow())
        self.live_badge.setVisible(False)

        self.capturing_label = QLabel(t("live_view.capturing"))
        self.capturing_label.setStyleSheet(
            "color: white; background: rgba(0, 0, 0, 160); font-weight: bold;"
        )
        self.capturing_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.capturing_label.setVisible(False)
        self.stage.set_center_overlay(self.capturing_label)

        self.fps_measured_label = QLabel(t("live_view.fps_measured_pending"))
        self.fps_measured_label.setStyleSheet("color: white; font-size: 8pt;")
        self.fps_measured_label.setGraphicsEffect(_floating_text_shadow())
        self._fps_label_updated_at: float | None = None

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

        # Floating directly over the video, not on a backing band: no box,
        # a soft shadow instead (`_floating_text_shadow`) — same reasoning
        # as `live_badge`/`fps_measured_label` above.
        self.shrink_button = QPushButton("⤡")
        self.shrink_button.setToolTip(t("live_view.collapse_tooltip"))
        self.shrink_button.setFixedSize(24, 24)
        self.shrink_button.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: white;"
            " font-size: 12pt; }"
            f" QPushButton:hover {{ color: {ACCENT}; }}"
        )
        self.shrink_button.setGraphicsEffect(_floating_text_shadow())
        self.shrink_button.clicked.connect(self.collapse)
        self.shrink_button.setVisible(False)

        # The one overlay control kept as a small filled shape rather than
        # a bare glyph — a close button floating with nothing but a shadow
        # reads as part of the image, not as something to click.
        self.close_button = QPushButton("×")
        self.close_button.setToolTip(t("live_view.close_tooltip"))
        self.close_button.setFixedSize(22, 22)
        self.close_button.setStyleSheet(
            "QPushButton { background: rgba(0, 0, 0, 110); border: none;"
            " border-radius: 11px; color: white; font-weight: bold; }"
            " QPushButton:hover { background: rgba(0, 0, 0, 170); }"
        )
        self.close_button.clicked.connect(self.closeRequested.emit)

        # No backing band: the global QSS fills every plain QWidget with
        # the app's opaque background, which would otherwise paint a grey
        # bar over the top of the video — floating text/icons carry their
        # own shadow or rond instead (see `_floating_text_shadow`,
        # `close_button`), so nothing is lost by making this transparent.
        top_overlay = QWidget()
        top_overlay.setStyleSheet("background: transparent;")
        top_row = QHBoxLayout(top_overlay)
        top_row.setContentsMargins(6, 3, 6, 3)
        top_row.addWidget(self.live_badge)
        top_row.addStretch(1)
        top_row.addWidget(self.fps_measured_label)
        top_row.addWidget(self.shrink_button)
        top_row.addWidget(self.close_button)
        self.stage.set_top_overlay(top_overlay)

        controls_row = QHBoxLayout()
        controls_row.addWidget(QLabel(t("live_view.fps_label")))
        controls_row.addWidget(self.fps_combo)
        controls_row.addWidget(QLabel(t("live_view.opacity_label")))
        controls_row.addWidget(self.opacity_slider, 1)
        controls_row.addWidget(self.toggle_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(self.stage, 1)
        layout.addLayout(controls_row)

        # Without this, a plain `QWidget` subclass never paints its own
        # stylesheet background/border (only its children) — same fix as
        # `conflict_panel`/`critical_banner` in `capture.py`.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"LiveViewWidget {{ background: #14171c; border: 1px solid {BORDER}; }}")
        self.expanded = False
        self._apply_opacity()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # Deferred to actually showing, not set once in `__init__`: a bare
        # `sizeHint()` measured before the widget is shown in its real
        # parent chain (and styled by the app's global stylesheet) doesn't
        # reliably match `fps_combo`'s own final rendered height.
        self.toggle_button.setFixedHeight(self.fps_combo.height())

    # --- fed by CaptureScreen from CameraController callbacks --------------

    def show_frame(self, frame: LiveViewFrame, *, now: float | None = None) -> None:
        image = image_from_live_view_frame(frame)
        if self._camera_config.live_view_rotate_180:
            image = image.mirrored(True, True)
        self.stage.set_image(image)
        current = time.monotonic() if now is None else now
        self._measured_fps.record(current)
        # The rolling average (`_MeasuredFps`) already smooths the value
        # itself — this throttles how often the *label* repaints, since a
        # number changing every single frame reads as jittery noise rather
        # than a measurement, however stable the underlying value is.
        if (
            self._fps_label_updated_at is None
            or current - self._fps_label_updated_at >= _FPS_LABEL_UPDATE_INTERVAL_S
        ):
            self._fps_label_updated_at = current
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
            self._fps_label_updated_at = None
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
