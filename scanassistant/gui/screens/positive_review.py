"""Positive calibration screen — post-capture screen for reviewing and
adjusting positives (13_INVERSION_NEGATIFS.md §9, 06_INTERFACE.md §8ter).

Part of the main window's screen stack, alongside Project/Capture (a full
takeover, not a floating utility window like `StatisticsScreen`/the
shortcuts help) — a deliberate, separate stop for an operator reviewing
flagged images at the end of a session — usable outside of an active
capture (loads a session the same way `StatisticsScreen` does).

Grid of thumbnails (reusing the already-exported `JPEG_POSITIVE` files, no
new decode) with multi-select, driving two independent groups of tools:

- **Content frame** (legacy engine only, see below): mouse-drag crop
  editing on the JPEG_MASTER preview (`PreviewArea`, reused as-is — the
  content frame is always axis-aligned, so none of its rotation handling
  applies here). Regenerates `jpeg_positive` only, through
  `CaptureSession.apply_manual_positive_override` — never the TIFF/JPEG
  master, whose geometry (the support frame) this never touches.
- **Tonal calibration** (`exports.jpeg_positive.engine`-dependent): the
  legacy engine's `PositiveSettingsPanel` (exposure/contrast/shadows/
  highlights) or the new engine's `PrintCalibrationPanel` (film base/Dmin,
  scan exposure, paper model — 13_INVERSION_NEGATIFS.md §9), never both at
  once. "Apply to selection" propagates the current image's tonal
  settings to every selected image (`CaptureSession.propagate_print_overrides`
  for the new engine; not offered for the legacy engine, which has no
  propagation primitive of its own).

**Crop editing is legacy-engine only**: `imaging.print_engine` doesn't yet
accept a content-frame override (only the tonal groups do, DECISIONS.md
I-181) — showing draggable handles that silently do nothing would be
worse than not showing them. A print_engine image's auto-detected content
frame is still drawn (informational), just not editable, here.

The preview reflects whichever engine is active. Legacy: `imaging.positive
.render_positive` on the already-decoded, already-cropped JPEG_MASTER —
effectively instant, safe to re-render on every settings change including
mid-drag. print_engine: a *real* `imaging.print_engine.render_print` call
— its own dedicated RAW decode plus the full density-domain render,
measured ~16.7s on a real image (DECISIONS.md I-182) — never re-rendered
live while dragging a slider (`PrintCalibrationPanel.live_changed` is
intentionally not wired to a re-render here), only once a group's value is
committed, with a wait-cursor and status text making the wait legible
rather than silently freezing the window.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QKeyEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from scanassistant.core.positive_review import (
    list_positives_by_category,
    reconstruct_content_frame_fraction,
)
from scanassistant.core.queue import ExportContext
from scanassistant.core.recovery import rebuild_export_context
from scanassistant.core.session import CaptureSession
from scanassistant.gui.widgets.histogram_widget import HistogramWidget
from scanassistant.gui.widgets.positive_settings_panel import PositiveSettingsPanel
from scanassistant.gui.widgets.preview_area import PreviewArea
from scanassistant.gui.widgets.print_calibration_panel import AutoValues, PrintCalibrationPanel
from scanassistant.i18n import t
from scanassistant.imaging import print_engine
from scanassistant.imaging.framing import RELIABLE, ConfidenceComponents, FrameResult
from scanassistant.imaging.geometry import FrameGeometry
from scanassistant.imaging.positive import ManualSettings, render_positive
from scanassistant.imaging.raw import RawDecoder, RawpyDecoder
from scanassistant.project.campaign import JpegPositiveExportConfig, ManualPositiveSettings
from scanassistant.project.layout import CampaignPaths
from scanassistant.project.positive_overrides import (
    PositiveOverride,
    load_positive_overrides,
    set_positive_print_overrides,
)

_DEFAULT_INSET_FRACTION = 0.05  # per side — a head start, not a guess: an
# operator reviewing a *deferred* image (nothing confidently auto-detected)
# still very rarely wants literally the whole support frame kept as-is.
_ZERO_COMPONENTS = ConfidenceComponents(0.0, 0.0, 0.0, 0.0, 0.0)

# Matches `gui.screens.capture`'s own histogram overlay placement/sizing.
_HISTOGRAM_WIDTH_FRACTION = 0.09
_HISTOGRAM_ASPECT = 2.2  # width / height

# Matches `gui.screens.capture._FAST_PREVIEW_MAX_DIM`: the tone-curve math
# (`render_positive`, legacy engine only — see module docstring) runs on
# every mouse-move while a settings slider is being dragged, expensive
# enough at full master resolution to stutter.
_FAST_PREVIEW_MAX_DIM = 480
# `render_positive` on a full-resolution JPEG_MASTER (`long_edge_px` 0 by
# default, i.e. full RAW size) measured ~2.5s on a 24 MP array — a multi-
# second freeze on every image navigation and every slider release, for a
# screen whose whole point is judging a crop/exposure by eye, not exporting.
# 2000px (~0.2s) is indistinguishable from full resolution at that purpose.
_DISPLAY_PREVIEW_MAX_DIM = 2000

_THUMBNAIL_SIZE = 128
_GRID_CELL = QSize(_THUMBNAIL_SIZE + 20, _THUMBNAIL_SIZE + 36)


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


@dataclass(frozen=True)
class _UndoCommand:
    """One confirmed change or propagation, undoable/redoable as a unit —
    granularity mandated by 06_INTERFACE.md §8ter ("per confirmed setting
    or per propagation to a selection", never per in-progress drag). Each
    snapshot is the *whole* prior/new `PositiveOverride` for that name, so
    undo/redo never has to guess which half (crop vs. tonal) changed."""

    description: str
    names: tuple[str, ...]
    before: dict[str, PositiveOverride | None]
    after: dict[str, PositiveOverride | None]


def _snapshot(paths: CampaignPaths, fs, names: list[str]) -> dict[str, PositiveOverride | None]:
    overrides = load_positive_overrides(paths, fs)
    return {name: overrides.get(name) for name in names}


class PositiveReviewScreen(QWidget):
    closed = Signal()  # Escape — back to the Project screen (`MainWindow`)

    def __init__(self, parent: QWidget | None = None, *, decoder: RawDecoder | None = None) -> None:
        super().__init__(parent)

        self._session: CaptureSession | None = None
        self._names: list[str] = []
        self._master_pixels: np.ndarray | None = None
        self._current_name: str | None = None
        self._current_frame: FrameResult | None = None
        self._preview_scale: float = 1.0
        self._exposure_config: JpegPositiveExportConfig | None = None
        self._pending_frames: dict[str, FrameResult] = {}
        self._pending_exposures: dict[str, ManualPositiveSettings] = {}
        self._auto_values: AutoValues | None = None
        self._using_print_engine = False
        # `RawpyDecoder` by default (production); a test double can be
        # injected here the same way `gui.screens.capture.CaptureScreen`
        # already allows — print_engine's own RAW decode (module docstring)
        # needs a real RAW to run against otherwise.
        self._decoder: RawDecoder = decoder or RawpyDecoder()
        self._undo_stack: list[_UndoCommand] = []
        self._redo_stack: list[_UndoCommand] = []

        self.category_deferred_checkbox = QCheckBox(t("positive_review.category_deferred"))
        self.category_deferred_checkbox.setChecked(True)
        self.category_deferred_checkbox.toggled.connect(self.refresh_list)

        self.category_applied_checkbox = QCheckBox(t("positive_review.category_applied"))
        self.category_applied_checkbox.setChecked(False)
        self.category_applied_checkbox.toggled.connect(self.refresh_list)

        self.category_manual_checkbox = QCheckBox(t("positive_review.category_manual"))
        self.category_manual_checkbox.setChecked(False)
        self.category_manual_checkbox.toggled.connect(self.refresh_list)

        # Grid of thumbnails (06_INTERFACE.md §8ter), reusing the already-
        # exported JPEG_POSITIVE — no new decode. Multi-select via Qt's own
        # Ctrl+click/Shift+click; Ctrl+A ("select all of the current
        # filter") is wired explicitly in `keyPressEvent`, since Qt's
        # built-in Ctrl+A only fires when the view itself has focus.
        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setIconSize(QSize(_THUMBNAIL_SIZE, _THUMBNAIL_SIZE))
        self.list_widget.setGridSize(_GRID_CELL)
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setMovement(QListWidget.Movement.Static)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_widget.setWordWrap(True)
        self.list_widget.currentItemChanged.connect(self._on_current_item_changed)

        self.preview_area = PreviewArea()
        self.preview_area.frame_dragged.connect(self._on_frame_dragged)

        self.histogram_widget = HistogramWidget(parent=self)

        self.settings_panel = PositiveSettingsPanel()
        self.settings_panel.live_changed.connect(lambda: self._refresh_preview(fast=True))
        self.settings_panel.settled_changed.connect(self._refresh_preview)

        self.print_panel = PrintCalibrationPanel()
        self.print_panel.settled_changed.connect(self._refresh_preview)

        self.include_dmin_checkbox = QCheckBox(t("positive_review.include_dmin"))
        self.apply_to_selection_button = QPushButton(t("positive_review.apply_to_selection"))
        self.apply_to_selection_button.clicked.connect(self.apply_to_selection)

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

        propagation_row = QHBoxLayout()
        propagation_row.addWidget(self.include_dmin_checkbox)
        propagation_row.addWidget(self.apply_to_selection_button)

        settings_column = QVBoxLayout()
        settings_column.addWidget(self.settings_panel)
        settings_column.addWidget(self.print_panel)
        settings_column.addLayout(propagation_row)
        settings_column.addWidget(self.confirm_button)
        settings_container = QWidget()
        settings_container.setLayout(settings_column)
        settings_container.setMaximumWidth(320)

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

    # --- loading / navigation ------------------------------------------------

    def load(self, session: CaptureSession) -> None:
        """Binds to a campaign and refreshes the list (safe to call again
        on every open — same convention as `StatisticsScreen.load`)."""
        self._session = session
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._using_print_engine = session.campaign.exports.jpeg_positive.engine == "print_engine"
        self.settings_panel.setVisible(not self._using_print_engine)
        self.print_panel.setVisible(self._using_print_engine)
        self.include_dmin_checkbox.setVisible(self._using_print_engine)
        self.apply_to_selection_button.setVisible(self._using_print_engine)
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
        for name in self._names:
            item = QListWidgetItem(name)
            item.setIcon(self._thumbnail_icon(name))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)
        target_row = self._names.index(select_name) if select_name in self._names else 0
        self.list_widget.setCurrentRow(target_row if self._names else -1)
        if not self._names:
            self._load_index(-1)

    def _thumbnail_icon(self, name: str) -> QIcon:
        """The already-exported `JPEG_POSITIVE` file, downscaled — no new
        decode, no RAW involved. A blank icon (not a crash) if it's
        missing (never exported yet, or a stale journal entry)."""
        session = self._session
        assert session is not None
        suffix = session.campaign.exports.jpeg_positive.suffix
        path = session.paths.jpeg_positive_dir / f"{name}{suffix}.jpg"
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return QIcon()
        scaled = pixmap.scaled(
            _THUMBNAIL_SIZE,
            _THUMBNAIL_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        return QIcon(scaled)

    def _on_current_item_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        self._load_index(self.list_widget.row(current) if current is not None else -1)

    def selected_names(self) -> list[str]:
        return [item.text() for item in self.list_widget.selectedItems()]

    def _load_index(self, index: int) -> None:
        self._save_pending_edits()
        session = self._session
        if session is None or index < 0 or index >= len(self._names):
            self._master_pixels = None
            self._current_name = None
            self._current_frame = None
            self._auto_values = None
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

        if self._using_print_engine:
            self._load_print_engine(name)
        else:
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

    def _load_print_engine(self, name: str) -> None:
        """Runs a real `print_engine.render_print` (own RAW decode, own
        density-domain render — see module docstring, ~16.7s measured) to
        get this image's automatic estimate *and* the first preview.
        Crop editing is not offered for this engine (module docstring) —
        the frame overlay shown is informational only."""
        session = self._session
        assert session is not None
        context = self._rebuild_context(name)
        if context is None:
            self.preview_area.show_message(t("positive_review.master_unavailable", name=name))
            return
        override = load_positive_overrides(session.paths, session.fs).get(name)
        self._render_and_show_print_engine(name, context, override)

    def _rebuild_context(self, name: str) -> ExportContext | None:
        session = self._session
        assert session is not None
        return rebuild_export_context(
            name, session.paths, session.fs, session.campaign.capture.extensions
        )

    def _render_and_show_print_engine(
        self, name: str, context, override: PositiveOverride | None
    ) -> None:
        session = self._session
        assert session is not None
        positive_cfg = session.campaign.exports.jpeg_positive
        framing = session.campaign.framing
        frame = FrameGeometry(
            x=context.x,
            y=context.y,
            width=context.width,
            height=context.height,
            angle_deg=context.angle_deg,
        )
        overrides = print_engine.ManualPrintOverrides(
            dmin=context.manual_print_dmin,
            exposure_shift=context.manual_print_exposure_shift,
            contrast=context.manual_print_contrast,
            paper_black=context.manual_print_paper_black,
            paper_soft_clip=context.manual_print_paper_soft_clip,
        )
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.status_label.setText(t("positive_review.reviewing", index=0, total=0, name=name))
        try:
            result = print_engine.render_print(
                self._decoder,
                context.raw_path,
                frame,
                rotation_deg=context.rotation_deg,
                size_mode=framing.size_mode,
                final_dimensions_px=(
                    framing.final_dimensions_px[0],
                    framing.final_dimensions_px[1],
                ),
                user_wb=session.campaign.imaging.white_balance,
                overrides=overrides,
                horizontal_flip=positive_cfg.horizontal_flip,
            )
        finally:
            QApplication.restoreOverrideCursor()
        self._auto_values = AutoValues(
            dmin=result.dmin,
            exposure_shift=result.exposure_shift,
            contrast=result.contrast,
            paper_black=print_engine.DEFAULT_PAPER_BLACK,
            paper_soft_clip=print_engine.DEFAULT_PAPER_SOFT_CLIP,
        )
        self.print_panel.load(self._auto_values, override)
        positive8 = (result.pixels // 257).astype(np.uint8)
        rgb = np.stack([positive8, positive8, positive8], axis=-1)
        self.preview_area.show_image(rgb)
        self.preview_area.set_frame_overlay(None)
        self.histogram_widget.set_pixels(rgb)
        self.status_label.setText(
            t(
                "positive_review.reviewing",
                index=self._names.index(name) + 1 if name in self._names else 0,
                total=len(self._names),
                name=name,
            )
        )

    def _save_pending_edits(self) -> None:
        """Remembers the in-progress crop/exposure for the image being
        navigated away from, so it's restored (instead of reset to the
        default inset / campaign settings) if the operator comes back to it
        before confirming. Legacy engine only — print_engine's tonal state
        is always read fresh from the persisted override + auto render."""
        name = self._current_name
        if name is None or self._using_print_engine:
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
        how small the on-screen preview was downscaled to. No-op for
        print_engine (module docstring: crop isn't wired into that engine
        yet)."""
        if self._using_print_engine:
            return
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
        """Re-renders the positive preview — legacy engine only (see module
        docstring; print_engine's preview is driven by `_load_print_engine`/
        `_render_and_show_print_engine` instead, on navigation and on a
        committed settings change, never on this fast/live path)."""
        if self._using_print_engine:
            return
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

    # --- confirm / apply to selection / undo ----------------------------------

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
        if session is None or self._current_name is None or row < 0:
            return
        name = self._names[row]
        next_name = self._names[row + 1] if row + 1 < len(self._names) else None
        before = _snapshot(session.paths, session.fs, [name])

        if self._using_print_engine:
            overrides = self.print_panel.current_overrides()
            set_positive_print_overrides(
                session.paths,
                session.fs,
                name,
                dmin=overrides.dmin,
                exposure_shift=overrides.exposure_shift,
                contrast=overrides.contrast,
                paper_black=overrides.paper_black,
                paper_soft_clip=overrides.paper_soft_clip,
            )
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self.status_label.setText(t("positive_review.reviewing", index=0, total=0, name=name))
            try:
                session.regenerate_positive(name)
            finally:
                QApplication.restoreOverrideCursor()
        else:
            if self._master_pixels is None or self._current_frame is None:
                return
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
            session.apply_manual_positive_override(
                name, content_frame=content_frame, settings=settings
            )
            self._pending_frames.pop(name, None)
            self._pending_exposures.pop(name, None)

        after = _snapshot(session.paths, session.fs, [name])
        self._push_undo(_UndoCommand("confirm", (name,), before, after))
        item = self.list_widget.item(row)
        if item is not None:
            item.setIcon(self._thumbnail_icon(name))
        self._current_name = None
        self.refresh_list(select_name=next_name)

    def apply_to_selection(self) -> None:
        """ "Apply to selection" (06_INTERFACE.md §8ter): copies the current
        image's print_engine overrides to every other selected image.
        Confirms the current image first (so what's propagated is exactly
        what's on screen), then explicit confirmation ("N images
        affected") before touching anything else."""
        session = self._session
        if session is None or self._current_name is None or not self._using_print_engine:
            return
        source_name = self._current_name
        targets = [n for n in self.selected_names() if n != source_name]
        if not targets:
            QMessageBox.information(
                self,
                t("positive_review.confirm_propagation_title"),
                t("positive_review.propagation_empty"),
            )
            return
        reply = QMessageBox.question(
            self,
            t("positive_review.confirm_propagation_title"),
            t("positive_review.confirm_propagation_body", count=len(targets)),
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Confirm the source first: propagation reads its *persisted*
        # override, not whatever's still only in the panel widgets.
        self.confirm_current()

        before = _snapshot(session.paths, session.fs, targets)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.status_label.setText(t("positive_review.confirm_propagation_body", count=len(targets)))
        try:
            session.propagate_print_overrides(
                source_name, targets, include_dmin=self.include_dmin_checkbox.isChecked()
            )
        finally:
            QApplication.restoreOverrideCursor()
        after = _snapshot(session.paths, session.fs, targets)
        self._push_undo(_UndoCommand("propagate", tuple(targets), before, after))
        self.status_label.setText(t("positive_review.propagation_done", count=len(targets)))
        self.refresh_list(select_name=source_name)

    def _push_undo(self, command: _UndoCommand) -> None:
        self._undo_stack.append(command)
        self._redo_stack.clear()

    def undo(self) -> None:
        if not self._undo_stack:
            return
        command = self._undo_stack.pop()
        self._restore(command.names, command.before)
        self._redo_stack.append(command)
        self.refresh_list(select_name=self._current_name)

    def redo(self) -> None:
        if not self._redo_stack:
            return
        command = self._redo_stack.pop()
        self._restore(command.names, command.after)
        self._undo_stack.append(command)
        self.refresh_list(select_name=self._current_name)

    def _restore(
        self, names: tuple[str, ...], snapshots: dict[str, PositiveOverride | None]
    ) -> None:
        session = self._session
        if session is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for name in names:
                session.restore_positive_override(name, snapshots.get(name))
        finally:
            QApplication.restoreOverrideCursor()

    # --- misc ------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.closed.emit()
            event.accept()
            return
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier and event.key() == Qt.Key.Key_A:
            self.list_widget.selectAll()
            event.accept()
            return
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier and event.key() == Qt.Key.Key_Z:
            self.undo()
            event.accept()
            return
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier and event.key() == Qt.Key.Key_Y:
            self.redo()
            event.accept()
            return
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier and event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ):
            self.apply_to_selection()
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
        if event.key() == Qt.Key.Key_PageDown:
            self._move(6)
            event.accept()
            return
        if event.key() == Qt.Key.Key_PageUp:
            self._move(-6)
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
