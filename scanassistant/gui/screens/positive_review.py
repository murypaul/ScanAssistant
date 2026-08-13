"""Positive calibration screen — post-capture screen for reviewing and
adjusting positives.

Part of the main window's screen stack, alongside Project/Capture (a full
takeover, not a floating utility window like `StatisticsScreen`/the
shortcuts help) — a deliberate, separate stop for an operator reviewing
flagged images at the end of a session — usable outside of an active
capture (loads a session the same way `StatisticsScreen` does).

Grid of thumbnails (reusing the already-exported `JPEG_POSITIVE` files, no
new decode) with multi-select, driving two independent groups of tools:

- **Content frame**: mouse-drag crop editing on a preview (`PreviewArea`,
  reused as-is — the content frame is always axis-aligned, so none of its
  rotation handling applies here) — drags the full (uncropped) print_engine
  preview itself (`imaging.print_engine.render_print_from_linear(
  crop_to_content=False)`, `ManualPrintOverrides.content_frame`), persisted
  as `PositiveOverride.print_content_frame`. Never touches the TIFF/JPEG
  master, whose geometry (the support frame) this screen never changes.
- **Tonal calibration**: `PrintCalibrationPanel` (film base/Dmin, scan
  exposure, paper model). "Apply to selection" propagates the current
  image's tonal settings to every selected image
  (`CaptureSession.propagate_print_overrides`) — never the crop, which is
  specific to each negative's own physical framing and never propagated
  across images.

`imaging.print_engine.render_print`'s own RAW decode + density-domain
render measured ~16.7s on a real image, cached per image for the rest of
this screen session (`_linear_cache`) — a repeat visit or a committed
settings/crop change re-runs only the cheap density-math half
(`render_print_from_linear`), never a second decode.

A committed change is still not free, though (~1.8-2.8s on the cached
array — density math + GrabCut, I-190): `PrintCalibrationPanel.live_changed`
(every tick while a slider is still being dragged) additionally drives a
*further* downsampled re-render (`_on_print_settings_live`,
`_LIVE_PREVIEW_MAX_DIM`), upscaled back to display resolution before
showing — a live preview an operator can actually judge tone by while
dragging, without paying that per-tick cost at full resolution (which
would fall behind the drag itself, backing up render after render).

Every operation that can still take real time runs off the GUI thread
(`_CallWorker`/`_run_async`), so a `~16.7s` block with no `processEvents()`
never reads to the OS as a hung, unresponsive application (the most likely
cause of "numerous crashes" this was originally reported to cause). Only a
"commit" (Confirm, Apply to selection, undo/redo, a committed settings
re-render) locks the screen's controls and shows a status message while it
runs — the decode that puts an image on screen in the first place never
does (`_load_print_engine`, always `silent=True`): navigating to another
image is never something the operator should have to wait on, and one
image's decode landing after the operator has already moved to another
must never overwrite what that other image is showing (`_render_from_
cached_linear`'s freshness check, `_print_engine_loaded_for`).
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
from PySide6.QtCore import QEvent, QObject, QPointF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QIcon, QKeyEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from scanassistant.core.positive_review import list_positives_by_category
from scanassistant.core.queue import ExportContext
from scanassistant.core.recovery import read_journal_entries, rebuild_export_context
from scanassistant.core.session import CaptureSession
from scanassistant.gui.widgets.histogram_widget import HistogramWidget
from scanassistant.gui.widgets.preview_area import PreviewArea
from scanassistant.gui.widgets.print_calibration_panel import AutoValues, PrintCalibrationPanel
from scanassistant.i18n import t
from scanassistant.imaging import print_engine
from scanassistant.imaging.framing import RELIABLE, ConfidenceComponents, FrameResult
from scanassistant.imaging.geometry import FrameGeometry, apply_geometry
from scanassistant.imaging.raw import RawDecoder, RawpyDecoder
from scanassistant.project import positive_linear_cache
from scanassistant.project.layout import CampaignPaths
from scanassistant.project.positive_overrides import (
    PositiveOverride,
    load_positive_overrides,
    set_positive_print_overrides,
)

# print_engine's own RAW decode + geometry crop is the ~16.7s dominant
# cost of a render — `render_print_from_linear` alone (the tonal math
# re-run on a settings change) is a small fraction of that.
# Caching the decoded-and-cropped linear array per image (bounded, since
# each is a full-resolution float32 RGB array) turns "revisit an
# already-loaded image" and "commit a slider change" into a sub-second
# re-render instead of a second full decode.
_LINEAR_CACHE_MAX = 3

# How many images ahead of the current one `_maybe_prefetch_next` decodes
# in the background — `_LINEAR_CACHE_MAX - 1`: the current image already
# occupies one of the cache's own slots, prefetching further than the
# cache can actually hold would just evict what it had barely finished
# decoding before it was ever looked at.
_PREFETCH_LOOKAHEAD = _LINEAR_CACHE_MAX - 1

# linear array, its geometry frame, a cached auto content-frame detection
# (fractions, pre-flip convention) if one was available, its mask source
# ("grabcut"/"inset_fallback", never "manual" — see `positive_linear_cache`),
# and the fingerprint that detection (if any) was validated against.
_DecodeResult = tuple[
    np.ndarray,
    FrameGeometry,
    tuple[float, float, float, float] | None,
    str | None,
    positive_linear_cache.DecodeFingerprint,
]

# Same value `gui.screens.capture` uses for its own frame/rotation commit
# debounce (`_FRAME_COMMIT_DELAY_MS`/`_ROTATION_COMMIT_DELAY_MS`) — a
# deliberately shared convention, not independently tuned.
_CONFIRM_DEBOUNCE_MS = 2500

# How often this screen's own periodic drain (`_poll_export_progress`)
# collects whatever a confirm/propagate/undo/redo's regenerate has
# finished in the background — same order of magnitude as `gui.screens.
# capture`'s own pump interval (`_MIN_PUMP_INTERVAL_MS`), not tied to it:
# nothing time-critical (no live capture) depends on this one being fast,
# just on it happening at all.
_EXPORT_POLL_INTERVAL_MS = 400
_THUMBNAIL_REFRESH_ATTEMPTS = 40  # ~16s at the interval above

# Cap on the long edge fed to `render_print_from_linear` for a *live* preview
# (`PrintCalibrationPanel.live_changed`, fired on every slider-drag tick —
# see that signal's own docstring for why it can never be wired straight to
# the cached array: a full render there costs ~1.8-2.8s even on the disk
# cache's already-downsampled 2200px array, I-190). Deliberately well under
# 1080p (this screen has no zoom — the preview area, itself narrower than
# the full window, never actually displays more than roughly a 1920x1080
# screen's worth of pixels regardless of the source array's own
# resolution), so this loses nothing an operator could actually see while
# judging a tonal change by eye. At this size the same render is a small
# fraction of a second, and the result is upscaled back to the cached
# array's own resolution before display (`_apply_live_render_result_to_
# preview`), so the crop overlay/histogram — both keyed to whatever
# resolution `preview_area.show_image` last received — never need their
# own separate coordinate space for it.
_LIVE_PREVIEW_MAX_DIM = 1024


class _CallWorker(QThread):
    """Runs a zero-arg callable on a background `QThread` and reports back
    via a signal — used for every print_engine operation this screen used
    to run synchronously on the GUI thread (decode, regenerate, propagate,
    undo/restore), once that blocking was reported as the likely cause of
    "numerous crashes": a main thread blocked for ~16.7s with no
    `processEvents()` reads to the OS/desktop environment as a hung
    application, which is usually what actually triggers an unprompted
    force-kill, not a real crash in the process itself."""

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


_ZERO_COMPONENTS = ConfidenceComponents(0.0, 0.0, 0.0, 0.0, 0.0)

# Matches `gui.screens.capture`'s own histogram overlay placement/sizing.
_HISTOGRAM_WIDTH_FRACTION = 0.09
_HISTOGRAM_ASPECT = 2.2  # width / height

_THUMBNAIL_SIZE = 128
_GRID_CELL = QSize(_THUMBNAIL_SIZE + 20, _THUMBNAIL_SIZE + 36)


@dataclass(frozen=True)
class _UndoCommand:
    """One confirmed change or propagation, undoable/redoable as a unit —
    per confirmed setting or per propagation to a selection, never per
    in-progress drag. Each
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
        self._current_name: str | None = None
        self._auto_values: AutoValues | None = None
        # Name whose print_engine render is what's actually on screen right
        # now (preview/panel/`_current_print_frame`) — `None`, or stale
        # (!= `_current_name`), means Confirm/Apply-to-selection must not
        # act yet: there is nothing genuine to persist for the image
        # currently selected. Set only by `_show_print_result`, which only
        # ever runs for a name still current at render time.
        self._print_engine_loaded_for: str | None = None
        # print_engine's own crop state — the preview is always shown at
        # native resolution, never downscaled.
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
        # name -> `_DecodeResult`, most-recently-used last. See
        # `_LINEAR_CACHE_MAX`'s docstring.
        self._linear_cache: OrderedDict[str, _DecodeResult] = OrderedDict()
        self._prefetching: set[str] = set()
        # Foreground decode requests currently in flight (distinct from
        # `_prefetching`, the silent head-start for the *next* image) — a
        # name in either set means a decode for it is already running
        # somewhere; a second navigation back onto it must not start a
        # duplicate RAW decode, just wait for whichever is already going.
        self._pending_decode_names: set[str] = set()
        # Downsampled linear + scaled `FrameGeometry` for the live-preview
        # path, memoized against `(name, id(full-resolution linear array))`
        # so a drag's many `live_changed` ticks resize once, not per tick —
        # `id()` is enough to detect the array being replaced (a fresh
        # decode/prefetch landing) since `_linear_cache` never mutates an
        # array in place.
        self._live_preview_source_cache: tuple[str, int, np.ndarray, FrameGeometry] | None = None
        # A live render already in flight never gets a second one started on
        # top of it (`_run_async` has no cancellation) — a tick arriving
        # meanwhile just marks `_live_render_pending` and gets folded into
        # one more render, with whatever the panel's values are by the time
        # the in-flight one finishes, once it does. This is what actually
        # throttles a drag's many ticks/second down to the pipeline's own
        # throughput, not a fixed timer interval.
        self._live_render_busy = False
        self._live_render_pending = False
        # Same coalescing guard as `_live_render_busy`/`_live_render_pending`
        # above, for committed (full-quality) renders instead of live-drag
        # ones: `render_print_from_linear` calls into OpenCV (GrabCut/
        # warpAffine), which isn't safe to run concurrently from two
        # `_CallWorker` threads at once — two committed renders fired back
        # to back (e.g. the Dmin picker's Auto→Manual toggle plus its own
        # explicit re-render, or two quick slider commits) could each start
        # their own worker before the other's finished, and one could hang
        # forever in native code with no Python exception to catch or log,
        # leaving the screen permanently busy-locked (operator report,
        # 2026-07-28 — a real campaign session stuck ~20 minutes, only a
        # restart recovered it).
        self._committed_render_busy = False
        self._committed_render_pending = False
        self._workers: set[_CallWorker] = set()
        self._foreground_workers: set[_CallWorker] = set()
        self._busy = False
        # Debounced auto-confirm, same pattern as `gui.screens.capture`'s
        # own frame/rotation commit timers: a real
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

        # A confirmed/propagated/restored regenerate is only *submitted*
        # here (`session.regenerate_positive`/`propagate_print_overrides`/
        # `restore_positive_override` already return once the export task
        # is queued, same contract `gui.screens.capture` relies on for its
        # own master/positive exports) — never waited out synchronously the
        # way this screen used to (`session.wait_for_pending_exports()`),
        # which held the screen busy and blocked moving to the next image
        # for as long as the actual render took (RAW decode + density math,
        # ~16.7s+ on a real image). `capture.py` gets away with never
        # waiting because its own pump timer keeps draining the export
        # queue on every tick; this screen has no such pump outside of a
        # confirm/navigation action, so it needs its own periodic drain
        # instead — same primitive `capture.py` uses while waiting out a
        # `stop(wait_for_exports=False)` (`CaptureSession.
        # collect_export_progress`, cheap/non-blocking by its own
        # contract), just polled continuously here rather than only during
        # shutdown.
        self._export_poll_timer = QTimer(self)
        self._export_poll_timer.timeout.connect(self._poll_export_progress)
        self._export_poll_timer.start(_EXPORT_POLL_INTERVAL_MS)
        # Names with a regenerate submitted but not yet visually confirmed
        # done — each poll tick retries their thumbnail icon (cheap: a
        # JPEG read + rescale) until `_THUMBNAIL_REFRESH_ATTEMPTS` have
        # passed, well past what a real regenerate normally takes; the
        # image itself is never stale (only the thumbnail icon can lag),
        # so giving up early here costs nothing but a slightly late icon.
        self._pending_thumbnail_refresh: dict[str, int] = {}
        # Guards against overlapping polls: if a journal scan somehow takes
        # longer than `_EXPORT_POLL_INTERVAL_MS` (a very large campaign),
        # the next tick must skip rather than pile up a second background
        # read on top of one still running.
        self._export_poll_in_flight = False
        # Loaded once per screen session, not re-read on every image
        # navigation/prefetch (`_journal_entries`'s own docstring) — the
        # journal grows with the whole campaign and used to be re-read and
        # re-parsed in full on every single `rebuild_export_context` call,
        # the dominant cost of just browsing between images on a large
        # campaign. Safe: this screen never itself writes a NAMING/FRAMING
        # entry (support-frame data, capture-only — `core.session.
        # apply_frame` requires the image to be `IN_REVIEW`, a state this
        # screen's images are never in), the only entry types
        # `rebuild_export_context` actually reads.
        self._journal_entries_cache: list[dict] | None = None

        self.category_deferred_checkbox = QCheckBox(t("positive_review.category_deferred"))
        self.category_deferred_checkbox.setChecked(True)
        self.category_deferred_checkbox.toggled.connect(self.refresh_list)

        self.category_applied_checkbox = QCheckBox(t("positive_review.category_applied"))
        self.category_applied_checkbox.setChecked(False)
        self.category_applied_checkbox.toggled.connect(self.refresh_list)

        self.category_manual_checkbox = QCheckBox(t("positive_review.category_manual"))
        self.category_manual_checkbox.setChecked(False)
        self.category_manual_checkbox.toggled.connect(self.refresh_list)

        # Grid of thumbnails, reusing the already-exported JPEG_POSITIVE —
        # no new decode. Multi-select via Qt's own
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
        # `QAbstractItemView`'s own key handling grabs arrow keys for grid
        # navigation whenever the list has focus (the normal state after a
        # click) — without a filter, Ctrl+Left/Right never reaches
        # `keyPressEvent` below, so the reserved rotation shortcut is
        # silently unreachable. `eventFilter` re-runs the same
        # `keyPressEvent` first and only swallows the key if that handled
        # it, so unreserved keys (Home/End/type-ahead, plain Left/Right,
        # which stay list navigation) still fall through to the list's own
        # default behavior untouched. Installed below, alongside every
        # other descendant, not here — see the loop at the end of
        # `__init__`.

        self.preview_area = PreviewArea()
        self.preview_area.frame_dragged.connect(self._on_frame_dragged)
        self.preview_area.point_picked.connect(self._on_dmin_point_picked)
        self.preview_area.picking_cancel_requested.connect(self._cancel_dmin_picking)

        self.histogram_widget = HistogramWidget(parent=self)

        self.print_panel = PrintCalibrationPanel()
        self.print_panel.settled_changed.connect(self._on_print_settings_committed)
        self.print_panel.settled_changed.connect(self._mark_edited)
        self.print_panel.live_changed.connect(self._on_print_settings_live)
        self.print_panel.pick_dmin_requested.connect(self._on_pick_dmin_requested)

        self.redetect_frame_button = QPushButton(t("positive_review.redetect_frame"))
        self.redetect_frame_button.clicked.connect(self._on_redetect_frame_requested)

        self.include_dmin_checkbox = QCheckBox(t("positive_review.include_dmin"))
        self.apply_to_selection_button = QPushButton(t("positive_review.apply_to_selection"))
        self.apply_to_selection_button.clicked.connect(self.apply_to_selection)

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
        settings_column.addWidget(self.print_panel)
        settings_column.addWidget(self.redetect_frame_button)
        settings_column.addLayout(propagation_column)
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

        # Arrow keys + their modifiers (and the other reserved shortcuts —
        # Escape, Ctrl+Z/Y/A/Enter, Space, V, Page Up/Down) must reach
        # `keyPressEvent` no matter which child widget happens to hold
        # keyboard focus — operator-reported: they stopped working the
        # moment focus moved off the thumbnail list (a button clicked, the
        # splitter dragged...). `list_widget` already needed this same
        # `eventFilter` re-run to reclaim arrows from its own grid
        # navigation; every other descendant gets the identical treatment
        # here, except a `SliderField`'s editable `QLineEdit` — Left/Right
        # there legitimately moves the text cursor while typing a value,
        # which must never be taken over.
        for descendant in self.findChildren(QWidget):
            if not isinstance(descendant, QLineEdit):
                descendant.installEventFilter(self)

    # --- background work (see `_CallWorker`) ----------------------------------

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self._busy = busy
        if status is not None:
            self.status_label.setText(status)
        for widget in (
            self.list_widget,
            self.include_dmin_checkbox,
            self.category_deferred_checkbox,
            self.category_applied_checkbox,
            self.category_manual_checkbox,
        ):
            widget.setEnabled(not busy)
        # print_engine's own decode/render never sets `busy` (silent — see
        # `_load_print_engine`), so these three stay under `_update_print_
        # engine_controls_enabled`'s own load-state gating instead of the
        # blanket toggle above: re-enabling them here regardless of whether
        # the currently-selected image has actually finished loading would
        # let Confirm act on another image's still-displayed leftovers.
        self._update_print_engine_controls_enabled()
        if busy:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()

    def _update_print_engine_controls_enabled(self) -> None:
        """Apply-to-selection/the tonal panel are only meaningful once the
        currently-selected image's own print_engine render is actually on
        screen (`_print_engine_loaded_for == _current_name`) — otherwise
        they'd act on whatever the previous image left behind. Confirm
        (Enter) guards the same condition itself rather than through a
        disabled button — there's no visible "Confirm" control anymore,
        navigating away already reviews the image (`_save_pending_edits`)."""
        ready = not self._busy and self._print_engine_loaded_for == self._current_name
        self.print_panel.setEnabled(ready)
        self.apply_to_selection_button.setEnabled(ready)

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

        `silent=True` (background prefetch, decode-on-navigate, debounced
        auto-confirm on navigating away) never touches `_set_busy`: the
        operator keeps browsing/editing while it runs, on top of
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
        self._live_preview_source_cache = None
        self._prefetching.clear()
        self._pending_decode_names.clear()
        self._pending_thumbnail_refresh.clear()
        self._print_engine_loaded_for = None
        self.refresh_list()

    def _checked_categories(self) -> frozenset[str]:
        categories: set[str] = set()
        if self.category_deferred_checkbox.isChecked():
            categories.add("deferred")
        if self.category_applied_checkbox.isChecked():
            categories.add("applied")
        if self.category_manual_checkbox.isChecked():
            categories.add("manual")
        return frozenset(categories)

    def refresh_list(self, *, select_name: str | None = None) -> None:
        session = self._session
        categories = self._checked_categories()
        if session is None or not categories:
            self._names = []
        else:
            # A deliberate, infrequent action (screen open, a filter
            # checkbox toggled) — worth a guaranteed-fresh read rather than
            # `_journal_entries()`'s cache, so a background regenerate that
            # landed since the cache was last filled is never shown under
            # the wrong category here even momentarily.
            self._journal_entries_cache = read_journal_entries(session.paths, session.fs)
            self._names = list_positives_by_category(
                session.paths, session.fs, categories, entries=self._journal_entries_cache
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

    def _remove_stale_entries(self, current_names: list[str] | None = None) -> None:
        """Removes exactly the row(s) that dropped out of the current
        category filters, leaving every other row's icon untouched — the
        cheap alternative to a full `refresh_list()` (which used to
        re-read and rescale the on-disk JPEG thumbnail of every visible
        entry on every single Confirm). Names newly *appearing* aren't
        handled here (rare outside of toggling a filter checkbox, which
        still goes through the full `refresh_list()`).

        `current_names` is normally precomputed off the Qt thread
        (`_poll_export_progress`'s own background worker — a full journal
        re-read on every tick, unbounded with the campaign's size, must
        never run directly here). `None` (the explicit single-confirm path,
        `_remove_stale_entries_and_select` — a one-off, operator-triggered
        read, not a recurring tick) computes it synchronously instead.

        Called from two places for two different reasons: right after a
        confirm (where it usually finds nothing to remove yet — the
        `POSITIVE_FRAMING` journal entry that actually flips `name`'s
        category is only written once the background regenerate this
        screen no longer waits for has genuinely finished, not at
        submission time), and from `_poll_export_progress`'s own periodic
        tick, which is what actually catches the removal once that render
        completes a few seconds later. Skipping the second call was a
        real regression: a just-confirmed image stayed visible, looking
        like it still needed review, for the rest of the screen session
        (and past a restart too, if the render hadn't reached the journal
        yet when the app closed)."""
        session = self._session
        if session is None:
            return
        if current_names is None:
            categories = self._checked_categories()
            current_names = (
                list_positives_by_category(session.paths, session.fs, categories)
                if categories
                else []
            )
        still_visible = set(current_names)
        stale = [name for name in self._names if name not in still_visible]
        if not stale:
            return
        # `takeItem` below can silently reassign Qt's own "current item" to
        # whatever slides into a removed row's place (the same quirk
        # `_remove_stale_entries_and_select` already works around for its
        # own caller) — even when every removed row belongs to some other,
        # unrelated image, which is the normal case here (this runs on
        # every periodic poll tick, catching a *different* image's
        # just-finished background regenerate). Left uncorrected, that
        # reassignment fires `currentItemChanged` on its own, and
        # `_on_current_item_changed`/`_load_index` then silently reviews
        # whatever the operator is still actually looking at — confirmed in
        # real use (2026-08-03): an untouched image flipped to "done
        # manually" after a few seconds, with no navigation at all. Restored
        # with signals blocked (`refresh_list`'s own pattern for the same
        # reason): this only corrects Qt's bookkeeping back to what it
        # already was, never a real navigation event worth reacting to.
        current_name = self._current_name
        for name in reversed(stale):
            row = self._names.index(name)
            self._names.pop(row)
            self.list_widget.takeItem(row)
            self._pending_thumbnail_refresh.pop(name, None)
        if current_name is not None and current_name in self._names:
            target_row = self._names.index(current_name)
            if self.list_widget.currentRow() != target_row:
                self.list_widget.blockSignals(True)
                self.list_widget.setCurrentRow(target_row)
                self.list_widget.blockSignals(False)

    def _remove_stale_entries_and_select(self, select_name: str | None) -> None:
        self._remove_stale_entries()
        target_row = self._names.index(select_name) if select_name in self._names else 0
        # Forced through -1 first: `takeItem` can leave some *other* row
        # already "current" at the Qt level (the item that slid into the
        # removed one's place) — going straight to `target_row` would then
        # silently no-op whenever that happens to already be it, and
        # `_on_current_item_changed`/`_load_index` would never fire for
        # the image this confirm is actually supposed to land on.
        self.list_widget.setCurrentRow(-1)
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
            self._current_name = None
            self._current_print_frame = None
            self._current_print_frame_shape = None
            self._auto_values = None
            self._print_engine_loaded_for = None
            self.apply_to_selection_button.setEnabled(False)
            self.preview_area.show_message(t("positive_review.nothing_to_review"))
            self.status_label.setText("")
            self.histogram_widget.set_pixels(None)
            return
        name = self._names[index]
        self._current_name = name
        self.status_label.setText(
            t("positive_review.reviewing", index=index + 1, total=len(self._names), name=name)
        )
        if _load_master_jpeg(session.paths, name) is None:
            self.preview_area.show_message(t("positive_review.master_unavailable", name=name))
            self.histogram_widget.set_pixels(None)
            return

        self._print_engine_loaded_for = None
        self._update_print_engine_controls_enabled()
        self._load_print_engine(name)

    def _load_print_engine(self, name: str) -> None:
        """Gets this image's positive on screen: instantly, from
        `_linear_cache`, if it's been decoded before this screen session;
        otherwise a real RAW decode + geometry crop, which takes ~16.7s on
        the reference test machine.

        Always `silent=True`: unlike Confirm/Apply-to-selection/undo/redo,
        looking at an image is never a "commit" the operator should have to
        wait on — `_set_busy` never runs for this, so navigating to another
        image immediately (before this one even finishes decoding) stays
        possible. What must never happen instead is a stale decode landing
        on the wrong image once it finishes: `name` is fixed by closure at
        call time (this method is only ever invoked with `name ==
        _current_name`), but `_current_name` itself can have moved on by
        the time `_on_decoded` runs — every write to shared preview/panel
        state is funneled through `_render_from_cached_linear`, which
        re-checks that before touching anything. A duplicate decode for a
        `name` already in flight (`_pending_decode_names`) or being
        prefetched (`_prefetching`) is skipped outright: whichever is
        already running renders this image itself once done, if it's still
        the one on screen by then."""
        cached = self._linear_cache.get(name)
        if cached is not None:
            self._linear_cache.move_to_end(name)
            linear, frame_in_output, content_frame, content_mask_source, fingerprint = cached
            self._render_from_cached_linear(
                name, linear, frame_in_output, content_frame, content_mask_source, fingerprint
            )
            return

        if name in self._prefetching or name in self._pending_decode_names:
            self.preview_area.show_message(t("positive_review.loading", name=name))
            return

        decode = self._build_decode_task(name)
        if decode is None:
            self.preview_area.show_message(t("positive_review.master_unavailable", name=name))
            return

        self.preview_area.show_message(t("positive_review.loading", name=name))

        def _on_decoded(result: _DecodeResult) -> None:
            self._pending_decode_names.discard(name)
            linear, frame_in_output, content_frame, content_mask_source, fingerprint = result
            self._cache_linear(
                name, linear, frame_in_output, content_frame, content_mask_source, fingerprint
            )
            self._render_from_cached_linear(
                name, linear, frame_in_output, content_frame, content_mask_source, fingerprint
            )

        def _on_failed(message: str) -> None:
            self._pending_decode_names.discard(name)
            if name == self._current_name:
                self.preview_area.show_message(t("positive_review.master_unavailable", name=name))
                self.status_label.setText(message)

        self._pending_decode_names.add(name)
        self._run_async(decode, _on_decoded, on_failure=_on_failed, silent=True)

    def _journal_entries(self) -> list[dict]:
        """The campaign journal, read once per screen session and reused —
        see `self._journal_entries_cache`'s own comment for why this is
        safe here. `_poll_export_progress`'s own category check
        deliberately does *not* use this cache (a fresh read, on its own
        background thread): that one specifically exists to notice a
        background regenerate's `POSITIVE_FRAMING` entry landing, which
        this cache would otherwise never see for the rest of the
        session."""
        session = self._session
        assert session is not None
        if self._journal_entries_cache is None:
            self._journal_entries_cache = read_journal_entries(session.paths, session.fs)
        return self._journal_entries_cache

    def _rebuild_context(self, name: str) -> ExportContext | None:
        session = self._session
        assert session is not None
        return rebuild_export_context(
            name,
            session.paths,
            session.fs,
            session.campaign.capture.extensions,
            entries=self._journal_entries(),
        )

    def _build_decode_task(self, name: str) -> Callable[[], _DecodeResult] | None:
        """The expensive half of a print_engine render (RAW decode +
        geometry crop) as a zero-arg callable ready for `_run_async` —
        `None` if `name`'s support frame can't be rebuilt from the journal.
        Shared by the foreground load (`_load_print_engine`) and the silent
        next-image prefetch (`_maybe_prefetch_next`). Also returns whatever
        content-frame detection `positive_linear_cache` already carries for
        this image (from the capture-time finalize pass, most often) —
        `None`/`None` on a cache miss, letting `_render_from_cached_linear`
        fall back to a fresh GrabCut and cache its own result for next
        time — plus the fingerprint needed to write that back."""
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
        paths = session.paths

        def _decode() -> _DecodeResult:
            fingerprint = positive_linear_cache.DecodeFingerprint.for_decode(
                raw_path=raw_path,
                frame=frame,
                rotation_deg=rotation_deg,
                size_mode=size_mode,
                final_dimensions_px=final_dimensions_px,
                white_balance=user_wb,
            )
            cached = positive_linear_cache.load(paths, name, fingerprint)
            if cached is not None:
                return (
                    cached.linear,
                    cached.frame_in_output,
                    cached.content_frame,
                    cached.content_mask_source,
                    fingerprint,
                )
            development = decoder.develop(raw_path, user_wb=user_wb, linear=True)
            geometry = apply_geometry(
                development.pixels,
                frame,
                rotation_deg=rotation_deg,
                size_mode=size_mode,
                final_dimensions_px=final_dimensions_px,
            )
            linear = geometry.pixels.astype(np.float32) / 65535.0
            positive_linear_cache.save(paths, name, linear, geometry.frame_in_output, fingerprint)
            return linear, geometry.frame_in_output, None, None, fingerprint

        return _decode

    def _cache_linear(
        self,
        name: str,
        linear: np.ndarray,
        frame_in_output: FrameGeometry,
        content_frame: tuple[float, float, float, float] | None,
        content_mask_source: str | None,
        fingerprint: positive_linear_cache.DecodeFingerprint,
    ) -> None:
        self._linear_cache[name] = (
            linear,
            frame_in_output,
            content_frame,
            content_mask_source,
            fingerprint,
        )
        self._linear_cache.move_to_end(name)
        while len(self._linear_cache) > _LINEAR_CACHE_MAX:
            self._linear_cache.popitem(last=False)

    def _maybe_prefetch_next(self, name: str) -> None:
        """Silently decodes up to `_PREFETCH_LOOKAHEAD` images ahead of
        `name` in the current filtered list while the operator is still
        looking at it — by the time they move on (once, or several times
        in a row), each following image is often already decoded, instead
        of every single navigation paying the full RAW decode cost even
        for a plain, unhurried review workflow. Strictly sequential (one
        decode in flight at a time, never two at once — each step only
        starts the next once its own decode has actually finished): a
        `_LINEAR_CACHE_MAX`-sized cache and a single background decode
        already dominates the machine enough on its own (I-179's own
        finalize-pool sizing already accounts for exactly this kind of
        concurrent RAW decode cost) without a second one racing it purely
        for a look-ahead that isn't even on screen yet. Never sets `_busy`
        (`_run_async(silent=True)`): this is a pure head-start, not
        something the operator should ever have to wait on directly."""
        if name not in self._names:
            return
        index = self._names.index(name)
        self._prefetch_ahead(index, _PREFETCH_LOOKAHEAD)

    def _prefetch_ahead(self, from_index: int, remaining: int) -> None:
        if remaining <= 0:
            return
        index = from_index + 1
        if index >= len(self._names):
            return
        next_name = self._names[index]
        already_cached = next_name in self._linear_cache
        already_pending = (
            already_cached
            or next_name in self._prefetching
            or next_name in self._pending_decode_names
        )
        if already_pending:
            # Already covered (a cache hit, or another decode already in
            # flight for it) — keep the chain going one step further out
            # rather than stalling the look-ahead depth on a step that
            # needed no work here.
            self._prefetch_ahead(index, remaining - 1)
            return
        decode = self._build_decode_task(next_name)
        if decode is None:
            return

        def _on_decoded(result: _DecodeResult) -> None:
            self._prefetching.discard(next_name)
            linear, frame_in_output, content_frame, content_mask_source, fingerprint = result
            self._cache_linear(
                next_name, linear, frame_in_output, content_frame, content_mask_source, fingerprint
            )
            if next_name == self._current_name:
                self._render_from_cached_linear(
                    next_name,
                    linear,
                    frame_in_output,
                    content_frame,
                    content_mask_source,
                    fingerprint,
                )
            self._prefetch_ahead(index, remaining - 1)

        def _on_failed(_message: str) -> None:
            self._prefetching.discard(next_name)

        self._prefetching.add(next_name)
        self._run_async(decode, _on_decoded, on_failure=_on_failed, silent=True)

    def _render_from_cached_linear(
        self,
        name: str,
        linear: np.ndarray,
        frame_in_output: FrameGeometry,
        content_frame: tuple[float, float, float, float] | None,
        content_mask_source: str | None,
        fingerprint: positive_linear_cache.DecodeFingerprint,
    ) -> None:
        """The cheap half of a print_engine render (`render_print_from_linear`
        alone — density math + GrabCut content-frame, no RAW decode):
        driven directly off `_linear_cache`, so a repeat visit or a
        committed settings change never re-decodes.

        `content_frame`/`content_mask_source` (from `positive_linear_cache`,
        via `_build_decode_task`): an automatic detection already computed
        elsewhere (capture-time finalize, most often) — passed through as
        `cached_content_frame` so `render_print_from_linear` skips GrabCut
        entirely when nothing better (an operator crop) applies. `None`
        means no such detection is cached yet: after this render, whatever
        GrabCut/inset just ran gets written back (off the GUI thread) so
        the *next* visit to this image is instant too — an operator crop
        already in play (`content_frame_override` below) never triggers
        this, it isn't an automatic detection to cache.

        The single point every decode/prefetch completion funnels through
        before touching the preview/panel/`_current_print_frame` — `name`
        may no longer be the image on screen by the time an async decode
        finishes (the operator moved on already), in which case this is a
        no-op: the result stays cached for whenever they come back, but
        nothing here overwrites what's currently displayed for a different
        image. Also triggers the next-image prefetch, but only once this
        render is confirmed to be the one actually shown.

        Always renders `crop_to_content=False` — the full support-frame
        positive with a draggable overlay, rather than the already-cropped
        result an operator could do nothing with."""
        if name != self._current_name:
            return
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
            content_frame_angle_deg=self._pending_print_content_frame_angle_deg(
                name, override, horizontal_flip
            ),
        )
        result = print_engine.render_print_from_linear(
            linear.astype(np.float64),
            frame_in_output,
            overrides=overrides,
            horizontal_flip=horizontal_flip,
            crop_to_content=False,
            cached_content_frame=content_frame,
            cached_content_mask_source=content_mask_source,
        )
        self._show_print_result(name, result, override)
        if content_frame_override is None and content_frame is None:
            detected_fraction = _print_frame_to_fraction(
                _print_frame_from_rect(result.content_frame, result.content_frame_angle_deg),
                full_width,
                full_height,
                horizontal_flip,
            )
            self._cache_linear(
                name,
                linear,
                frame_in_output,
                detected_fraction,
                result.content_mask_source,
                fingerprint,
            )
            paths = session.paths

            def _persist_detection() -> None:
                positive_linear_cache.save(
                    paths,
                    name,
                    linear,
                    frame_in_output,
                    fingerprint,
                    content_frame=detected_fraction,
                    content_mask_source=result.content_mask_source,
                )

            self._run_async(_persist_detection, lambda _: None, silent=True)
        self._maybe_prefetch_next(name)

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

    def _pending_print_content_frame_angle_deg(
        self, name: str, override: PositiveOverride | None, horizontal_flip: bool
    ) -> float:
        """Same priority order as `_pending_print_content_frame`, for the
        crop's own deskew angle."""
        pending = self._pending_print_frames.get(name)
        if pending is not None:
            return _print_frame_to_angle_deg(pending, horizontal_flip)
        if override is not None:
            return override.print_content_frame_angle_deg
        return 0.0

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
        self._current_print_frame = _print_frame_from_rect(
            result.content_frame, result.content_frame_angle_deg
        )
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
        self._print_engine_loaded_for = name
        self._update_print_engine_controls_enabled()

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
        the operator already dragged into place.

        Coalesced like `_on_print_settings_live`: a commit arriving while
        one is already rendering just marks `_committed_render_pending`
        and returns — the next render, once the in-flight one finishes,
        reads the panel's values fresh. Never two `_CallWorker`s running
        `render_print_from_linear` (OpenCV GrabCut/warpAffine) at once —
        that combination isn't safe to run concurrently and could hang a
        worker thread indefinitely with nothing to catch or log."""
        if self._current_name is None:
            return
        if self._print_engine_loaded_for != self._current_name:
            return  # nothing genuine on screen yet for this image
        if self._committed_render_busy:
            self._committed_render_pending = True
            return
        name = self._current_name
        cached = self._linear_cache.get(name)
        if cached is None:
            return  # still decoding for the first time; nothing to re-render yet
        linear, frame_in_output, _content_frame, _content_mask_source, _fingerprint = cached
        session = self._session
        assert session is not None
        horizontal_flip = session.campaign.exports.jpeg_positive.horizontal_flip
        full_height, full_width = linear.shape[:2]
        content_frame_override = None
        content_frame_angle_deg = 0.0
        if self._current_print_frame is not None:
            content_frame_override = _print_frame_to_fraction(
                self._current_print_frame, full_width, full_height, horizontal_flip
            )
            content_frame_angle_deg = _print_frame_to_angle_deg(
                self._current_print_frame, horizontal_flip
            )
        overrides = replace(
            self.print_panel.current_overrides(),
            content_frame=content_frame_override,
            content_frame_angle_deg=content_frame_angle_deg,
        )

        def _render() -> print_engine.PrintResult:
            return print_engine.render_print_from_linear(
                linear.astype(np.float64),
                frame_in_output,
                overrides=overrides,
                horizontal_flip=horizontal_flip,
                crop_to_content=False,
            )

        def _on_rendered(result: print_engine.PrintResult) -> None:
            self._committed_render_busy = False
            pending = self._committed_render_pending
            self._committed_render_pending = False
            if name == self._current_name:
                self._apply_render_result_to_preview(result)
            if pending:
                self._on_print_settings_committed()

        def _on_failed(message: str) -> None:
            self._committed_render_busy = False
            self._committed_render_pending = False
            QMessageBox.warning(self, t("positive_review.title"), message)

        self._committed_render_busy = True
        self._run_async(
            _render,
            _on_rendered,
            busy_text=t("positive_review.rendering", name=name),
            on_failure=_on_failed,
        )

    def _live_preview_source(
        self, name: str, linear: np.ndarray, frame_in_output: FrameGeometry
    ) -> tuple[np.ndarray, FrameGeometry]:
        """The (memoized) downsampled `linear`/`frame_in_output` pair a live
        render actually runs on — see `_LIVE_PREVIEW_MAX_DIM`. Recomputed
        only when `name` or the underlying array identity changes, never
        per slider tick."""
        cached = self._live_preview_source_cache
        if cached is not None and cached[0] == name and cached[1] == id(linear):
            return cached[2], cached[3]
        height, width = linear.shape[:2]
        scale = min(1.0, _LIVE_PREVIEW_MAX_DIM / max(height, width))
        if scale >= 1.0:
            small_linear, small_frame = linear, frame_in_output
        else:
            small_linear = cv2.resize(
                linear.astype(np.float32),
                (round(width * scale), round(height * scale)),
                interpolation=cv2.INTER_AREA,
            )
            small_frame = FrameGeometry(
                x=frame_in_output.x * scale,
                y=frame_in_output.y * scale,
                width=frame_in_output.width * scale,
                height=frame_in_output.height * scale,
                angle_deg=frame_in_output.angle_deg,
            )
        self._live_preview_source_cache = (name, id(linear), small_linear, small_frame)
        return small_linear, small_frame

    def _on_print_settings_live(self) -> None:
        """`PrintCalibrationPanel.live_changed`: a cheap, reduced-resolution
        re-render while a slider is still being dragged — the full-quality
        commit still happens separately on release (`settled_changed` →
        `_on_print_settings_committed`), unaffected by this. Reuses
        `self._current_print_frame` for the crop, same as a committed
        render — a live tonal drag must not move or reset a crop the
        operator already placed.

        Coalesced, not queued: while a live render is already in flight,
        a tick just sets `_live_render_pending` and returns — the next
        render (once the in-flight one finishes) reads the panel's values
        fresh, so it always reflects wherever the drag actually is by then,
        never a backlog of stale intermediate positions."""
        if self._current_name is None or self._print_engine_loaded_for != self._current_name:
            return
        if self._live_render_busy:
            self._live_render_pending = True
            return
        name = self._current_name
        cached = self._linear_cache.get(name)
        if cached is None:
            return
        linear, frame_in_output, _content_frame, _content_mask_source, _fingerprint = cached
        small_linear, small_frame = self._live_preview_source(name, linear, frame_in_output)
        session = self._session
        assert session is not None
        horizontal_flip = session.campaign.exports.jpeg_positive.horizontal_flip
        full_height, full_width = linear.shape[:2]
        content_frame_override = None
        content_frame_angle_deg = 0.0
        if self._current_print_frame is not None:
            content_frame_override = _print_frame_to_fraction(
                self._current_print_frame, full_width, full_height, horizontal_flip
            )
            content_frame_angle_deg = _print_frame_to_angle_deg(
                self._current_print_frame, horizontal_flip
            )
        overrides = replace(
            self.print_panel.current_overrides(),
            content_frame=content_frame_override,
            content_frame_angle_deg=content_frame_angle_deg,
        )
        target_shape = (linear.shape[0], linear.shape[1])

        def _render() -> print_engine.PrintResult:
            return print_engine.render_print_from_linear(
                small_linear.astype(np.float64),
                small_frame,
                overrides=overrides,
                horizontal_flip=horizontal_flip,
                crop_to_content=False,
            )

        def _on_rendered(result: print_engine.PrintResult) -> None:
            self._live_render_busy = False
            pending = self._live_render_pending
            self._live_render_pending = False
            if name == self._current_name:
                self._apply_live_render_result_to_preview(result, target_shape)
            if pending:
                self._on_print_settings_live()

        def _on_failed(_message: str) -> None:
            self._live_render_busy = False
            self._live_render_pending = False

        self._live_render_busy = True
        self._run_async(_render, _on_rendered, on_failure=_on_failed, silent=True)

    def _apply_live_render_result_to_preview(
        self, result: print_engine.PrintResult, target_shape: tuple[int, int]
    ) -> None:
        """Same tail as `_apply_render_result_to_preview`, but the rendered
        array is upscaled back to `target_shape` (the cached array's own
        resolution, whatever `preview_area` is currently showing) first —
        cheap next to the tonal render itself, and it means the crop
        overlay/histogram never need a separate reduced-resolution
        coordinate space of their own for this path."""
        positive8 = (result.pixels // 257).astype(np.uint8)
        if positive8.shape[:2] != target_shape:
            positive8 = cv2.resize(
                positive8,
                (target_shape[1], target_shape[0]),
                interpolation=cv2.INTER_LINEAR,
            )
        rgb = np.stack([positive8, positive8, positive8], axis=-1)
        self.preview_area.show_image(rgb)
        self.histogram_widget.set_pixels(rgb)
        self._reposition_histogram()

    def _apply_render_result_to_preview(self, result: print_engine.PrintResult) -> None:
        """Shared tail of every re-render that only touches the preview/
        crop-overlay/histogram, not the panel or status label (unlike
        `_show_print_result`, the initial-load path, which also re-binds
        `print_panel`/`_auto_values`/the status text)."""
        positive8 = (result.pixels // 257).astype(np.uint8)
        rgb = np.stack([positive8, positive8, positive8], axis=-1)
        self.preview_area.show_image(rgb)
        self._current_print_frame = _print_frame_from_rect(
            result.content_frame, result.content_frame_angle_deg
        )
        self._current_print_frame_shape = rgb.shape[:2]
        self.preview_area.set_frame_overlay(self._current_print_frame)
        self.histogram_widget.set_pixels(rgb)
        self._reposition_histogram()

    def _on_redetect_frame_requested(self) -> None:
        """ "Redetect frame": forces a fresh GrabCut/inset detection,
        bypassing both the persisted `print_content_frame` override and
        `positive_linear_cache`'s own cached detection — the one explicit
        way to get a new automatic proposal instead of what's currently
        shown. The result becomes the new pending crop, going through the
        same debounced auto-confirm as a manual drag (`_mark_edited`) —
        no separate persistence path to duplicate."""
        name = self._current_name
        if name is None or self._print_engine_loaded_for != name:
            return
        cached = self._linear_cache.get(name)
        if cached is None:
            return
        linear, frame_in_output, _content_frame, _content_mask_source, _fingerprint = cached
        session = self._session
        assert session is not None
        horizontal_flip = session.campaign.exports.jpeg_positive.horizontal_flip
        # `content_frame_angle_deg=0.0` explicitly, not left to default: a
        # fresh automatic detection is always axis-aligned, and this must
        # keep resetting it even if `current_overrides()` ever starts
        # carrying a nonzero angle of its own.
        overrides = replace(
            self.print_panel.current_overrides(), content_frame=None, content_frame_angle_deg=0.0
        )

        def _render() -> print_engine.PrintResult:
            return print_engine.render_print_from_linear(
                linear.astype(np.float64),
                frame_in_output,
                overrides=overrides,
                horizontal_flip=horizontal_flip,
                crop_to_content=False,
            )

        def _on_rendered(result: print_engine.PrintResult) -> None:
            if name != self._current_name:
                return
            self._apply_render_result_to_preview(result)
            self._mark_edited()

        self._run_async(_render, _on_rendered, busy_text=t("positive_review.rendering", name=name))

    def _on_pick_dmin_requested(self) -> None:
        """`PrintCalibrationPanel.pick_dmin_requested` ("Pick from image"):
        arms the preview's crosshair, mirroring `gui.screens.capture`'s own
        white-balance picker (arm → pick → sample → apply → disarm) — the
        next click samples `_on_dmin_point_picked` instead of drag-editing
        the crop."""
        if self._current_name is None or self._print_engine_loaded_for != self._current_name:
            return
        self.preview_area.set_picking_enabled(True)
        self.status_label.setText(t("positive_review.picking_dmin"))

    def _cancel_dmin_picking(self) -> None:
        """Right-click on the preview, or Escape, while armed (`_on_pick_dmin_
        requested`) — arming it by mistake used to force actually sampling a
        point (or leaving the whole screen, since Escape unconditionally
        closed it) just to get back to the normal crop-drag tool."""
        self.preview_area.set_picking_enabled(False)
        self.status_label.setText(t("positive_review.picking_dmin_cancelled"))

    def _on_dmin_point_picked(self, point: QPointF) -> None:
        """Samples the film base color directly from the already-decoded
        *linear* negative at the clicked point — not the rendered positive
        (`preview_area`'s displayed pixels), which would give the wrong
        color entirely (post density/tone-curve/paper model). The clicked
        point is in the displayed preview's own coordinate space
        (`crop_to_content=False`, already mirrored when `horizontal_flip`)
        — mirrored back before indexing into `linear`, same convention
        `_print_frame_to_fraction` already applies to a dragged crop."""
        self.preview_area.set_picking_enabled(False)
        name = self._current_name
        if name is None or self._print_engine_loaded_for != name:
            return
        cached = self._linear_cache.get(name)
        if cached is None:
            return
        linear, _frame_in_output, _content_frame, _content_mask_source, _fingerprint = cached
        session = self._session
        assert session is not None
        horizontal_flip = session.campaign.exports.jpeg_positive.horizontal_flip
        full_height, full_width = linear.shape[:2]
        x = round(point.x())
        if horizontal_flip:
            x = full_width - 1 - x
        y = round(point.y())
        x = max(0, min(full_width - 1, x))
        y = max(0, min(full_height - 1, y))
        r, g, b = print_engine.sample_dmin_at_point(linear.astype(np.float64), x, y)

        # Set the sliders *before* flipping the group to Manual: that flip
        # fires `committed`/`settled_changed` synchronously (`_Group.
        # _on_toggled`), which re-renders and persists from whatever the
        # sliders hold at that instant — setting them after would commit
        # the stale, pre-pick value instead of the one just sampled.
        was_manual = self.print_panel.dmin_group.is_manual()
        self.print_panel.dmin_r.setValue(r)
        self.print_panel.dmin_g.setValue(g)
        self.print_panel.dmin_b.setValue(b)
        self.print_panel.dmin_group.set_manual(True)
        self._mark_edited()
        # `set_manual(True)` above already fired `committed`/`settled_
        # changed` (hence a render) on its own if the group was still on
        # Auto — the operator's first pick on a given image, by far the
        # common case. Calling `_on_print_settings_committed` again there
        # queued a second full render right behind the first one: on a
        # real campaign image (operator report, 2026-07-28) each render is
        # several seconds, so that "harmless duplicate" was actually
        # doubling the wait on every first pick. Only genuinely needed when
        # the group was already Manual, where `set_manual(True)` is a
        # no-op and never rendered anything.
        if was_manual:
            self._on_print_settings_committed()

    def _save_pending_edits(self) -> None:
        """The single choke point every way of leaving the current image
        goes through (navigation, close, switching campaigns): an operator
        who looked at an image and moved on has reviewed it, whether or not
        they touched anything — no separate "Confirm" action required.

        First flushes a debounced auto-confirm if one is pending (an edit
        the operator made but the `_CONFIRM_DEBOUNCE_MS` quiet period
        hasn't elapsed for yet must never be silently dropped just because
        they moved on before it fired, same guarantee `gui.screens.capture`'s
        own commit timers give) — that real edit needs an actual
        `regenerate_positive` (`_auto_confirm_silently`), the pixels
        genuinely changed. Otherwise, if a render was actually shown for
        this image, the cheap path (`_mark_reviewed_without_render`) flips
        it to reviewed without paying for a RAW decode + density render
        that would reproduce exactly what's already on disk.

        A pending edit is only trusted if it was made against this image's
        own actually-loaded render (`_print_engine_loaded_for == name`) —
        the panel/preview can only be interacted with once that's true
        (both are disabled until then), but this is the single choke point
        every navigation goes through, so it stays the authoritative guard
        rather than trusting the UI state alone."""
        name = self._current_name
        if name is None:
            return
        if self._confirm_pending:
            self._confirm_debounce_timer.stop()
            self._confirm_pending = False
            if self._print_engine_loaded_for == name:
                self._auto_confirm_silently()
            return
        if self._current_print_frame is not None:
            self._pending_print_frames[name] = self._current_print_frame
        if self._print_engine_loaded_for == name:
            self._mark_reviewed_without_render(name)

    def _on_frame_dragged(self, frame: FrameResult) -> None:
        """`frame` is in the currently *displayed* preview's own coordinate
        space — the preview is always shown at its own native resolution
        (no manual downscale), so `frame` is already in the right space,
        stored as-is."""
        self._current_print_frame = frame
        self._mark_edited()

    def _nudge_print_frame(self, *, dx: int = 0, dy: int = 0) -> None:
        """Up/Down/Left/Right (reserved, always active — see
        `keyPressEvent`): moves the content-frame crop, same 1 px / 10
        px-with-Shift convention as `gui.screens.capture._nudge_frame`'s
        own support-frame nudge. Mouse drag remains the primary way to
        position it; this is for a fine keyboard nudge once a drag has
        gotten close. Same overlay-only + debounce pattern as
        `_rotate_print_frame` — the actual pixels only change once that
        commits."""
        if self._current_print_frame is None or self._print_engine_loaded_for != self._current_name:
            return
        frame = self._current_print_frame
        self._current_print_frame = replace(frame, x=frame.x + dx, y=frame.y + dy)
        self.preview_area.set_frame_overlay(self._current_print_frame)
        self._mark_edited()

    def _rotate_print_frame(self, delta_deg: float) -> None:
        """Ctrl+Left/Right (reserved, always active — see `keyPressEvent`):
        deskews the content-frame crop, bounded to [-45, 45]° same as
        `gui.screens.capture._rotate_frame`'s own support-frame rotation.
        Mouse drag (`_on_frame_dragged`) never touches the angle at all —
        this is the only way to set one. Only updates the overlay and
        arms the debounce, same as a drag: the actual pixels only change
        once that commits (`_on_print_settings_committed`/`_auto_confirm_
        silently`), which already re-renders from whatever
        `_current_print_frame` currently holds."""
        if self._current_print_frame is None or self._print_engine_loaded_for != self._current_name:
            return
        frame = self._current_print_frame
        new_angle = max(-45.0, min(45.0, frame.angle_deg + delta_deg))
        self._current_print_frame = replace(frame, angle_deg=new_angle)
        self.preview_area.set_frame_overlay(self._current_print_frame)
        self._mark_edited()

    # --- confirm / apply to selection / undo ----------------------------------

    def _mark_edited(self) -> None:
        """A real edit happened (crop drag, a print_engine group toggled or
        a slider committed) — (re)starts the debounced auto-confirm, same
        pattern as `gui.screens.capture`'s own
        frame/rotation commit timers. A no-op
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

    def _poll_export_progress(self) -> None:
        """Periodic, non-blocking drain of whatever a confirm/propagate/
        undo/redo's regenerate has finished in the background since the
        last tick (`CaptureSession.collect_export_progress` — journals and
        persists state, submits nothing new) — the counterpart, for this
        screen, of `gui.screens.capture`'s own pump timer doing the same
        thing on every tick while a session is running. Also removes any
        name that's since dropped out of the current category filters
        (`_remove_stale_entries` — a just-confirmed image doesn't actually
        change category until this later completion, not at submission
        time) and retries the thumbnail icon of any name still in
        `_pending_thumbnail_refresh` (see its own docstring).

        The journal/state I/O this needs (`collect_export_progress`,
        `list_positives_by_category` — a full re-read and re-parse of the
        *whole* campaign journal, unbounded, only ever growing) runs on a
        background worker, never directly on this tick — confirmed in real
        use to freeze the app long enough to be force-killed as
        unresponsive when done straight on the Qt thread every single
        tick, the same class of bug `_CallWorker`'s own docstring already
        describes for a blocking render. Only the actual list-widget
        mutation (cheap) happens back here, once the read completes."""
        session = self._session
        if session is None or self._export_poll_in_flight:
            return
        self._export_poll_in_flight = True
        categories = self._checked_categories()

        def _work() -> list[str]:
            session.collect_export_progress()
            return (
                list_positives_by_category(session.paths, session.fs, categories)
                if categories
                else []
            )

        def _on_done(current_names: object) -> None:
            self._export_poll_in_flight = False
            if self._session is not session:
                return  # a different campaign was loaded while this ran
            self._remove_stale_entries(current_names)  # type: ignore[arg-type]
            self._retry_pending_thumbnails()

        def _on_failed(_message: str) -> None:
            self._export_poll_in_flight = False

        self._run_async(_work, _on_done, on_failure=_on_failed, silent=True)

    def _retry_pending_thumbnails(self) -> None:
        if not self._pending_thumbnail_refresh:
            return
        for name, attempts_left in list(self._pending_thumbnail_refresh.items()):
            row = self._names.index(name) if name in self._names else -1
            item = self.list_widget.item(row) if row >= 0 else None
            if item is not None:
                item.setIcon(self._thumbnail_icon(name))
            remaining = attempts_left - 1
            if remaining <= 0 or item is None:
                del self._pending_thumbnail_refresh[name]
            else:
                self._pending_thumbnail_refresh[name] = remaining

    def _auto_confirm_silently(self) -> None:
        """The debounced/navigate-away half of the auto-confirm: persists
        whatever the operator just changed without an explicit Confirm
        click. Silent for print_engine
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

        self._persist_print_overrides_for_current(name)

        def _regenerate() -> None:
            # Submits the regenerate and returns — never waits for it to
            # actually finish (`_poll_export_progress`'s own docstring).
            session.regenerate_positive(name)

        def _on_regenerated(_result: object) -> None:
            self._record_confirm(name, before, row)

        self._run_async(_regenerate, _on_regenerated, silent=True)

    def _persist_print_overrides_for_current(self, name: str) -> None:
        """Writes `name`'s current tonal + crop state as its print_engine
        override — the actual "confirm" for that engine, shared by the
        explicit Confirm action and the silent debounced auto-confirm."""
        session = self._session
        assert session is not None
        overrides = self.print_panel.current_overrides()
        content_frame = None
        content_frame_angle_deg = 0.0
        if self._current_print_frame is not None and self._current_print_frame_shape is not None:
            full_height, full_width = self._current_print_frame_shape
            horizontal_flip = session.campaign.exports.jpeg_positive.horizontal_flip
            content_frame = _print_frame_to_fraction(
                self._current_print_frame, full_width, full_height, horizontal_flip
            )
            content_frame_angle_deg = _print_frame_to_angle_deg(
                self._current_print_frame, horizontal_flip
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
            content_frame_angle_deg=content_frame_angle_deg,
        )
        self._pending_print_frames.pop(name, None)

    def _mark_reviewed_without_render(self, name: str) -> None:
        """The operator looked at `name` (its print_engine render was
        genuinely on screen) and moved on without touching the crop or any
        tonal control — the pixels already on disk are still correct, so
        this only needs to flip the journal's `POSITIVE_FRAMING` outcome to
        `manual` (`CaptureSession.mark_positive_reviewed`), never a real
        `regenerate_positive` — that would pay for the full RAW decode +
        density render for zero visual difference, which `_confirm_pending`
        (a real edit) already routes through `_auto_confirm_silently`
        instead. Geometry comes straight from what's already on screen
        (`_current_print_frame`), same conversion `_persist_print_overrides_
        for_current` uses — nothing here re-reads the journal or re-decodes
        anything, so it stays cheap enough to run on every plain navigation."""
        session = self._session
        if (
            session is None
            or self._current_print_frame is None
            or self._current_print_frame_shape is None
        ):
            return
        full_height, full_width = self._current_print_frame_shape
        horizontal_flip = session.campaign.exports.jpeg_positive.horizontal_flip
        frame = self._current_print_frame
        content_frame_fraction = _print_frame_to_fraction(
            frame, full_width, full_height, horizontal_flip
        )
        content_frame_angle_deg = _print_frame_to_angle_deg(frame, horizontal_flip)
        session.mark_positive_reviewed(
            name,
            x=frame.x,
            y=frame.y,
            width=frame.width,
            height=frame.height,
            content_frame_fraction=content_frame_fraction,
            angle_deg=content_frame_angle_deg,
        )
        self._pending_thumbnail_refresh[name] = _THUMBNAIL_REFRESH_ATTEMPTS

    def _record_confirm(
        self, name: str, before: dict[str, PositiveOverride | None], row: int
    ) -> None:
        """Undo-stack entry + thumbnail refresh only — no list navigation
        (shared by `_finish_confirm`, which adds that, and the silent
        auto-confirm, which deliberately doesn't). The icon set here is
        immediate but likely stale — the regenerate this follows is only
        just submitted, not finished (`_poll_export_progress`'s own
        docstring) — so `name` is also queued for a few more retries once
        the real file is actually ready."""
        session = self._session
        assert session is not None
        after = _snapshot(session.paths, session.fs, [name])
        self._push_undo(_UndoCommand("confirm", (name,), before, after))
        item = self.list_widget.item(row)
        if item is not None:
            item.setIcon(self._thumbnail_icon(name))
        self._pending_thumbnail_refresh[name] = _THUMBNAIL_REFRESH_ATTEMPTS

    def _rotate_current_image(self, *, direction: int = 1) -> None:
        """V/Shift+V: corrects a 90° orientation missed during capture —
        the one thing this screen otherwise never touches (the module
        docstring's "never touches the TIFF/JPEG master" is about the
        *content*-frame crop tool specifically; this is a distinct,
        explicit action). Re-runs all three exports
        (`CaptureSession.rotate_reviewed_image`), so — like Confirm — this
        is a real (if short) wait, not routed through the silent auto-
        confirm path. Not on the undo/redo stack (`Ctrl+Z`/`Ctrl+Y`): that
        stack only ever holds `PositiveOverride` snapshots (tonal/crop),
        and rotation lives in the journal instead, same as during capture."""
        session = self._session
        name = self._current_name
        if session is None or name is None or self._busy:
            return
        if self._print_engine_loaded_for != name:
            return  # nothing genuine on screen yet for this image

        def _rotate() -> None:
            session.rotate_reviewed_image(name, direction=direction)

        def _on_rotated(_result: object) -> None:
            # The journal/linear-decode caches now hold this image's *old*
            # rotation — dropped so the reload below picks up the new one
            # instead of silently redisplaying the pre-rotation pixels.
            self._journal_entries_cache = None
            self._linear_cache.pop(name, None)
            self._print_engine_loaded_for = None
            self._pending_thumbnail_refresh[name] = _THUMBNAIL_REFRESH_ATTEMPTS
            self._load_print_engine(name)

        self._run_async(_rotate, _on_rotated, busy_text=t("positive_review.rotating", name=name))

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

        `session.regenerate_positive` only submits the real render
        (~16.7s+, RAW decode + density math) to the background export
        queue and returns — never waited out here (`_poll_export_progress`
        drains it later, same as `gui.screens.capture`'s own pump timer),
        so Enter/next-image navigation stays fast regardless of how long
        the actual render takes. Still routed through `_run_async`
        (`on_done` lets `apply_to_selection` chain its own propagation step
        after this completes)."""
        session = self._session
        row = self.list_widget.currentRow()
        if session is None or self._current_name is None or row < 0 or self._busy:
            return
        if self._print_engine_loaded_for != self._current_name:
            return  # nothing genuine on screen yet for this image
        self._confirm_debounce_timer.stop()
        self._confirm_pending = False
        name = self._names[row]
        next_name = self._names[row + 1] if row + 1 < len(self._names) else None
        before = _snapshot(session.paths, session.fs, [name])

        self._persist_print_overrides_for_current(name)

        def _regenerate() -> None:
            session.regenerate_positive(name)

        def _on_regenerated(_result: object) -> None:
            self._finish_confirm(name, next_name, before, row)
            if on_done is not None:
                on_done()

        self._run_async(
            _regenerate, _on_regenerated, busy_text=t("positive_review.regenerating", name=name)
        )

    def _finish_confirm(
        self,
        name: str,
        next_name: str | None,
        before: dict[str, PositiveOverride | None],
        row: int,
    ) -> None:
        self._record_confirm(name, before, row)
        self._current_name = None
        # `_remove_stale_entries_and_select`, not a full `refresh_list()`:
        # this confirm touched exactly one image (`name`) — no need to
        # re-read and rescale every other, unrelated thumbnail in the list
        # on every single Confirm.
        self._remove_stale_entries_and_select(next_name)

    def apply_to_selection(self) -> None:
        """Copies the current image's print_engine overrides to every other
        selected image. Confirms the current image first (so what's
        propagated is exactly what's on screen), then explicit confirmation
        ("N images affected") before touching anything else. Both steps are
        `_run_async` calls chained via `confirm_current`'s `on_done`."""
        session = self._session
        if (
            session is None
            or self._current_name is None
            or self._busy
            or self._print_engine_loaded_for != self._current_name
        ):
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
                # Submits every target's regenerate and returns — never
                # waits for them to finish (`_poll_export_progress`'s own
                # docstring).
                session.propagate_print_overrides(
                    source_name, targets, include_dmin=self.include_dmin_checkbox.isChecked()
                )

            def _on_propagated(_result: object) -> None:
                after = _snapshot(session.paths, session.fs, targets)
                self._push_undo(_UndoCommand("propagate", tuple(targets), before, after))
                self.status_label.setText(t("positive_review.propagation_done", count=len(targets)))
                self.refresh_list(select_name=source_name)
                for target in targets:
                    self._pending_thumbnail_refresh[target] = _THUMBNAIL_REFRESH_ATTEMPTS

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
            # Submits each name's regenerate and returns — never waits for
            # them to finish (`_poll_export_progress`'s own docstring).
            for name in names:
                session.restore_positive_override(name, snapshots.get(name))

        def _on_restored(result: object) -> None:
            for name in names:
                self._pending_thumbnail_refresh[name] = _THUMBNAIL_REFRESH_ATTEMPTS
            on_done(result)

        self._run_async(_restore, _on_restored, busy_text=t("positive_review.undo"))

    # --- misc ------------------------------------------------------------------

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Installed on every descendant widget except a `SliderField`'s own
        # `QLineEdit` (see `__init__`'s own installation loop) — not just
        # `list_widget`: any focusable child (a button, a checkbox, a
        # splitter handle after a drag-to-resize...) otherwise leaves the
        # reserved shortcuts below (arrows + modifiers, chiefly — operator-
        # reported: they stopped reaching the crop nudge whenever focus had
        # moved off the list) unreachable for as long as it holds focus.
        #
        # A key event actually bound for a focused `QLineEdit` still reaches
        # *this* filter first — Qt walks a focused widget's whole ancestor
        # chain for key events (the same mechanism that lets an action's
        # shortcut pre-empt a plain widget's own key handling), calling an
        # ancestor's installed filter before the focused widget's own
        # `keyPressEvent` ever runs, regardless of which ancestor the event
        # filter happens to be installed on — excluding the `QLineEdit`
        # itself from installation (above) is therefore not enough on its
        # own. Checking the actual focus widget here, not just `watched`,
        # is what actually lets that field keep Left/Right for its own text
        # cursor.
        if event.type() == QEvent.Type.KeyPress and not isinstance(self.focusWidget(), QLineEdit):
            key_event = event
            assert isinstance(key_event, QKeyEvent)
            self.keyPressEvent(key_event)
            if key_event.isAccepted():
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            if self.preview_area.is_picking:
                self._cancel_dmin_picking()
                event.accept()
                return
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
        if event.key() == Qt.Key.Key_Space:
            # Next image, without confirming — the same "keep moving
            # forward with one hand" muscle memory as Capture's own Space,
            # even though that key does something unrelated there (remote
            # shutter). Ignored while the *previous* move's image hasn't
            # actually reached the screen yet (operator-reported): Space is
            # the key an impatient operator leans on hardest, and a second
            # press fired before visible confirmation of the first must not
            # silently skip an image.
            if self._print_engine_loaded_for == self._current_name:
                self._move(1)
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
        # Up/Down/Left/Right (reserved, always active — operator-reported:
        # used to browse images here, confusing since the same keys move
        # the crop in Capture): nudge the content-frame crop instead, same
        # step/Shift convention as `gui.screens.capture._nudge_frame`'s own
        # support-frame nudge. Click, Page Up/Page Down and Enter cover
        # browsing now, so none of the four arrows are needed for that here.
        if event.key() in (
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
        ) and not bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            step = 10 if bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 1
            if event.key() == Qt.Key.Key_Up:
                self._nudge_print_frame(dy=-step)
            elif event.key() == Qt.Key.Key_Down:
                self._nudge_print_frame(dy=step)
            elif event.key() == Qt.Key.Key_Left:
                self._nudge_print_frame(dx=-step)
            else:
                self._nudge_print_frame(dx=step)
            event.accept()
            return
        # V/Shift+V: the support frame's own 90° orientation, same key and
        # clockwise/counter-clockwise convention as Capture's rotate
        # shortcut — for a rotation missed there, caught only once judging
        # tone here. Distinct from Ctrl+Left/Right below (the content-frame
        # crop's fine deskew).
        if event.key() == Qt.Key.Key_V and not bool(
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._rotate_current_image(direction=-1 if shift else 1)
            event.accept()
            return
        # Ctrl+Left/Right: deskew the content-frame crop — same reserved,
        # always-active convention `gui.screens.capture` uses for the
        # support frame's own rotation. Checked ahead of the plain
        # Left/Right nudge above only in the modifier condition, not in
        # ordering: both branches are mutually exclusive on Ctrl.
        if bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier) and event.key() in (
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
        ):
            shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            step_deg = 1.0 if shift else 0.1
            self._rotate_print_frame(step_deg if event.key() == Qt.Key.Key_Right else -step_deg)
            event.accept()
            return
        super().keyPressEvent(event)

    def _move(self, delta: int) -> None:
        """Browsing to another image is never blocked by `_busy` — that
        flag only ever covers a "commit" op (confirm/regenerate, apply to
        selection, undo/redo), never the print_engine decode itself (always
        silent, see `_load_print_engine`). Left in place mainly so a
        keyboard nudge can't slip a list-widget navigation past a genuine
        commit still in flight (`list_widget` itself is mouse-disabled by
        `_set_busy` for the same reason; this was the one path around it —
        `setCurrentRow` below is a programmatic call, which Qt still
        delivers even to a disabled widget)."""
        if self._busy or not self._names:
            return
        row = max(0, min(len(self._names) - 1, self.list_widget.currentRow() + delta))
        self.list_widget.setCurrentRow(row)

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


def _print_frame_from_rect(rect: tuple[int, int, int, int], angle_deg: float = 0.0) -> FrameResult:
    """Wraps a print_engine content-frame rect (already in the preview's
    own display coordinate space — `PrintResult.content_frame` from a
    `crop_to_content=False` render) as a draggable `FrameResult` overlay
    (always shown as "reliable" — the frame's own correctness here is the
    operator's judgment, not a detector score). `angle_deg` is the same
    already-flipped-space angle `PrintResult.content_frame_angle_deg`
    carries alongside it — `0.0` for an automatic detection, which never
    rotates."""
    x, y, w, h = rect
    return FrameResult(
        x=x,
        y=y,
        width=w,
        height=h,
        angle_deg=angle_deg,
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


def _print_frame_to_angle_deg(frame: FrameResult, horizontal_flip: bool) -> float:
    """Companion to `_print_frame_to_fraction`, for the crop's own deskew
    angle — same mirrored-on-flip convention `imaging.print_engine.
    render_print_from_linear(crop_to_content=False)` applies to its own
    returned `content_frame_angle_deg`."""
    return -frame.angle_deg if horizontal_flip else frame.angle_deg


def _load_master_jpeg(paths: CampaignPaths, name: str) -> np.ndarray | None:
    path = Path(paths.jpeg_master_dir) / f"{name}.jpg"
    try:
        with Image.open(path) as image:
            return np.array(image.convert("RGB"))
    except OSError:
        return None
