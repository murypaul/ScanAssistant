"""Positive calibration screen — post-capture screen for reviewing and
adjusting positives (13_INVERSION_NEGATIFS.md §9, 06_INTERFACE.md §8ter).

Part of the main window's screen stack, alongside Project/Capture (a full
takeover, not a floating utility window like `StatisticsScreen`/the
shortcuts help) — a deliberate, separate stop for an operator reviewing
flagged images at the end of a session — usable outside of an active
capture (loads a session the same way `StatisticsScreen` does).

Grid of thumbnails (reusing the already-exported `JPEG_POSITIVE` files, no
new decode) with multi-select, driving two independent groups of tools:

- **Content frame**: mouse-drag crop editing on a preview (`PreviewArea`,
  reused as-is — the content frame is always axis-aligned, so none of its
  rotation handling applies here), for *either* engine now. Legacy: drags
  the JPEG_MASTER preview, regenerates `jpeg_positive` through
  `CaptureSession.apply_manual_positive_override`. print_engine: drags the
  full (uncropped) print_engine preview itself (`imaging.print_engine.
  render_print_from_linear(crop_to_content=False)`,
  `ManualPrintOverrides.content_frame` — DECISIONS.md I-181's own
  follow-up), persisted as `PositiveOverride.print_content_frame`,
  entirely separate from the legacy engine's `content_frame` field.
  Neither ever touches the TIFF/JPEG master, whose geometry (the support
  frame) this screen never changes.
- **Tonal calibration** (`exports.jpeg_positive.engine`-dependent): the
  legacy engine's `PositiveSettingsPanel` (exposure/contrast/shadows/
  highlights) or the new engine's `PrintCalibrationPanel` (film base/Dmin,
  scan exposure, paper model — 13_INVERSION_NEGATIFS.md §9), never both at
  once. "Apply to selection" propagates the current image's tonal
  settings to every selected image (`CaptureSession.propagate_print_overrides`
  for the new engine; not offered for the legacy engine, which has no
  propagation primitive of its own) — never the crop, which is specific to
  each negative's own physical framing and never propagated across images.

The preview reflects whichever engine is active. Legacy: `imaging.positive
.render_positive` on the already-decoded, already-cropped JPEG_MASTER —
effectively instant, safe to re-render on every settings change including
mid-drag. print_engine: `imaging.print_engine.render_print`'s own RAW
decode + density-domain render measured ~16.7s on a real image
(DECISIONS.md I-182) is cached per image for the rest of this screen
session (`_linear_cache`) — a repeat visit or a committed settings/crop
change re-runs only the cheap density-math half
(`render_print_from_linear`), never a second decode. Every operation that
can still take real time (the first decode of an image, a committed
change, Confirm, Apply to selection, undo/redo) runs off the GUI thread
(`_CallWorker`/`_run_async`) with the screen's controls locked and a
status message, rather than blocking the Qt event loop — a `~16.7s` block
with no `processEvents()` is what the OS sees as a hung, unresponsive
application, the most likely cause of the "numerous crashes" this was
originally reported to cause.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image
from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QIcon, QKeyEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
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
from scanassistant.imaging.geometry import FrameGeometry, apply_geometry
from scanassistant.imaging.positive import ManualSettings, render_positive
from scanassistant.imaging.raw import RawDecoder, RawpyDecoder
from scanassistant.project.campaign import JpegPositiveExportConfig, ManualPositiveSettings
from scanassistant.project.layout import CampaignPaths
from scanassistant.project.positive_overrides import (
    PositiveOverride,
    load_positive_overrides,
    set_positive_print_overrides,
)

# print_engine's own RAW decode + geometry crop is the ~16.7s (DECISIONS.md
# I-182) dominant cost of a render — `render_print_from_linear` alone (the
# tonal math re-run on a settings change) is a small fraction of that.
# Caching the decoded-and-cropped linear array per image (bounded, since
# each is a full-resolution float32 RGB array) turns "revisit an
# already-loaded image" and "commit a slider change" into a sub-second
# re-render instead of a second full decode.
_LINEAR_CACHE_MAX = 3

# Same value `gui.screens.capture` uses for its own frame/rotation commit
# debounce (`_FRAME_COMMIT_DELAY_MS`/`_ROTATION_COMMIT_DELAY_MS`) — a
# deliberately shared convention, not independently tuned.
_CONFIRM_DEBOUNCE_MS = 2500


class _CallWorker(QThread):
    """Runs a zero-arg callable on a background `QThread` and reports back
    via a signal — used for every print_engine operation this screen used
    to run synchronously on the GUI thread (decode, regenerate, propagate,
    undo/restore). DECISIONS.md I-183(b) accepted that blocking as a known
    limitation; it's since been reported as the likely cause of "numerous
    crashes" — a main thread blocked for ~16.7s with no `processEvents()`
    reads to the OS/desktop environment as a hung application, which is
    usually what actually triggers an unprompted force-kill, not a real
    crash in the process itself."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, func: Callable[[], Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._func = func

    def run(self) -> None:
        try:
            result = self._func()
        except Exception as exc:  # noqa: BLE001 - surfaced via `failed`, never swallowed
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)

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
        # print_engine's own crop state (module docstring: crop editing is
        # offered for both engines now) — parallel to `_current_frame`/
        # `_pending_frames` above, kept separate rather than reused since
        # the two engines' preview coordinate spaces differ (print_engine
        # is always shown at native resolution, never downscaled).
        self._current_print_frame: FrameResult | None = None
        self._current_print_frame_shape: tuple[int, int] | None = None
        self._pending_print_frames: dict[str, FrameResult] = {}
        # `RawpyDecoder` by default (production); a test double can be
        # injected here the same way `gui.screens.capture.CaptureScreen`
        # already allows — print_engine's own RAW decode (module docstring)
        # needs a real RAW to run against otherwise.
        self._decoder: RawDecoder = decoder or RawpyDecoder()
        self._undo_stack: list[_UndoCommand] = []
        self._redo_stack: list[_UndoCommand] = []
        # name -> (linear float32 HxWx3 [0,1], geometry.frame_in_output),
        # most-recently-used last. See `_LINEAR_CACHE_MAX`'s docstring.
        self._linear_cache: OrderedDict[str, tuple[np.ndarray, FrameGeometry]] = OrderedDict()
        self._prefetching: set[str] = set()
        self._workers: set[_CallWorker] = set()
        self._foreground_workers: set[_CallWorker] = set()
        self._busy = False
        # Debounced auto-confirm (06_INTERFACE.md §8ter), same pattern as
        # `gui.screens.capture`'s own frame/rotation commit timers: a real
        # edit (crop drag, tonal setting) starts/restarts this, and it fires
        # `_auto_confirm_silently` after `_CONFIRM_DEBOUNCE_MS` of no further
        # edit — never requires an explicit Confirm click. Every navigation
        # away from the current image also flushes it immediately first
        # (`_save_pending_edits`), the same "never lose a pending edit even
        # if the timer hasn't fired yet" guarantee `capture.py` already
        # relies on for its own commit timers.
        self._confirm_pending = False
        self._confirm_debounce_timer = QTimer(self)
        self._confirm_debounce_timer.setSingleShot(True)
        self._confirm_debounce_timer.timeout.connect(self._commit_pending_confirm)

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
        self.settings_panel.settled_changed.connect(self._mark_edited)

        self.print_panel = PrintCalibrationPanel()
        self.print_panel.settled_changed.connect(self._on_print_settings_committed)
        self.print_panel.settled_changed.connect(self._mark_edited)

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

        # Stacked, not side-by-side: at the settings column's width, the
        # checkbox + button together truncated the button's own label (the
        # user saw the trailing "(Ctrl+Enter)" clipped down to "(Ctrl+E" —
        # confirmed by the mis-copied shortcut in their report). Each on its
        # own row always has the full column width to render in.
        propagation_column = QVBoxLayout()
        propagation_column.addWidget(self.include_dmin_checkbox)
        propagation_column.addWidget(self.apply_to_selection_button)

        settings_column = QVBoxLayout()
        settings_column.addWidget(self.settings_panel)
        settings_column.addWidget(self.print_panel)
        settings_column.addLayout(propagation_column)
        settings_column.addWidget(self.confirm_button)
        settings_container = QWidget()
        settings_container.setLayout(settings_column)
        settings_container.setMinimumWidth(280)
        settings_container.setMaximumWidth(360)

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

    # --- background work (see `_CallWorker`) ----------------------------------

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self._busy = busy
        if status is not None:
            self.status_label.setText(status)
        for widget in (
            self.list_widget,
            self.print_panel,
            self.settings_panel,
            self.confirm_button,
            self.apply_to_selection_button,
            self.include_dmin_checkbox,
            self.category_deferred_checkbox,
            self.category_applied_checkbox,
            self.category_manual_checkbox,
        ):
            widget.setEnabled(not busy)
        if busy:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()

    def _run_async(
        self,
        func: Callable[[], Any],
        on_success: Callable[[Any], None],
        *,
        busy_text: str = "",
        on_failure: Callable[[str], None] | None = None,
        silent: bool = False,
    ) -> None:
        """Runs `func` off the GUI thread (`_CallWorker`). Every
        *externally* (user-)triggered entry point is disabled while busy
        (`_set_busy`), but a completion handler can still legitimately
        start a second worker before returning — e.g. `confirm_current`'s
        `_finish_confirm` calls `refresh_list`, which auto-loads the next
        image (a fresh `_run_async` decode) *before* `apply_to_selection`'s
        own chained propagation step gets to start its own. `self._workers`
        tracks all of them (a set, not a single slot) so neither loses its
        reference — a lost reference here isn't just a bookkeeping error,
        it orphans a real background `QThread` that can still be running
        (touching numpy/opencv state) when the screen is later torn down,
        which crashes the process outright rather than raising a catchable
        exception.

        `silent=True` (background prefetch, debounced auto-confirm on
        navigating away — 06_INTERFACE.md §8ter) never touches `_set_busy`:
        the operator keeps browsing/editing while it runs, on top of
        whatever a foreground (non-silent) operation is also doing — the
        two are independent, `self._foreground_workers` tracks only the
        latter so the screen unlocks exactly when no *foreground* work is
        left, regardless of what's still finishing silently underneath."""
        worker = _CallWorker(func, parent=self)

        def _on_succeeded(result: object) -> None:
            self._workers.discard(worker)
            if not silent:
                self._foreground_workers.discard(worker)
                self._set_busy(bool(self._foreground_workers))
            on_success(result)

        def _on_failed(message: str) -> None:
            self._workers.discard(worker)
            if not silent:
                self._foreground_workers.discard(worker)
                self._set_busy(bool(self._foreground_workers))
            if on_failure is not None:
                on_failure(message)
            elif not silent:
                QMessageBox.warning(self, t("positive_review.title"), message)

        worker.succeeded.connect(_on_succeeded)
        worker.failed.connect(_on_failed)
        self._workers.add(worker)
        if not silent:
            self._foreground_workers.add(worker)
            self._set_busy(True, busy_text)
        worker.start()

    def _wait_for_workers(self) -> None:
        """Blocks until every in-flight worker has finished — used wherever
        this screen is about to stop existing in its current form (torn
        down, or rebound to a different session/campaign via `load`) so no
        `QThread` is ever destroyed while still running (Qt aborts the
        process when that happens, confirmed while testing this screen)."""
        for worker in list(self._workers):
            worker.wait()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_pending_edits()  # flush a pending debounced auto-confirm first
        self._wait_for_workers()
        super().closeEvent(event)

    # --- loading / navigation ------------------------------------------------

    def load(self, session: CaptureSession) -> None:
        """Binds to a campaign and refreshes the list (safe to call again
        on every open — same convention as `StatisticsScreen.load`).

        First flushes a pending debounced auto-confirm for the *previous*
        session's current image (same reasoning as `_save_pending_edits`),
        then waits out any worker still running for it: `_linear_cache` is
        about to be cleared and `self._session` reassigned, and a stale
        worker's completion handler closing over the old session/name would
        otherwise run against that discarded state."""
        self._save_pending_edits()
        self._wait_for_workers()
        self._session = session
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._linear_cache.clear()
        self._prefetching.clear()
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
            self._current_print_frame = None
            self._current_print_frame_shape = None
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
            manual = self._pending_exposures.get(name)
            if manual is None:
                confirmed = load_positive_overrides(session.paths, session.fs).get(name)
                if confirmed is not None and confirmed.settings is not None:
                    exposure_ev, contrast, shadows, highlights = confirmed.settings
                    manual = ManualPositiveSettings(
                        exposure_ev=exposure_ev,
                        contrast=contrast,
                        shadows=shadows,
                        highlights=highlights,
                    )
            manual = manual or session.campaign.exports.jpeg_positive.manual_settings
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
        """Gets this image's positive on screen: instantly, from
        `_linear_cache`, if it's been decoded before this screen session;
        otherwise a real RAW decode + geometry crop, off the GUI thread
        (`_run_async`) so the window stays responsive for the ~16.7s that
        takes (DECISIONS.md I-182). Crop editing is not offered for this
        engine (module docstring) — the frame overlay shown is
        informational only."""
        cached = self._linear_cache.get(name)
        if cached is not None:
            self._linear_cache.move_to_end(name)
            linear, frame_in_output = cached
            self._render_from_cached_linear(name, linear, frame_in_output)
            self._maybe_prefetch_next(name)
            return

        decode = self._build_decode_task(name)
        if decode is None:
            self.preview_area.show_message(t("positive_review.master_unavailable", name=name))
            return

        def _on_decoded(result: tuple[np.ndarray, FrameGeometry]) -> None:
            linear, frame_in_output = result
            self._cache_linear(name, linear, frame_in_output)
            self._render_from_cached_linear(name, linear, frame_in_output)
            self._maybe_prefetch_next(name)

        def _on_failed(message: str) -> None:
            self.preview_area.show_message(t("positive_review.master_unavailable", name=name))
            self.status_label.setText(message)

        self._run_async(
            decode, _on_decoded, busy_text=t("positive_review.loading", name=name), on_failure=_on_failed
        )

    def _rebuild_context(self, name: str) -> ExportContext | None:
        session = self._session
        assert session is not None
        return rebuild_export_context(
            name, session.paths, session.fs, session.campaign.capture.extensions
        )

    def _build_decode_task(
        self, name: str
    ) -> Callable[[], tuple[np.ndarray, FrameGeometry]] | None:
        """The expensive half of a print_engine render (RAW decode +
        geometry crop, DECISIONS.md I-182) as a zero-arg callable ready for
        `_run_async` — `None` if `name`'s support frame can't be rebuilt
        from the journal. Shared by the foreground load (`_load_print_engine`)
        and the silent next-image prefetch (`_maybe_prefetch_next`)."""
        session = self._session
        assert session is not None
        context = self._rebuild_context(name)
        if context is None:
            return None
        framing = session.campaign.framing
        frame = FrameGeometry(
            x=context.x,
            y=context.y,
            width=context.width,
            height=context.height,
            angle_deg=context.angle_deg,
        )
        decoder = self._decoder
        raw_path = context.raw_path
        rotation_deg = context.rotation_deg
        size_mode = framing.size_mode
        final_dimensions_px = (framing.final_dimensions_px[0], framing.final_dimensions_px[1])
        user_wb = session.campaign.imaging.white_balance

        def _decode() -> tuple[np.ndarray, FrameGeometry]:
            development = decoder.develop(raw_path, user_wb=user_wb, linear=True)
            geometry = apply_geometry(
                development.pixels,
                frame,
                rotation_deg=rotation_deg,
                size_mode=size_mode,
                final_dimensions_px=final_dimensions_px,
            )
            linear = geometry.pixels.astype(np.float32) / 65535.0
            return linear, geometry.frame_in_output

        return _decode

    def _cache_linear(self, name: str, linear: np.ndarray, frame_in_output: FrameGeometry) -> None:
        self._linear_cache[name] = (linear, frame_in_output)
        self._linear_cache.move_to_end(name)
        while len(self._linear_cache) > _LINEAR_CACHE_MAX:
            self._linear_cache.popitem(last=False)

    def _maybe_prefetch_next(self, name: str) -> None:
        """Silently decodes the *next* image in the current filtered list
        while the operator is still looking at `name` — by the time they
        move on (an explicit Confirm, or the debounced auto-confirm,
        06_INTERFACE.md §8ter), the following image is often already
        decoded, instead of every single navigation paying the full RAW
        decode cost even for a plain, unhurried "look at the next flagged
        image" workflow. Never sets `_busy` (`_run_async(silent=True)`):
        this is a pure head-start, not something the operator should ever
        have to wait on directly."""
        if not self._using_print_engine or name not in self._names:
            return
        index = self._names.index(name)
        if index + 1 >= len(self._names):
            return
        next_name = self._names[index + 1]
        if next_name in self._linear_cache or next_name in self._prefetching:
            return
        decode = self._build_decode_task(next_name)
        if decode is None:
            return

        def _on_decoded(result: tuple[np.ndarray, FrameGeometry]) -> None:
            self._prefetching.discard(next_name)
            linear, frame_in_output = result
            self._cache_linear(next_name, linear, frame_in_output)

        def _on_failed(_message: str) -> None:
            self._prefetching.discard(next_name)

        self._prefetching.add(next_name)
        self._run_async(decode, _on_decoded, on_failure=_on_failed, silent=True)

    def _render_from_cached_linear(
        self, name: str, linear: np.ndarray, frame_in_output: FrameGeometry
    ) -> None:
        """The cheap half of a print_engine render (`render_print_from_linear`
        alone — density math + GrabCut content-frame, no RAW decode):
        driven directly off `_linear_cache`, so a repeat visit or a
        committed settings change never re-decodes.

        Always renders `crop_to_content=False` (module docstring: crop
        editing is now offered for print_engine too, unlike when this was
        first built) — the full support-frame positive with a draggable
        overlay, matching the legacy engine's own crop-editing preview
        exactly, rather than the already-cropped result an operator could
        do nothing with."""
        session = self._session
        assert session is not None
        override = load_positive_overrides(session.paths, session.fs).get(name)
        horizontal_flip = session.campaign.exports.jpeg_positive.horizontal_flip
        full_height, full_width = linear.shape[:2]
        content_frame_override = self._pending_print_content_frame(
            name, override, full_width, full_height, horizontal_flip
        )
        overrides = print_engine.ManualPrintOverrides(
            dmin=override.print_dmin if override else None,
            exposure_shift=override.print_exposure_shift if override else None,
            contrast=override.print_contrast if override else None,
            paper_black=override.print_paper_black if override else None,
            paper_soft_clip=override.print_paper_soft_clip if override else None,
            content_frame=content_frame_override,
        )
        result = print_engine.render_print_from_linear(
            linear.astype(np.float64),
            frame_in_output,
            overrides=overrides,
            horizontal_flip=horizontal_flip,
            crop_to_content=False,
        )
        self._show_print_result(name, result, override)

    def _pending_print_content_frame(
        self,
        name: str,
        override: PositiveOverride | None,
        full_width: int,
        full_height: int,
        horizontal_flip: bool,
    ) -> tuple[float, float, float, float] | None:
        """The crop to render with, in priority order: an in-progress drag
        not yet confirmed (`_pending_print_frames`, this screen session
        only), else the persisted override, else `None` (let the engine
        auto-detect). The pending frame is in the preview's own
        full-resolution, already-flipped display space and needs mirroring
        back to the pre-flip fraction convention every stored/engine-facing
        crop uses; the persisted override is already in that convention."""
        pending = self._pending_print_frames.get(name)
        if pending is not None:
            return _print_frame_to_fraction(pending, full_width, full_height, horizontal_flip)
        if override is not None and override.print_content_frame is not None:
            return override.print_content_frame
        return None

    def _show_print_result(
        self, name: str, result: print_engine.PrintResult, override: PositiveOverride | None
    ) -> None:
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
        self._current_print_frame = _print_frame_from_rect(result.content_frame)
        self._current_print_frame_shape = rgb.shape[:2]
        self.preview_area.set_frame_overlay(self._current_print_frame)
        self.histogram_widget.set_pixels(rgb)
        self._reposition_histogram()
        self.status_label.setText(
            t(
                "positive_review.reviewing",
                index=self._names.index(name) + 1 if name in self._names else 0,
                total=len(self._names),
                name=name,
            )
        )

    def _on_print_settings_committed(self) -> None:
        """`PrintCalibrationPanel.settled_changed`: re-render from the
        panel's own current (not-yet-confirmed) values — the live-preview
        gap the user reported ("pas de visualisation en temps réel"). Only
        possible now that a committed change costs a `render_print_from_
        linear` call on cached data, not a second full RAW decode; still
        routed through `_run_async` since that alone (density math +
        GrabCut on a full-resolution array) is not guaranteed instant.

        Reuses `self._current_print_frame` (not the auto-detector) for the
        crop: a tonal-only commit must not silently move or reset a crop
        the operator already dragged into place."""
        if not self._using_print_engine or self._current_name is None:
            return
        name = self._current_name
        cached = self._linear_cache.get(name)
        if cached is None:
            return  # still decoding for the first time; nothing to re-render yet
        linear, frame_in_output = cached
        session = self._session
        assert session is not None
        horizontal_flip = session.campaign.exports.jpeg_positive.horizontal_flip
        full_height, full_width = linear.shape[:2]
        content_frame_override = None
        if self._current_print_frame is not None:
            content_frame_override = _print_frame_to_fraction(
                self._current_print_frame, full_width, full_height, horizontal_flip
            )
        overrides = replace(self.print_panel.current_overrides(), content_frame=content_frame_override)

        def _render() -> print_engine.PrintResult:
            return print_engine.render_print_from_linear(
                linear.astype(np.float64),
                frame_in_output,
                overrides=overrides,
                horizontal_flip=horizontal_flip,
                crop_to_content=False,
            )

        def _on_rendered(result: print_engine.PrintResult) -> None:
            positive8 = (result.pixels // 257).astype(np.uint8)
            rgb = np.stack([positive8, positive8, positive8], axis=-1)
            self.preview_area.show_image(rgb)
            self._current_print_frame = _print_frame_from_rect(result.content_frame)
            self._current_print_frame_shape = rgb.shape[:2]
            self.preview_area.set_frame_overlay(self._current_print_frame)
            self.histogram_widget.set_pixels(rgb)
            self._reposition_histogram()

        self._run_async(_render, _on_rendered, busy_text=t("positive_review.rendering", name=name))

    def _save_pending_edits(self) -> None:
        """First flushes a debounced auto-confirm if one is pending
        (06_INTERFACE.md §8ter — an edit the operator made but the
        `_CONFIRM_DEBOUNCE_MS` quiet period hasn't elapsed for yet must
        never be silently dropped just because they moved on before it
        fired, same guarantee `gui.screens.capture`'s own commit timers
        give). Only without a pending edit does the older, purely-in-memory
        "remember the in-progress crop/exposure, restore it if the operator
        comes back before confirming" fallback still apply."""
        name = self._current_name
        if name is None:
            return
        if self._confirm_pending:
            self._confirm_debounce_timer.stop()
            self._confirm_pending = False
            self._auto_confirm_silently()
            return
        if self._using_print_engine:
            if self._current_print_frame is not None:
                self._pending_print_frames[name] = self._current_print_frame
            return
        if self._current_frame is not None:
            self._pending_frames[name] = self._current_frame
        if self._exposure_config is not None:
            self._pending_exposures[name] = self._exposure_config.manual_settings

    def _on_frame_dragged(self, frame: FrameResult) -> None:
        """`frame` is in the currently *displayed* preview's own coordinate
        space. Legacy engine: that preview is possibly downscaled
        (`self._preview_scale`), converted back to `self._master_pixels`'
        full-resolution space before being stored. print_engine: the
        preview is always shown at its own native resolution (no manual
        downscale, module docstring on `_DISPLAY_PREVIEW_MAX_DIM`'s legacy-
        only scope) — `frame` is already in the right space, stored as-is."""
        if self._using_print_engine:
            self._current_print_frame = frame
            self._mark_edited()
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
        self._mark_edited()

    def _refresh_preview(self, *, fast: bool = False) -> None:
        """Re-renders the positive preview — legacy engine only (see module
        docstring; print_engine's preview is driven by `_load_print_engine`/
        `_on_print_settings_committed` instead, on navigation and on a
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

    def _mark_edited(self) -> None:
        """A real edit happened (crop drag, a print_engine group toggled or
        a slider committed, a legacy setting committed) — (re)starts the
        debounced auto-confirm (06_INTERFACE.md §8ter), same pattern as
        `gui.screens.capture`'s own frame/rotation commit timers. A no-op
        call from `PrintCalibrationPanel.load()`'s own programmatic setup
        can't reach here: that emits with its groups' signals blocked
        specifically to prevent this (see its own docstring)."""
        self._confirm_pending = True
        self._confirm_debounce_timer.start(_CONFIRM_DEBOUNCE_MS)

    def _commit_pending_confirm(self) -> None:
        self._confirm_debounce_timer.stop()
        if not self._confirm_pending:
            return
        self._confirm_pending = False
        self._auto_confirm_silently()

    def _auto_confirm_silently(self) -> None:
        """The debounced/navigate-away half of the auto-confirm
        (06_INTERFACE.md §8ter): persists whatever the operator just
        changed without an explicit Confirm click. Silent for print_engine
        (`_run_async(silent=True)`) — never blocks moving on to the next
        image, unlike `confirm_current`'s own (deliberately visible)
        regenerate. No list-navigation side effect either way: this fires
        *during* a navigation the operator already initiated themselves (or
        after a quiet pause with none at all), so jumping to some other
        "next" image would fight whatever's already happening."""
        session = self._session
        name = self._current_name
        if session is None or name is None or name not in self._names:
            return
        row = self._names.index(name)
        before = _snapshot(session.paths, session.fs, [name])

        if self._using_print_engine:
            self._persist_print_overrides_for_current(name)

            def _regenerate() -> None:
                session.regenerate_positive(name)
                session.wait_for_pending_exports()

            def _on_regenerated(_result: object) -> None:
                self._record_confirm(name, before, row)

            self._run_async(_regenerate, _on_regenerated, silent=True)
            return

        if not self._persist_legacy_overrides_for_current(name):
            return
        self._record_confirm(name, before, row)

    def _persist_print_overrides_for_current(self, name: str) -> None:
        """Writes `name`'s current tonal + crop state as its print_engine
        override — the actual "confirm" for that engine, shared by the
        explicit Confirm action and the silent debounced auto-confirm."""
        session = self._session
        assert session is not None
        overrides = self.print_panel.current_overrides()
        content_frame = None
        if self._current_print_frame is not None and self._current_print_frame_shape is not None:
            full_height, full_width = self._current_print_frame_shape
            horizontal_flip = session.campaign.exports.jpeg_positive.horizontal_flip
            content_frame = _print_frame_to_fraction(
                self._current_print_frame, full_width, full_height, horizontal_flip
            )
        set_positive_print_overrides(
            session.paths,
            session.fs,
            name,
            dmin=overrides.dmin,
            exposure_shift=overrides.exposure_shift,
            contrast=overrides.contrast,
            paper_black=overrides.paper_black,
            paper_soft_clip=overrides.paper_soft_clip,
            content_frame=content_frame,
        )
        self._pending_print_frames.pop(name, None)

    def _persist_legacy_overrides_for_current(self, name: str) -> bool:
        """Same for the legacy engine — `False` (nothing persisted) if the
        master preview isn't loaded, the same guard `confirm_current` used
        to inline directly."""
        session = self._session
        assert session is not None
        if self._master_pixels is None or self._current_frame is None:
            return False
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
        session.wait_for_pending_exports()
        self._pending_frames.pop(name, None)
        self._pending_exposures.pop(name, None)
        return True

    def _record_confirm(
        self, name: str, before: dict[str, PositiveOverride | None], row: int
    ) -> None:
        """Undo-stack entry + thumbnail refresh only — no list navigation
        (shared by `_finish_confirm`, which adds that, and the silent
        auto-confirm, which deliberately doesn't)."""
        session = self._session
        assert session is not None
        after = _snapshot(session.paths, session.fs, [name])
        self._push_undo(_UndoCommand("confirm", (name,), before, after))
        item = self.list_widget.item(row)
        if item is not None:
            item.setIcon(self._thumbnail_icon(name))

    def confirm_current(self, *, on_done: Callable[[], None] | None = None) -> None:
        """Applies the current crop/exposure and advances to the next
        flagged image (Enter). The confirmed image no longer drops out of
        the list on its own once more than one category checkbox is
        checked — confirming an `applied` image while "Already confirmed
        manually" is also checked leaves it visible, now under `manual` —
        so this always explicitly selects whatever followed it, rather than
        relying on `refresh_list`'s default (row 0), which would otherwise
        re-select the same image forever whenever it's also the very first
        one ever logged in the campaign.

        For print_engine, `session.regenerate_positive` is a real render
        (~16.7s) — routed through `_run_async` (`on_done` lets
        `apply_to_selection` chain its own propagation step after this
        completes, instead of assuming it's already done by the time this
        method returns, as it used to when this was synchronous)."""
        session = self._session
        row = self.list_widget.currentRow()
        if session is None or self._current_name is None or row < 0 or self._busy:
            return
        self._confirm_debounce_timer.stop()
        self._confirm_pending = False
        name = self._names[row]
        next_name = self._names[row + 1] if row + 1 < len(self._names) else None
        before = _snapshot(session.paths, session.fs, [name])

        if self._using_print_engine:
            self._persist_print_overrides_for_current(name)

            def _regenerate() -> None:
                session.regenerate_positive(name)
                session.wait_for_pending_exports()

            def _on_regenerated(_result: object) -> None:
                self._finish_confirm(name, next_name, before, row)
                if on_done is not None:
                    on_done()

            self._run_async(
                _regenerate, _on_regenerated, busy_text=t("positive_review.regenerating", name=name)
            )
            return

        if not self._persist_legacy_overrides_for_current(name):
            return
        self._finish_confirm(name, next_name, before, row)
        if on_done is not None:
            on_done()

    def _finish_confirm(
        self,
        name: str,
        next_name: str | None,
        before: dict[str, PositiveOverride | None],
        row: int,
    ) -> None:
        self._record_confirm(name, before, row)
        self._current_name = None
        self.refresh_list(select_name=next_name)

    def apply_to_selection(self) -> None:
        """ "Apply to selection" (06_INTERFACE.md §8ter): copies the current
        image's print_engine overrides to every other selected image.
        Confirms the current image first (so what's propagated is exactly
        what's on screen), then explicit confirmation ("N images
        affected") before touching anything else. Both steps are
        `_run_async` calls chained via `confirm_current`'s `on_done`."""
        session = self._session
        if session is None or self._current_name is None or not self._using_print_engine or self._busy:
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

        def _start_propagation() -> None:
            before = _snapshot(session.paths, session.fs, targets)

            def _propagate() -> None:
                session.propagate_print_overrides(
                    source_name, targets, include_dmin=self.include_dmin_checkbox.isChecked()
                )
                session.wait_for_pending_exports()

            def _on_propagated(_result: object) -> None:
                after = _snapshot(session.paths, session.fs, targets)
                self._push_undo(_UndoCommand("propagate", tuple(targets), before, after))
                self.status_label.setText(t("positive_review.propagation_done", count=len(targets)))
                self.refresh_list(select_name=source_name)

            self._run_async(
                _propagate,
                _on_propagated,
                busy_text=t("positive_review.confirm_propagation_body", count=len(targets)),
            )

        # Confirm the source first: propagation reads its *persisted*
        # override, not whatever's still only in the panel widgets.
        self.confirm_current(on_done=_start_propagation)

    def _push_undo(self, command: _UndoCommand) -> None:
        self._undo_stack.append(command)
        self._redo_stack.clear()

    def undo(self) -> None:
        if not self._undo_stack or self._busy:
            return
        # An edit still only debouncing (not yet confirmed) firing *after*
        # the undo below would silently override it a couple of seconds
        # later — discard it, the undo itself is the operator's intent now.
        self._confirm_debounce_timer.stop()
        self._confirm_pending = False
        command = self._undo_stack.pop()

        def _on_restored(_result: object) -> None:
            self._redo_stack.append(command)
            self.refresh_list(select_name=self._current_name)

        self._restore_async(command.names, command.before, _on_restored)

    def redo(self) -> None:
        if not self._redo_stack or self._busy:
            return
        self._confirm_debounce_timer.stop()
        self._confirm_pending = False
        command = self._redo_stack.pop()

        def _on_restored(_result: object) -> None:
            self._undo_stack.append(command)
            self.refresh_list(select_name=self._current_name)

        self._restore_async(command.names, command.after, _on_restored)

    def _restore_async(
        self,
        names: tuple[str, ...],
        snapshots: dict[str, PositiveOverride | None],
        on_done: Callable[[object], None],
    ) -> None:
        session = self._session
        if session is None:
            return

        def _restore() -> None:
            for name in names:
                session.restore_positive_override(name, snapshots.get(name))
            session.wait_for_pending_exports()

        self._run_async(_restore, on_done, busy_text=t("positive_review.undo"))

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


def _print_frame_from_rect(rect: tuple[int, int, int, int]) -> FrameResult:
    """Wraps a print_engine content-frame rect (already in the preview's
    own display coordinate space — `PrintResult.content_frame` from a
    `crop_to_content=False` render) as a draggable `FrameResult` overlay,
    same confidence/level convention as `_default_content_frame`/
    `_frame_from_fraction` (always shown as "reliable" — the frame's own
    correctness here is the operator's judgment, not a detector score)."""
    x, y, w, h = rect
    return FrameResult(
        x=x,
        y=y,
        width=w,
        height=h,
        angle_deg=0.0,
        confidence=1.0,
        level=RELIABLE,
        components=_ZERO_COMPONENTS,
    )


def _print_frame_to_fraction(
    frame: FrameResult, full_width: int, full_height: int, horizontal_flip: bool
) -> tuple[float, float, float, float]:
    """Converts a content-frame rect from the print_engine preview's own
    (full-resolution, already-flipped) display space to the pre-flip
    fraction convention `PositiveOverride.print_content_frame`/
    `imaging.print_engine.ManualPrintOverrides.content_frame` both use —
    the inverse of the mirroring `render_print_from_linear(crop_to_content
    =False)` applies to its own returned `content_frame`."""
    x, y, w, h = frame.x, frame.y, frame.width, frame.height
    if horizontal_flip:
        x = full_width - x - w
    return (x / full_width, y / full_height, w / full_width, h / full_height)


def _load_master_jpeg(paths: CampaignPaths, name: str) -> np.ndarray | None:
    path = Path(paths.jpeg_master_dir) / f"{name}.jpg"
    try:
        with Image.open(path) as image:
            return np.array(image.convert("RGB"))
    except OSError:
        return None
