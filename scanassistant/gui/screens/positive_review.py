"""Positive crop review — post-capture screen for images whose content
frame the automatic detector (`imaging.content_framing`) wasn't confident
enough to apply on its own.

Standalone window (like `StatisticsScreen`/the shortcuts help), not part of
the home/project/capture stack: a deliberate, separate stop for an operator
reviewing flagged images at the end of a session — usable outside of an
active capture (loads a session the same way `StatisticsScreen` does).

Keyboard-first for the single most frequent action — confirm this image and
move to the next — with mouse-drag (`PreviewArea`, reused as-is: the
content frame is always axis-aligned, so none of its rotation handling
applies here) for fine adjustment of the crop. Regenerates `jpeg_positive`
only, through `CaptureSession.apply_manual_positive_override` — never the
TIFF/JPEG master, whose geometry (the support frame) this never touches.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QListWidget,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from scanassistant.core.positive_review import list_deferred_positives
from scanassistant.core.session import CaptureSession
from scanassistant.gui.widgets.pin_checkbox import make_pin_checkbox
from scanassistant.gui.widgets.positive_settings_panel import PositiveSettingsPanel
from scanassistant.gui.widgets.preview_area import PreviewArea
from scanassistant.i18n import t
from scanassistant.imaging.framing import RELIABLE, ConfidenceComponents, FrameResult
from scanassistant.project.campaign import JpegPositiveExportConfig, ManualPositiveSettings
from scanassistant.project.layout import CampaignPaths

_DEFAULT_INSET_FRACTION = 0.05  # per side — a head start, not a guess: an
# operator reviewing a *deferred* image (nothing confidently auto-detected)
# still very rarely wants literally the whole support frame kept as-is.
_ZERO_COMPONENTS = ConfidenceComponents(0.0, 0.0, 0.0, 0.0, 0.0)


class PositiveReviewScreen(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("positive_review.title"))
        self.resize(960, 640)

        self._session: CaptureSession | None = None
        self._names: list[str] = []
        self._master_pixels: np.ndarray | None = None
        self._current_frame: FrameResult | None = None
        # Same object passed to `self.settings_panel.load(...)` — the panel
        # mutates `manual_settings` on it in place, so this reference always
        # reflects the operator's live edits without reaching into the
        # panel's own private state.
        self._exposure_config: JpegPositiveExportConfig | None = None

        self.category_deferred_checkbox = QCheckBox(t("positive_review.category_deferred"))
        self.category_deferred_checkbox.setChecked(True)
        self.category_deferred_checkbox.toggled.connect(self.refresh_list)

        self.list_widget = QListWidget()
        self.list_widget.currentRowChanged.connect(self._on_list_row_changed)

        self.preview_area = PreviewArea()
        self.preview_area.frame_dragged.connect(self._on_frame_dragged)

        self.settings_panel = PositiveSettingsPanel()

        self.confirm_button = QPushButton(t("positive_review.confirm_and_next"))
        self.confirm_button.clicked.connect(self.confirm_current)
        self.confirm_button.setEnabled(False)

        self.status_label = QLabel()
        self.status_label.setProperty("role", "secondary")
        self.status_label.setWordWrap(True)

        list_column = QVBoxLayout()
        list_column.addWidget(self.category_deferred_checkbox)
        list_column.addWidget(self.list_widget, 1)
        list_container = QWidget()
        list_container.setLayout(list_column)

        detail_column = QVBoxLayout()
        detail_column.addWidget(self.preview_area, 1)
        detail_column.addWidget(self.settings_panel)
        detail_column.addWidget(self.confirm_button)
        detail_container = QWidget()
        detail_container.setLayout(detail_column)

        splitter = QSplitter()
        splitter.addWidget(list_container)
        splitter.addWidget(detail_container)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(make_pin_checkbox(self))
        layout.addWidget(splitter, 1)
        layout.addWidget(self.status_label)

    def load(self, session: CaptureSession) -> None:
        """Binds to a campaign and refreshes the list (safe to call again
        on every open — same convention as `StatisticsScreen.load`)."""
        self._session = session
        self.refresh_list()

    def refresh_list(self) -> None:
        session = self._session
        if session is None or not self.category_deferred_checkbox.isChecked():
            self._names = []
        else:
            self._names = list_deferred_positives(session.paths, session.fs)
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        self.list_widget.addItems(self._names)
        self.list_widget.blockSignals(False)
        self.list_widget.setCurrentRow(0 if self._names else -1)
        if not self._names:
            self._load_index(-1)

    def _on_list_row_changed(self, row: int) -> None:
        self._load_index(row)

    def _load_index(self, index: int) -> None:
        session = self._session
        if session is None or index < 0 or index >= len(self._names):
            self._master_pixels = None
            self._current_frame = None
            self.confirm_button.setEnabled(False)
            self.preview_area.show_message(t("positive_review.nothing_to_review"))
            self.status_label.setText("")
            return
        name = self._names[index]
        self.status_label.setText(
            t("positive_review.reviewing", index=index + 1, total=len(self._names), name=name)
        )
        pixels = _load_master_jpeg(session.paths, name)
        if pixels is None:
            self._master_pixels = None
            self._current_frame = None
            self.confirm_button.setEnabled(False)
            self.preview_area.show_message(t("positive_review.master_unavailable", name=name))
            return
        self._master_pixels = pixels
        height, width = pixels.shape[:2]
        self._current_frame = _default_content_frame(width, height)
        self.confirm_button.setEnabled(True)
        self.preview_area.show_image(pixels)
        self.preview_area.set_frame_overlay(self._current_frame)
        manual = session.campaign.exports.jpeg_positive.manual_settings
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
        self.settings_panel.load(self._exposure_config)

    def _on_frame_dragged(self, frame: FrameResult) -> None:
        self._current_frame = frame

    def confirm_current(self) -> None:
        """Applies the current crop/exposure and advances to the next
        flagged image (Enter) — the confirmed image drops out of the
        "needs review" list (`core.positive_review`), it isn't re-added."""
        session = self._session
        row = self.list_widget.currentRow()
        if session is None or self._master_pixels is None or self._current_frame is None or row < 0:
            return
        name = self._names[row]
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
        self.refresh_list()

    def keyPressEvent(self, event: QKeyEvent) -> None:
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


def _load_master_jpeg(paths: CampaignPaths, name: str) -> np.ndarray | None:
    path = Path(paths.jpeg_master_dir) / f"{name}.jpg"
    try:
        with Image.open(path) as image:
            return np.array(image.convert("RGB"))
    except OSError:
        return None
