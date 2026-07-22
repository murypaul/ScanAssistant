"""Positive crop review — post-capture screen for images whose content
frame the automatic detector (`imaging.content_framing`) wasn't confident
enough to apply on its own.

Part of the main window's screen stack, alongside Project/Capture (a full
takeover, not a floating utility window like `StatisticsScreen`/the
shortcuts help) — a deliberate, separate stop for an operator reviewing
flagged images at the end of a session — usable outside of an active
capture (loads a session the same way `StatisticsScreen` does).

Keyboard-first for the single most frequent action — confirm this image and
move to the next — with mouse-drag (`PreviewArea`, reused as-is: the
content frame is always axis-aligned, so none of its rotation handling
applies here) for fine adjustment of the crop. Regenerates `jpeg_positive`
only, through `CaptureSession.apply_manual_positive_override` — never the
TIFF/JPEG master, whose geometry (the support frame) this never touches.

The preview itself is rendered as a positive (`imaging.positive.render_positive`,
same call the real export makes), not the raw negative JPEG_MASTER — an
operator can't judge a crop or an exposure setting against inverted colors.
Re-rendered live as the settings panel changes, same `live_changed`
(downscaled, mid-drag)/`settled_changed` (full resolution) split
`gui.screens.capture` already uses for its own positive preview.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QListWidget,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from scanassistant.core.positive_review import (
    list_positives_by_category,
    reconstruct_content_frame_fraction,
)
from scanassistant.core.session import CaptureSession
from scanassistant.gui.widgets.histogram_widget import HistogramWidget
from scanassistant.gui.widgets.positive_settings_panel import PositiveSettingsPanel
from scanassistant.gui.widgets.preview_area import PreviewArea
from scanassistant.i18n import t
from scanassistant.imaging.framing import RELIABLE, ConfidenceComponents, FrameResult
from scanassistant.imaging.positive import ManualSettings, render_positive
from scanassistant.project.campaign import JpegPositiveExportConfig, ManualPositiveSettings
from scanassistant.project.layout import CampaignPaths

_DEFAULT_INSET_FRACTION = 0.05  # per side — a head start, not a guess: an
# operator reviewing a *deferred* image (nothing confidently auto-detected)
# still very rarely wants literally the whole support frame kept as-is.
_ZERO_COMPONENTS = ConfidenceComponents(0.0, 0.0, 0.0, 0.0, 0.0)

# Matches `gui.screens.capture`'s own histogram overlay placement/sizing.
_HISTOGRAM_WIDTH_FRACTION = 0.09
_HISTOGRAM_ASPECT = 2.2  # width / height

# Matches `gui.screens.capture._FAST_PREVIEW_MAX_DIM`: the tone-curve math
# (`render_positive`) runs on every mouse-move while a settings slider is
# being dragged, expensive enough at full master resolution to stutter.
_FAST_PREVIEW_MAX_DIM = 480
# `render_positive` on a full-resolution JPEG_MASTER (`long_edge_px` 0 by
# default, i.e. full RAW size) measured ~2.5s on a 24 MP array — a multi-
# second freeze on every image navigation and every slider release, for a
# screen whose whole point is judging a crop/exposure by eye, not exporting.
# 2000px (~0.2s) is indistinguishable from full resolution at that purpose.
_DISPLAY_PREVIEW_MAX_DIM = 2000


def _downscaled_for_preview(
    pixels: np.ndarray, frame: FrameResult, max_dim: int
) -> tuple[np.ndarray, FrameResult, float]:
    """Returns the scale actually used (1.0 if `pixels` was already smaller
    than `max_dim`) — the caller needs it to convert a drag on the (possibly
    downscaled) displayed frame back to `pixels`'/`master_pixels`' own,
    full-resolution coordinate space."""
    height, width = pixels.shape[:2]
    scale = max_dim / max(height, width)
    if scale >= 1.0:
        return pixels, frame, 1.0
    resized = cv2.resize(
        pixels, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA
    )
    scaled_frame = replace(
        frame,
        x=frame.x * scale,
        y=frame.y * scale,
        width=frame.width * scale,
        height=frame.height * scale,
    )
    return resized, scaled_frame, scale


class PositiveReviewScreen(QWidget):
    closed = Signal()  # Escape — back to the Project screen (`MainWindow`)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._session: CaptureSession | None = None
        self._names: list[str] = []
        self._master_pixels: np.ndarray | None = None
        self._current_name: str | None = None
        self._current_frame: FrameResult | None = None
        # Scale of the currently *displayed* preview relative to
        # `self._master_pixels` (1.0 = full resolution) — `self._current_frame`
        # itself always stays in `self._master_pixels`' own coordinate space
        # (what `confirm_current` needs), so a drag on a downscaled preview
        # must be converted back through this before being stored.
        self._preview_scale: float = 1.0
        # Same object passed to `self.settings_panel.load(...)` — the panel
        # mutates `manual_settings` on it in place, so this reference always
        # reflects the operator's live edits without reaching into the
        # panel's own private state.
        self._exposure_config: JpegPositiveExportConfig | None = None
        # In-progress crop/exposure edits, keyed by name, for images the
        # operator dragged/adjusted but hasn't confirmed yet: without this,
        # navigating to another row and back would silently replace them
        # with the default inset / campaign-wide settings again.
        self._pending_frames: dict[str, FrameResult] = {}
        self._pending_exposures: dict[str, ManualPositiveSettings] = {}

        self.category_deferred_checkbox = QCheckBox(t("positive_review.category_deferred"))
        self.category_deferred_checkbox.setChecked(True)
        self.category_deferred_checkbox.toggled.connect(self.refresh_list)

        self.category_applied_checkbox = QCheckBox(t("positive_review.category_applied"))
        self.category_applied_checkbox.setChecked(False)
        self.category_applied_checkbox.toggled.connect(self.refresh_list)

        self.category_manual_checkbox = QCheckBox(t("positive_review.category_manual"))
        self.category_manual_checkbox.setChecked(False)
        self.category_manual_checkbox.toggled.connect(self.refresh_list)

        self.list_widget = QListWidget()
        self.list_widget.currentRowChanged.connect(self._on_list_row_changed)

        self.preview_area = PreviewArea()
        self.preview_area.frame_dragged.connect(self._on_frame_dragged)

        self.histogram_widget = HistogramWidget(parent=self)

        self.settings_panel = PositiveSettingsPanel()
        self.settings_panel.live_changed.connect(lambda: self._refresh_preview(fast=True))
        self.settings_panel.settled_changed.connect(self._refresh_preview)

        self.confirm_button = QPushButton(t("positive_review.confirm_and_next"))
        self.confirm_button.clicked.connect(self.confirm_current)
        self.confirm_button.setEnabled(False)

        self.status_label = QLabel()
        self.status_label.setProperty("role", "secondary")
        self.status_label.setWordWrap(True)

        self.back_hint_label = QLabel(t("positive_review.back_hint"))
        self.back_hint_label.setProperty("role", "secondary")

        list_column = QVBoxLayout()
        list_column.addWidget(self.category_deferred_checkbox)
        list_column.addWidget(self.category_applied_checkbox)
        list_column.addWidget(self.category_manual_checkbox)
        list_column.addWidget(self.list_widget, 1)
        list_container = QWidget()
        list_container.setLayout(list_column)
        # Narrow by default: the preview is the whole point of this screen,
        # the image list only needs enough width to read a name.
        list_container.setMaximumWidth(260)

        settings_column = QVBoxLayout()
        settings_column.addWidget(self.settings_panel)
        settings_column.addWidget(self.confirm_button)
        settings_container = QWidget()
        settings_container.setLayout(settings_column)
        settings_container.setMaximumWidth(300)

        detail_row = QSplitter()
        detail_row.addWidget(self.preview_area)
        detail_row.addWidget(settings_container)
        detail_row.setStretchFactor(0, 4)
        detail_row.setStretchFactor(1, 1)

        splitter = QSplitter()
        splitter.addWidget(list_container)
        splitter.addWidget(detail_row)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)

        layout = QVBoxLayout(self)
        layout.addWidget(self.back_hint_label)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.status_label)

    def load(self, session: CaptureSession) -> None:
        """Binds to a campaign and refreshes the list (safe to call again
        on every open — same convention as `StatisticsScreen.load`)."""
        self._session = session
        self.refresh_list()

    def refresh_list(self, *, select_name: str | None = None) -> None:
        session = self._session
        categories: set[str] = set()
        if self.category_deferred_checkbox.isChecked():
            categories.add("deferred")
        if self.category_applied_checkbox.isChecked():
            categories.add("applied")
        if self.category_manual_checkbox.isChecked():
            categories.add("manual")
        if session is None or not categories:
            self._names = []
        else:
            self._names = list_positives_by_category(
                session.paths, session.fs, frozenset(categories)
            )
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        self.list_widget.addItems(self._names)
        self.list_widget.blockSignals(False)
        target_row = self._names.index(select_name) if select_name in self._names else 0
        self.list_widget.setCurrentRow(target_row if self._names else -1)
        if not self._names:
            self._load_index(-1)

    def _on_list_row_changed(self, row: int) -> None:
        self._load_index(row)

    def _load_index(self, index: int) -> None:
        self._save_pending_edits()
        session = self._session
        if session is None or index < 0 or index >= len(self._names):
            self._master_pixels = None
            self._current_name = None
            self._current_frame = None
            self.confirm_button.setEnabled(False)
            self.preview_area.show_message(t("positive_review.nothing_to_review"))
            self.status_label.setText("")
            self.histogram_widget.set_pixels(None)
            return
        name = self._names[index]
        self._current_name = name
        self.status_label.setText(
            t("positive_review.reviewing", index=index + 1, total=len(self._names), name=name)
        )
        pixels = _load_master_jpeg(session.paths, name)
        if pixels is None:
            self._master_pixels = None
            self._current_frame = None
            self.confirm_button.setEnabled(False)
            self.preview_area.show_message(t("positive_review.master_unavailable", name=name))
            self.histogram_widget.set_pixels(None)
            return
        self._master_pixels = pixels
        height, width = pixels.shape[:2]
        fraction = reconstruct_content_frame_fraction(session.paths, session.fs, name)
        self._current_frame = (
            self._pending_frames.get(name)
            or (_frame_from_fraction(fraction, width, height) if fraction else None)
            or _default_content_frame(width, height)
        )
        self.confirm_button.setEnabled(True)
        manual = (
            self._pending_exposures.get(name)
            or session.campaign.exports.jpeg_positive.manual_settings
        )
        self._exposure_config = JpegPositiveExportConfig(
            mode="manual",
            horizontal_flip=session.campaign.exports.jpeg_positive.horizontal_flip,
            manual_settings=ManualPositiveSettings(
                exposure_ev=manual.exposure_ev,
                contrast=manual.contrast,
                shadows=manual.shadows,
                highlights=manual.highlights,
            ),
        )
        self._refresh_preview()
        self.settings_panel.load(self._exposure_config)

    def _save_pending_edits(self) -> None:
        """Remembers the in-progress crop/exposure for the image being
        navigated away from, so it's restored (instead of reset to the
        default inset / campaign settings) if the operator comes back to it
        before confirming."""
        name = self._current_name
        if name is None:
            return
        if self._current_frame is not None:
            self._pending_frames[name] = self._current_frame
        if self._exposure_config is not None:
            self._pending_exposures[name] = self._exposure_config.manual_settings

    def _on_frame_dragged(self, frame: FrameResult) -> None:
        """`frame` is in the currently *displayed* (possibly downscaled)
        preview's coordinate space — converted back to `self._master_pixels`'
        own, full-resolution space (`self._preview_scale`) before being
        stored, so `confirm_current`'s fractions stay correct regardless of
        how small the on-screen preview was downscaled to."""
        scale = self._preview_scale
        self._current_frame = (
            frame
            if scale == 1.0
            else replace(
                frame,
                x=frame.x / scale,
                y=frame.y / scale,
                width=frame.width / scale,
                height=frame.height / scale,
            )
        )

    def _refresh_preview(self, *, fast: bool = False) -> None:
        """Re-renders the positive preview from `self._master_pixels`
        (already in memory, no RAW redecode) using the settings panel's
        current values — called on load and on every settings-panel change.
        Always downscaled first (`_DISPLAY_PREVIEW_MAX_DIM`, tighter still
        — `_FAST_PREVIEW_MAX_DIM` — while `fast` is set, mid-drag on a
        slider): rendering `render_positive` at full JPEG_MASTER resolution
        measured multiple seconds, an unacceptable freeze for a screen this
        frequently used, on every navigation and every slider release."""
        if self._master_pixels is None or self._current_frame is None or self._session is None:
            return
        max_dim = _FAST_PREVIEW_MAX_DIM if fast else _DISPLAY_PREVIEW_MAX_DIM
        pixels, frame, scale = _downscaled_for_preview(
            self._master_pixels, self._current_frame, max_dim
        )
        self._preview_scale = scale
        positive = self._render_positive(pixels)
        self.preview_area.show_image(positive)
        self.preview_area.set_frame_overlay(frame)
        self.histogram_widget.set_pixels(positive)
        # Not just `resizeEvent`: the nested `QSplitter` (list/preview/
        # settings) only finishes distributing space to its children once
        # the screen is actually shown, after the screen's own last resize
        # already fired — repositioning here as well catches that case.
        self._reposition_histogram()

    def _render_positive(self, pixels: np.ndarray) -> np.ndarray:
        """The positive rendering an operator judges the crop/exposure
        against — same `render_positive` call the real `jpeg_positive`
        export makes, always in `manual` mode (this screen's settings panel
        never offers auto/simple, see `_load_index`), so what's on screen
        always matches what `confirm_current` is about to write."""
        assert self._session is not None
        config = self._session.campaign.exports.jpeg_positive
        manual = (
            self._exposure_config.manual_settings
            if self._exposure_config
            else ManualPositiveSettings()
        )
        array16 = pixels.astype(np.uint16) * 257
        positive16 = render_positive(
            array16,
            horizontal_flip=config.horizontal_flip,
            mode="manual",
            manual=ManualSettings(
                exposure_ev=manual.exposure_ev,
                contrast=manual.contrast,
                shadows=manual.shadows,
                highlights=manual.highlights,
            ),
        )
        positive8 = (positive16 // 257).astype(np.uint8)
        return np.stack([positive8, positive8, positive8], axis=-1)

    def confirm_current(self) -> None:
        """Applies the current crop/exposure and advances to the next
        flagged image (Enter). The confirmed image no longer drops out of
        the list on its own once more than one category checkbox is
        checked — confirming an `applied` image while "Already confirmed
        manually" is also checked leaves it visible, now under `manual` —
        so this always explicitly selects whatever followed it, rather than
        relying on `refresh_list`'s default (row 0), which would otherwise
        re-select the same image forever whenever it's also the very first
        one ever logged in the campaign."""
        session = self._session
        row = self.list_widget.currentRow()
        if session is None or self._master_pixels is None or self._current_frame is None or row < 0:
            return
        name = self._names[row]
        next_name = self._names[row + 1] if row + 1 < len(self._names) else None
        height, width = self._master_pixels.shape[:2]
        frame = self._current_frame
        content_frame = (
            frame.x / width,
            frame.y / height,
            frame.width / width,
            frame.height / height,
        )
        manual = self._exposure_config.manual_settings if self._exposure_config else None
        settings = (
            (manual.exposure_ev, manual.contrast, manual.shadows, manual.highlights)
            if manual is not None
            else None
        )
        session.apply_manual_positive_override(name, content_frame=content_frame, settings=settings)
        self._pending_frames.pop(name, None)
        self._pending_exposures.pop(name, None)
        self._current_name = None
        self.refresh_list(select_name=next_name)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.closed.emit()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.confirm_current()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Down:
            self._move(1)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Up:
            self._move(-1)
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._reposition_histogram()

    def _reposition_histogram(self) -> None:
        # Bottom-left of the preview area — same corner/sizing as
        # `gui.screens.capture`'s own histogram overlay. `mapTo`, not
        # `geometry()` directly: unlike `capture.CaptureScreen`, where
        # `preview_area` is a direct child, here it's nested inside the
        # detail/settings `QSplitter` — `geometry()` alone would be relative
        # to that splitter, not to this screen, and land over the image list.
        top_left = self.preview_area.mapTo(self, self.preview_area.rect().topLeft())
        size = self.preview_area.size()
        width = size.width() * _HISTOGRAM_WIDTH_FRACTION
        height = width / _HISTOGRAM_ASPECT
        margin = 10
        self.histogram_widget.setGeometry(
            round(top_left.x() + margin),
            round(top_left.y() + size.height() - height - margin),
            round(width),
            round(height),
        )
        self.histogram_widget.raise_()

    def _move(self, delta: int) -> None:
        if not self._names:
            return
        row = max(0, min(len(self._names) - 1, self.list_widget.currentRow() + delta))
        self.list_widget.setCurrentRow(row)


def _default_content_frame(width: int, height: int) -> FrameResult:
    inset_x = round(width * _DEFAULT_INSET_FRACTION)
    inset_y = round(height * _DEFAULT_INSET_FRACTION)
    return FrameResult(
        x=inset_x,
        y=inset_y,
        width=max(1, width - 2 * inset_x),
        height=max(1, height - 2 * inset_y),
        angle_deg=0.0,
        confidence=1.0,
        level=RELIABLE,
        components=_ZERO_COMPONENTS,
    )


def _frame_from_fraction(
    fraction: tuple[float, float, float, float], width: int, height: int
) -> FrameResult:
    """Rebuilds the frame last applied/confirmed for this image (fractions
    of `master.pixels`' own width/height, resolution-independent) against
    the currently displayed JPEG_MASTER's own pixel dimensions."""
    x_frac, y_frac, w_frac, h_frac = fraction
    return FrameResult(
        x=round(x_frac * width),
        y=round(y_frac * height),
        width=max(1, round(w_frac * width)),
        height=max(1, round(h_frac * height)),
        angle_deg=0.0,
        confidence=1.0,
        level=RELIABLE,
        components=_ZERO_COMPONENTS,
    )


def _load_master_jpeg(paths: CampaignPaths, name: str) -> np.ndarray | None:
    path = Path(paths.jpeg_master_dir) / f"{name}.jpg"
    try:
        with Image.open(path) as image:
            return np.array(image.convert("RGB"))
    except OSError:
        return None
