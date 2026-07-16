"""Capture screen.

Drives a `core.session.CaptureSession` through a `QTimer` loop (polling
mode). Preview extraction runs in a `PreviewWorker` (dedicated thread) to
never block the Qt thread.

Base shortcuts are centralized in `keyPressEvent` — a single handler,
not scattered `QShortcut` instances — so the context-priority rule
(text field > capture) stays verifiable in one place.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import shiboken6
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QCompleter,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from scanassistant.core.crash_recovery import RecoveryReport, perform_crash_recovery
from scanassistant.core.errors import IllegalTransitionError
from scanassistant.core.events import (
    CriticalError,
    CriticalResolved,
    ImageDetected,
    ImageErrored,
    ImageRejected,
    ImageStabilized,
    ImageStateChanged,
    NameConflictDetected,
    SessionEvent,
    StabilizationTimedOut,
)
from scanassistant.core.events import Warning as WarningEvent
from scanassistant.core.export_runner import MasterExportRunner
from scanassistant.core.fs import FileSystem
from scanassistant.core.queue import ExportExecutor, ExportRunner, InlineExportExecutor
from scanassistant.core.session import CaptureSession, SessionHistoryEntry
from scanassistant.gui.errors import format_critical, format_warning
from scanassistant.gui.preview_worker import PreviewResult, PreviewWorker
from scanassistant.gui.shortcuts import (
    CAPTURE,
    NAME_CONFLICT,
    matches,
    matches_shifted,
    merge_with_defaults,
)
from scanassistant.gui.widgets.preview_area import PreviewArea
from scanassistant.i18n import t
from scanassistant.imaging.framing import (
    IMPOSSIBLE,
    ConfidenceComponents,
    FrameResult,
)
from scanassistant.imaging.geometry import FrameGeometry, apply_geometry
from scanassistant.imaging.positive import ManualSettings, render_positive
from scanassistant.imaging.raw import RawDecoder, RawpyDecoder
from scanassistant.journal.journal import Journal
from scanassistant.metadata.writer import ExifToolMetadataWriter
from scanassistant.metadata.writer import is_available as is_exiftool_available
from scanassistant.project.campaign import Campaign
from scanassistant.project.inventory import MAX_NAME_LENGTH, STATUS_COLUMN, Inventory
from scanassistant.project.layout import CampaignPaths
from scanassistant.project.state import FramingState, ProjectState
from scanassistant.watcher.monitor import FolderMonitor
from scanassistant.watcher.stability import poll_interval_s

_STATUS_MESSAGE_DURATION_MS = 5000
_MIN_PUMP_INTERVAL_MS = 100
_ROTATION_COMMIT_DELAY_MS = 2500
_FRAME_COMMIT_DELAY_MS = 2500
# Detection (rescue in particular) is capped well under this on real hardware;
# bounded so quitting can never hang indefinitely even if it isn't.
_PREVIEW_WORKER_SHUTDOWN_TIMEOUT_MS = 2000

_LEVEL_LABELS = {
    "reliable": ("capture.confidence_reliable", "ok"),
    "review": ("capture.confidence_review", "warning"),
    "impossible": ("capture.confidence_impossible", "critical"),
    # "manual": GUI-only sentinel (a nudge, resize, rotate, or drag on the
    # crop), not a possible output of `imaging.framing.classify()`.
    "manual": ("capture.confidence_manual", "ok"),
}


def _set_role(widget: QWidget, role: str) -> None:
    widget.setProperty("role", role)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def _to_reference_space(frame: FrameResult, scale_factor: float) -> FrameResult:
    """Converts a frame from preview space to full-resolution reference space.

    `detect_frame()` and edit mode both work in the displayed preview's
    coordinate space (overlay, keyboard moves); only the boundary with
    `session.apply_frame()` (full-resolution coordinates, consumed by
    `imaging.geometry`) needs to convert via `Preview.scale_factor` —
    identity if the preview is already full resolution.
    """
    if scale_factor == 1.0:
        return frame
    return replace(
        frame,
        x=round(frame.x * scale_factor),
        y=round(frame.y * scale_factor),
        width=round(frame.width * scale_factor),
        height=round(frame.height * scale_factor),
    )


def _rotated_for_display(
    pixels: np.ndarray, frame: FrameResult | None, rotation_deg: int
) -> tuple[np.ndarray, FrameResult | None]:
    """Applies the image's stored `rotation_deg` (V key, cycles 0/90/180/270°)
    to the plain negative view — display only, `pixels`/`frame` themselves
    (canonical, reference-space) are untouched. `angle_deg` (deskew) is left
    as-is: it describes a tilt relative to whatever's currently "up", which a
    90°-multiple rotation doesn't change.
    """
    times = (rotation_deg // 90) % 4
    if times == 0:
        return pixels, frame
    rotated_pixels = pixels
    rotated_frame = frame
    for _ in range(times):
        image_height = rotated_pixels.shape[0]
        rotated_pixels = np.ascontiguousarray(np.rot90(rotated_pixels, k=-1))
        if rotated_frame is not None:
            rotated_frame = replace(
                rotated_frame,
                x=image_height - rotated_frame.y - rotated_frame.height,
                y=rotated_frame.x,
                width=rotated_frame.height,
                height=rotated_frame.width,
            )
    return rotated_pixels, rotated_frame


_FAST_PREVIEW_MAX_DIM = 480


def _downscaled_for_fast_preview(
    pixels: np.ndarray, frame: FrameResult | None
) -> tuple[np.ndarray, FrameResult | None]:
    """A much smaller copy of `pixels` (long edge ~480px) for the positive/
    master preview while a Positive-settings slider is being dragged: the
    tone-curve math (`imaging.positive.render_positive`) runs on every
    mouse-move and is expensive enough at full preview resolution to
    visibly stutter. `frame`'s coordinates are scaled down to match — it
    must stay in the same space as the pixels it's cropping."""
    height, width = pixels.shape[:2]
    scale = _FAST_PREVIEW_MAX_DIM / max(height, width)
    if scale >= 1.0:
        return pixels, frame
    resized = cv2.resize(
        pixels, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA
    )
    if frame is None:
        return resized, None
    scaled_frame = replace(
        frame,
        x=frame.x * scale,
        y=frame.y * scale,
        width=frame.width * scale,
        height=frame.height * scale,
    )
    return resized, scaled_frame


class CaptureScreen(QWidget):
    stopped = Signal()
    queue_changed = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        decoder: RawDecoder | None = None,
        export_runner: ExportRunner | None = None,
        export_executor: ExportExecutor | None = None,
        shortcuts: dict[str, dict[str, str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.session: CaptureSession | None = None
        self._shortcuts = merge_with_defaults(shortcuts or {})
        self._decoder = decoder or RawpyDecoder()
        self._export_runner_override = export_runner
        # Default stays `InlineExportExecutor` (synchronous, deterministic —
        # what every test relies on): only `gui.main_window`'s real,
        # user-facing instantiation passes a `ThreadedExportExecutor`, so a
        # slow export never freezes the Qt thread (DECISIONS.md I-92/I-98).
        self._export_executor_override = export_executor
        self._preview_worker: PreviewWorker | None = None
        self._stabilizing: set[Path] = set()
        self._loaded_preview_for: str | None = None
        self._current_frame_result: FrameResult | None = None
        self._current_preview_pixels: np.ndarray | None = None
        self._current_preview_scale_factor: float = 1.0
        self._positive_preview_active = False
        self._master_preview_active = False
        self._pending_conflict: NameConflictDetected | None = None

        self.preview_area = PreviewArea()
        self.preview_area.frame_dragged.connect(self._on_frame_dragged)
        self.preview_area.frame_drag_finished.connect(self._on_frame_drag_finished)

        # Stage header: name + confidence, in a bar of its own directly
        # above the preview — never drawn on top of the image itself, only
        # the frame overlay is (it has to be, it shows where the crop is).
        self.name_label = QLabel()
        self.name_label.setStyleSheet("font-size: 28pt; font-weight: bold;")
        self.confidence_label = QLabel()
        stage_header = QHBoxLayout()
        stage_header.addWidget(self.name_label)
        stage_header.addSpacing(16)
        stage_header.addWidget(self.confidence_label)
        stage_header.addStretch(1)
        self.stage_header_widget = QWidget()
        self.stage_header_widget.setProperty("role", "stage-header")
        self.stage_header_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.stage_header_widget.setLayout(stage_header)

        # Console: status message, then next/queue/progress, then mode —
        # everything that isn't about the current image specifically.
        self.progress_label = QLabel()
        self.progress_label.setProperty("role", "secondary")
        self.next_label = QLabel()
        self.next_label.setProperty("role", "secondary")
        self.queue_label = QLabel()
        self.queue_label.setProperty("role", "secondary")

        self.go_to_name_edit = QLineEdit()
        self.go_to_name_edit.setVisible(False)
        self.go_to_name_edit.setPlaceholderText(t("capture.go_to_name_placeholder"))
        self.go_to_name_edit.returnPressed.connect(self._submit_go_to_name)

        # Name conflict panel: inline, never a popup — 1/Rename,
        # 2/Replace (button, explicit confirmation), 3/Rename existing;
        # Escape = option 1 with an empty field.
        self.conflict_label = QLabel()
        self.conflict_option1_edit = QLineEdit()
        self.conflict_option1_edit.returnPressed.connect(
            lambda: self._resolve_conflict(1, self.conflict_option1_edit.text().strip())
        )
        self.conflict_use_next_free_button = QPushButton(t("capture.conflict_use_next_free"))
        self.conflict_use_next_free_button.clicked.connect(self._use_next_free_name)
        self.conflict_replace_button = QPushButton(t("capture.conflict_option2"))
        self.conflict_replace_button.clicked.connect(lambda: self._resolve_conflict(2))
        self.conflict_option3_edit = QLineEdit()
        self.conflict_option3_edit.returnPressed.connect(
            lambda: self._resolve_conflict(3, self.conflict_option3_edit.text().strip())
        )
        conflict_row1 = QHBoxLayout()
        conflict_row1.addWidget(QLabel(t("capture.conflict_option1")))
        conflict_row1.addWidget(self.conflict_option1_edit, 1)
        conflict_row1.addWidget(self.conflict_use_next_free_button)
        conflict_row2 = QHBoxLayout()
        conflict_row2.addWidget(self.conflict_replace_button)
        conflict_row2.addStretch(1)
        conflict_row3 = QHBoxLayout()
        conflict_row3.addWidget(QLabel(t("capture.conflict_option3")))
        conflict_row3.addWidget(self.conflict_option3_edit, 1)
        conflict_layout = QVBoxLayout()
        conflict_layout.addWidget(self.conflict_label)
        conflict_layout.addLayout(conflict_row1)
        conflict_layout.addLayout(conflict_row2)
        conflict_layout.addLayout(conflict_row3)
        self.conflict_panel = QWidget()
        self.conflict_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.conflict_panel.setProperty("role", "critical-banner")
        self.conflict_panel.setLayout(conflict_layout)
        self.conflict_panel.setVisible(False)

        # Persistent banners: warning (yellow, clickable) and critical (red,
        # "Resume processing" button) — distinct from the status line
        # (transient, 5 s).
        self.warning_banner = QPushButton()
        self.warning_banner.setFlat(True)
        self.warning_banner.setProperty("role", "warning-banner")
        self.warning_banner.clicked.connect(self._show_warning_detail)
        self.warning_banner_close = QPushButton("×")
        self.warning_banner_close.setFlat(True)
        self.warning_banner_close.setProperty("role", "warning-banner-close")
        self.warning_banner_close.setToolTip(t("capture.dismiss_warning"))
        self.warning_banner_close.setFixedWidth(28)
        self.warning_banner_close.clicked.connect(self._hide_warning_banner)
        warning_row = QHBoxLayout()
        warning_row.setContentsMargins(0, 0, 0, 0)
        warning_row.setSpacing(0)
        warning_row.addWidget(self.warning_banner, 1)
        warning_row.addWidget(self.warning_banner_close)
        self.warning_banner_widget = QWidget()
        self.warning_banner_widget.setLayout(warning_row)
        self.warning_banner_widget.setVisible(False)
        self._last_warning: tuple[str, dict[str, object]] | None = None

        self.critical_banner_label = QLabel()
        self.resume_button = QPushButton(t("capture.resume_processing"))
        self.resume_button.setDefault(True)  # Enter triggers it when the banner has focus
        self.resume_button.clicked.connect(self._on_resume_processing)
        critical_row = QHBoxLayout()
        critical_row.addWidget(self.critical_banner_label, 1)
        critical_row.addWidget(self.resume_button)
        self.critical_banner = QWidget()
        self.critical_banner.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.critical_banner.setProperty("role", "critical-banner")
        self.critical_banner.setLayout(critical_row)
        self.critical_banner.setVisible(False)

        self.status_label = QLabel()
        self.status_label.setProperty("role", "secondary")
        self.mode_label = QLabel()
        # Two rows: the status message gets the full width to itself — the
        # instructional text shown during frame edit is long enough that it
        # would otherwise crowd into next/queue/progress on the same line.
        console_status_row = QHBoxLayout()
        console_status_row.addWidget(self.status_label, 1)
        console_info_row = QHBoxLayout()
        console_info_row.addWidget(self.next_label)
        console_info_row.addSpacing(16)
        console_info_row.addWidget(self.queue_label)
        console_info_row.addSpacing(16)
        console_info_row.addWidget(self.progress_label)
        console_info_row.addStretch(1)
        console_info_row.addWidget(self.mode_label)
        console_layout = QVBoxLayout()
        console_layout.addLayout(console_status_row)
        console_layout.addLayout(console_info_row)
        self.console_widget = QWidget()
        self.console_widget.setProperty("role", "console")
        self.console_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.console_widget.setLayout(console_layout)

        layout = QVBoxLayout(self)
        layout.addWidget(self.stage_header_widget)
        layout.addWidget(self.preview_area, 1)
        layout.addWidget(self.go_to_name_edit)
        layout.addWidget(self.conflict_panel)
        layout.addWidget(self.warning_banner_widget)
        layout.addWidget(self.critical_banner)
        layout.addWidget(self.console_widget)

        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self.status_label.setText(""))

        self._pump_timer = QTimer(self)
        self._pump_timer.timeout.connect(self._pump)

        # V/Shift+V only updates the display immediately; the actual
        # cancel-and-re-export (`session.set_rotation`) is debounced so that
        # rotating several times in a row doesn't re-decode the RAW and
        # re-export once per intermediate press.
        self._pending_rotation_deg: int | None = None
        self._rotation_commit_timer = QTimer(self)
        self._rotation_commit_timer.setSingleShot(True)
        self._rotation_commit_timer.timeout.connect(self._commit_pending_rotation)

        # Crop nudge/resize/rotate (keyboard) and drag (mouse) only update
        # the display immediately; the actual `session.apply_frame()` call
        # is debounced the same way — see `_commit_pending_frame`. A mouse
        # release commits immediately instead of waiting for the timer.
        self._frame_commit_pending = False
        self._frame_commit_timer = QTimer(self)
        self._frame_commit_timer.setSingleShot(True)
        self._frame_commit_timer.timeout.connect(self._commit_pending_frame)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_shortcuts(self, shortcuts: dict[str, dict[str, str]]) -> None:
        """Applies a new shortcut map (Preferences ▸ Shortcuts) without restarting capture."""
        self._shortcuts = merge_with_defaults(shortcuts)

    # --- lifecycle ---------------------------------------------------------

    def start(
        self,
        *,
        campaign: Campaign,
        state: ProjectState,
        inventory: Inventory,
        paths: CampaignPaths,
        journal: Journal,
        fs: FileSystem,
        exiftool_executable: str = "",
        was_stale: bool = False,
        disk_warn_gb: float = 10.0,
        disk_critical_gb: float = 2.0,
        max_name_length: int = MAX_NAME_LENGTH,
        export_queue_warn_threshold: int = 20,
    ) -> None:
        monitor = FolderMonitor(
            Path(campaign.capture.watched_folder),
            campaign.capture.extensions,
            watch_mode="polling",
            stabilization_delay_s=campaign.capture.stabilization_delay_s,
            stabilization_timeout_s=campaign.capture.stabilization_timeout_s,
            extra_ignored_suffixes=tuple(campaign.capture.extra_ignored_suffixes),
        )
        export_runner = self._export_runner_override or MasterExportRunner(
            decoder=self._decoder,
            campaign=campaign,
            paths=paths,
            metadata_writer=ExifToolMetadataWriter(executable=exiftool_executable),
            journal=journal,
        )
        self.session = CaptureSession(
            paths=paths,
            campaign=campaign,
            inventory=inventory,
            state=state,
            journal=journal,
            fs=fs,
            monitor=monitor,
            export_runner=export_runner,
            export_executor=self._export_executor_override or InlineExportExecutor(),
            disk_warn_gb=disk_warn_gb,
            disk_critical_gb=disk_critical_gb,
            max_name_length=max_name_length,
            export_queue_warn_threshold=export_queue_warn_threshold,
        )
        if was_stale:
            report = perform_crash_recovery(self.session)
            self._show_recovery_report(report)
        if not is_exiftool_available(exiftool_executable):
            # Persistent warning: exports will be produced without metadata.
            self._show_warning_banner("A-01", {"message": t("metadata.exiftool_unavailable")})
        self._dispatch(self.session.initial_scan())
        self._stabilizing.clear()
        self._loaded_preview_for = None
        self._refresh_banner()
        self._refresh_preview_state()

        interval_ms = max(
            _MIN_PUMP_INTERVAL_MS,
            int(poll_interval_s(campaign.capture.stabilization_delay_s) * 1000),
        )
        self._pump_timer.start(interval_ms)
        self.setFocus()

    def stop(self, *, wait_for_exports: bool = True) -> None:
        self._pump_timer.stop()
        self._flush_pending_edits()  # never leave a rotation/crop edit un-exported
        if self.session is not None:
            self._dispatch(self.session.stop(wait_for_exports=wait_for_exports))
            if wait_for_exports:
                self.session.export_executor.shutdown()  # releases the background thread, if any
            # else: abandoned deliberately (Quit without waiting) — the
            # daemon thread dies with the process, whatever it was still
            # writing is safely re-run from the untouched RAW next launch.
        self.session = None
        self._pending_conflict = None
        self.conflict_panel.setVisible(False)

    # --- non-blocking shutdown (drain_on_exit) ----------------------------------

    def begin_shutdown(self) -> int:
        """First step of closing the app while a capture session is open.

        Stops monitoring the watched folder, finalizes the current image,
        and submits the whole export backlog without blocking. The session
        is kept alive (unlike `stop()`) so the caller can either wait it out
        (`poll_export_progress`) or abandon it (`finish_shutdown`).
        Returns the number of exports still pending right after submission.
        """
        self._pump_timer.stop()
        self._flush_pending_edits()  # never leave a rotation/crop edit un-exported
        if self.session is None:
            return 0
        self._dispatch(self.session.stop(wait_for_exports=False))
        return len(self.session.export_queue)

    def poll_export_progress(self) -> int:
        """Call periodically after `begin_shutdown()`. Returns the number of
        exports still pending; 0 once it is safe to actually close."""
        if self.session is None:
            return 0
        self._dispatch(self.session.collect_export_progress())
        return len(self.session.export_queue)

    def finish_shutdown(self, *, wait_for_exports: bool) -> None:
        """Second step, after `begin_shutdown()`. `wait_for_exports=True` only
        once `poll_export_progress()` has reached 0 (never blocks then);
        `False` abandons whatever is still in flight (Quit without waiting)."""
        # A still-running detection thread must never be abandoned here: if
        # nothing keeps it referenced until it actually finishes, Qt aborts
        # the whole process rather than raising a catchable error. Bounded,
        # not indefinite — this only fires at the moment of quitting, not
        # during capture.
        if self._preview_worker is not None:
            self._preview_worker.wait(_PREVIEW_WORKER_SHUTDOWN_TIMEOUT_MS)
        if self.session is not None:
            if wait_for_exports:
                self.session.export_executor.shutdown()
            self.session = None
        self._pending_conflict = None
        self.conflict_panel.setVisible(False)

    # --- loop ----------------------------------------------------------------

    def _pump(self) -> None:
        if self.session is None:
            return
        # A newly-arrived, stabilized file can implicitly validate whatever
        # is still `current` inside this very `pump()` call — a rotation or
        # crop edit still only debounced in this screen's own state (not yet
        # committed to `session`) would then export with the stale,
        # pre-edit value. `before_finalize_current` flushes it, but only on
        # the (rare) tick where that's actually about to happen: flushing
        # unconditionally on every tick — this runs every ~100 ms — would
        # cut the debounce down to that same ~100 ms and defeat it entirely.
        self._dispatch(
            self.session.pump(time.monotonic(), before_finalize_current=self._flush_pending_edits)
        )
        self._refresh_banner()
        self._refresh_preview_state()
        self.queue_changed.emit()

    def _flush_pending_edits(self) -> None:
        self._commit_pending_rotation()
        self._commit_pending_frame()

    def _dispatch(self, events: list[SessionEvent]) -> None:
        """Routes session-returned events to the same handler `_pump()` uses.

        Every `CaptureSession` action called directly from this screen
        (reject, validate, rotate, resolve conflict, resume, …) can — like
        `pump()` — surface a conflict panel or a critical/warning banner as
        a *side effect* of what it does internally (e.g. draining a paused
        queue). Those events must reach `_handle_event` exactly like the
        ones from the polling loop, or they're silently lost.
        """
        for event in events:
            self._handle_event(event)

    def _handle_event(self, event: object) -> None:
        if isinstance(event, ImageDetected):
            self._stabilizing.add(event.path)
        elif isinstance(event, ImageStabilized | StabilizationTimedOut):
            self._stabilizing.discard(event.path)
        elif isinstance(event, ImageRejected):
            self._set_status(t("capture.status_rejected", name=event.name))
        elif isinstance(event, NameConflictDetected):
            self._set_status(t("capture.status_conflict", name=event.name))
            self._show_conflict_panel(event)
        elif isinstance(event, CriticalError):
            self._set_status(format_critical(event.code, event.details))
            self._show_critical_banner(event.code, event.details)
        elif isinstance(event, CriticalResolved):
            self._hide_critical_banner()
        elif isinstance(event, WarningEvent):
            self._set_status(format_warning(event.code, event.details))
            self._show_warning_banner(event.code, event.details)
        elif isinstance(event, ImageErrored):
            self._set_status(t("capture.status_image_errored", name=event.name, code=event.code))
        elif isinstance(event, ImageStateChanged) and event.new == "COMPLETED":
            self._set_status(t("capture.status_export_done", name=event.name))

    # --- persistent banners ----------------------------------------------------

    def _show_warning_banner(self, code: str, details: dict[str, object]) -> None:
        """Warning (yellow): persistent until the operator dismisses it (×) or
        acts on it, clickable elsewhere on the row for detail.

        Only one banner shown at a time (most recent warning) rather than a
        stack per code: real warnings are rare, a full queue would be
        unwarranted complexity here. Some warnings (e.g. E-15, a growing
        export queue) can legitimately stay relevant for a while — dismissal
        is a deliberate operator action, never an automatic timeout, so it's
        never missed and never stuck either.
        """
        self._last_warning = (code, details)
        self.warning_banner.setText(format_warning(code, details))
        self.warning_banner_widget.setVisible(True)

    def _hide_warning_banner(self) -> None:
        self.warning_banner_widget.setVisible(False)

    def _show_warning_detail(self) -> None:
        if self._last_warning is None:
            return
        code, details = self._last_warning
        QMessageBox.information(self, code, format_warning(code, details))

    def _show_critical_banner(self, code: str, details: dict[str, object] | None = None) -> None:
        self.critical_banner_label.setText(format_critical(code, details or {}))
        self.critical_banner.setVisible(True)
        self.resume_button.setFocus()

    def _hide_critical_banner(self) -> None:
        self.critical_banner.setVisible(False)

    def _on_resume_processing(self) -> None:
        """Resume-processing button — Enter also triggers it (default button)."""
        session = self.session
        if session is None:
            return
        self._dispatch(session.resume_from_critical())
        self._hide_critical_banner()
        self._refresh_banner()
        self._refresh_preview_state()

    def _show_recovery_report(self, report: RecoveryReport) -> None:
        """Recovery panel shown after an unclean shutdown."""
        if report.is_empty:
            return
        QMessageBox.information(
            self, t("capture.recovery_report_title"), "\n".join(report.summary_lines())
        )

    # --- banner + preview ------------------------------------------------------

    def _refresh_banner(self) -> None:
        session = self.session
        if session is None:
            return
        current = session.state.current_image
        self.name_label.setText(current.assigned_name if current else "")

        total = len(session.inventory.rows)
        done = sum(1 for row in session.inventory.rows if row[STATUS_COLUMN] == "done")
        pct = round(100 * done / total) if total else 0
        self.progress_label.setText(t("capture.progress", done=done, total=total, pct=pct))

        next_name = session.inventory.current_name()
        self.next_label.setText(t("capture.next", name=next_name) if next_name else "")
        self.queue_label.setText(t("capture.queue", count=len(session.export_queue)))

        self._update_confidence_label()
        self._set_mode(session.paused)

    def _update_confidence_label(self) -> None:
        frame = self._current_frame_result
        if frame is None:
            self.confidence_label.setText("")
            return
        key, role = _LEVEL_LABELS[frame.level]
        self.confidence_label.setText(t(key, score=f"{frame.confidence:.2f}"))
        _set_role(self.confidence_label, role)

    def _refresh_preview_state(self) -> None:
        session = self.session
        if session is None:
            return
        current = session.state.current_image
        if current is not None:
            if self._loaded_preview_for != current.assigned_name:
                self._load_preview(current.assigned_name, current.extension)
            return

        self._loaded_preview_for = None
        self._current_frame_result = None
        if self._stabilizing:
            name = next(iter(self._stabilizing)).name
            self.preview_area.show_stabilizing(name)
        else:
            self.preview_area.show_waiting(session.inventory.current_name() or "")

    def _load_preview(self, name: str, extension: str, *, journal_action: str = "auto") -> None:
        assert self.session is not None
        # Defense in depth: finalize/reject already flush a pending rotation
        # or crop edit before the cursor moves on, but never let one linger
        # onto a different image's export regardless of how we got here.
        self._flush_pending_edits()
        # Each new image starts back in plain negative view — positive/master
        # is a per-image check, not a standing preference to carry forward.
        self._positive_preview_active = False
        self._master_preview_active = False
        self._loaded_preview_for = name
        self._current_frame_result = None
        self._update_confidence_label()
        raw_path = self.session.paths.raw_dir / f"{name}{extension}"

        # No parent widget: detection can take over a second (rescue on an
        # unreadable negative), and Qt aborts the process outright if a
        # QThread is destroyed while still running. Parenting it to this
        # screen would let a window close mid-detection do exactly that.
        worker = PreviewWorker(raw_path, self._decoder, self.session.campaign.framing)
        # Target name is bound into the connection closure, not re-read from
        # `self._loaded_preview_for` when the signal fires: if another image
        # was already loaded in the meantime, that field would have changed
        # and the (stale) result would otherwise land on the wrong image.
        worker.succeeded.connect(
            lambda result, n=name, ja=journal_action: self._on_preview_ready(result, n, ja)
        )
        worker.failed.connect(self._on_preview_failed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(lambda w=worker: self._clear_preview_worker(w))
        self._preview_worker = worker
        worker.start()

    def _clear_preview_worker(self, worker: PreviewWorker) -> None:
        # `deleteLater` (connected alongside this) only runs once the event
        # loop gets to it — `self._preview_worker` must stop pointing at the
        # worker the instant it actually finishes, not after, or a shutdown
        # check landing in between would touch an already-deleted C++ object.
        if self._preview_worker is worker:
            self._preview_worker = None

    def _on_preview_ready(self, result: PreviewResult, name: str, journal_action: str) -> None:
        if not shiboken6.isValid(self):
            # This screen was torn down (e.g. app closed) before detection —
            # rescue in particular can run a second or more — finished; the
            # worker outlives it by design (never destroyed while running),
            # so its result can still arrive after there's nothing to show it on.
            return
        session = self.session
        current = session.state.current_image if session is not None else None
        if current is None or current.assigned_name != name:
            return  # image already changed (validated/rejected) before extraction finished
        self._current_frame_result = result.frame
        self._current_preview_pixels = result.preview.pixels
        self._current_preview_scale_factor = result.preview.scale_factor
        self._display_current_preview()
        self._update_confidence_label()
        if result.frame is not None:
            self._apply_frame_result(name, journal_action, result.frame, rescued=result.rescued)

    def _on_preview_failed(self, error: str) -> None:
        if not shiboken6.isValid(self):
            return  # see `_on_preview_ready`
        self.preview_area.show_message(t("capture.preview_unavailable", error=error))

    def _load_preview_known_frame(self, name: str, extension: str, framing: FramingState) -> None:
        """Loads the preview for a reopened image (history panel) without re-detecting.

        Unlike `_load_preview`, the frame is already known (session history)
        and must not be replaced by a fresh, possibly different detection.
        """
        assert self.session is not None
        self._loaded_preview_for = name
        self._current_frame_result = None
        self._update_confidence_label()
        raw_path = self.session.paths.raw_dir / f"{name}{extension}"

        # No parent widget: same reasoning as `_load_preview` — a QThread
        # destroyed mid-run (window closing while this is still reading the
        # RAW file) aborts the process rather than raising a catchable error.
        worker = PreviewWorker(
            raw_path, self._decoder, self.session.campaign.framing, skip_detection=True
        )
        worker.succeeded.connect(
            lambda result, n=name, f=framing: self._on_known_frame_preview_ready(result, n, f)
        )
        worker.failed.connect(self._on_preview_failed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(lambda w=worker: self._clear_preview_worker(w))
        self._preview_worker = worker
        worker.start()

    def _on_known_frame_preview_ready(
        self, result: PreviewResult, name: str, framing: FramingState
    ) -> None:
        if not shiboken6.isValid(self):
            return  # see `_on_preview_ready`
        session = self.session
        current = session.state.current_image if session is not None else None
        if current is None or current.assigned_name != name:
            return  # image already changed before extraction finished
        self._current_preview_pixels = result.preview.pixels
        self._current_preview_scale_factor = result.preview.scale_factor
        # `framing` is in full-resolution (reference) space, same as what
        # `_to_reference_space` produces — inverted here to redraw the
        # overlay at preview resolution.
        scale = result.preview.scale_factor
        self._current_frame_result = FrameResult(
            x=round(framing.x / scale),
            y=round(framing.y / scale),
            width=round(framing.width / scale),
            height=round(framing.height / scale),
            angle_deg=framing.angle_deg,
            confidence=framing.confidence,
            level="manual",
            components=ConfidenceComponents(0.0, 0.0, 0.0, 0.0, 0.0),
        )
        self._display_current_preview()
        self._update_confidence_label()

    # --- positive preview (P key) -----------------------------------------------

    def toggle_positive_preview(self) -> None:
        if self._current_preview_pixels is None:
            return
        self._positive_preview_active = not self._positive_preview_active
        if self._positive_preview_active:
            self._master_preview_active = False  # only one toggle active at a time
        self._display_current_preview()

    # --- master preview (T key) -------------------------------------------------

    def toggle_master_preview(self) -> None:
        """Toggles the "frame applied" render (T key)."""
        if self._current_preview_pixels is None:
            return
        self._master_preview_active = not self._master_preview_active
        if self._master_preview_active:
            self._positive_preview_active = False  # only one toggle active at a time
        self._display_current_preview()

    # --- cycle preview (K key) ---------------------------------------------------

    def cycle_preview_action(self, *, direction: int = 1) -> None:
        """K key: negative → positive → master → negative (Shift+K: the other
        way around), independent of P/T."""
        if self._current_preview_pixels is None:
            return
        if direction >= 0:
            if not self._positive_preview_active and not self._master_preview_active:
                self._positive_preview_active = True
            elif self._positive_preview_active:
                self._positive_preview_active = False
                self._master_preview_active = True
            else:
                self._master_preview_active = False
        else:
            if not self._positive_preview_active and not self._master_preview_active:
                self._master_preview_active = True
            elif self._master_preview_active:
                self._master_preview_active = False
                self._positive_preview_active = True
            else:
                self._positive_preview_active = False
        self._display_current_preview()

    def refresh_active_preview(self, *, fast: bool = False) -> None:
        """Re-renders the positive/master preview from what's already in memory —
        no RAW redecode. Called whenever Positive settings changes; `fast`
        (mid-drag on a slider) trades resolution for speed so the preview
        keeps up with the mouse instead of stuttering."""
        if self._positive_preview_active or self._master_preview_active:
            self._display_current_preview(fast=fast)

    def _display_current_preview(self, *, fast: bool = False) -> None:
        """Switches between negative / positive (P) / master (T) preview in the same area."""
        pixels = self._current_preview_pixels
        if pixels is None:
            return
        if self._positive_preview_active:
            if fast:
                small_pixels, small_frame = _downscaled_for_fast_preview(
                    pixels, self._current_frame_result
                )
                image = self._render_positive_preview(small_pixels, frame_override=small_frame)
            else:
                image = self._render_positive_preview(pixels)
            self.preview_area.show_image(image)
            self.preview_area.set_frame_overlay(None)  # not relevant to the inverted positive
        elif self._master_preview_active:
            if fast:
                small_pixels, small_frame = _downscaled_for_fast_preview(
                    pixels, self._current_frame_result
                )
                image = self._render_master_preview(small_pixels, frame_override=small_frame)
            else:
                image = self._render_master_preview(pixels)
            self.preview_area.show_image(image)
            self.preview_area.set_frame_overlay(None)  # frame is already applied, no need to repeat
        else:
            current = self.session.state.current_image if self.session is not None else None
            if self._pending_rotation_deg is not None:
                rotation_deg = self._pending_rotation_deg
            else:
                rotation_deg = current.rotation_deg if current is not None else 0
            rotated_pixels, rotated_frame = _rotated_for_display(
                pixels, self._current_frame_result, rotation_deg
            )
            self.preview_area.show_image(rotated_pixels)
            self.preview_area.set_frame_overlay(rotated_frame)

    def _render_master_preview(
        self, preview_pixels: np.ndarray, *, frame_override: FrameResult | None = None
    ) -> np.ndarray:
        """Preview with the frame *and rotation* applied, on the preview already in
        memory — no RAW redecode. `frame_override` (already in `preview_pixels`'s
        own coordinate space) is used instead of `self._current_frame_result`
        when set — the fast/downscaled preview path needs this, since the
        real frame's coordinates wouldn't match a shrunk pixel array.

        Same geometry logic as the real export (`imaging.geometry.apply_geometry`,
        reused as-is), but always in `native` mode: this toggle shows the
        crop/deskew/rotation, not the campaign's `fixed` scaling, which
        wouldn't make sense at preview resolution.

        Falls back to a whole-image `FrameGeometry` (width/height 0) rather
        than bailing out to the raw, unrotated `preview_pixels` when no frame
        is available yet (detection disabled, or still running in the
        background): `apply_geometry` already turns a degenerate frame into
        "crop = whole image" — dropping straight to `preview_pixels` here
        used to also skip the rotation, which is the one thing this preview
        exists to show (DECISIONS.md I-99).
        """
        current = self.session.state.current_image if self.session is not None else None
        if current is None:
            return preview_pixels
        frame = frame_override if frame_override is not None else self._current_frame_result
        geometry = apply_geometry(
            preview_pixels,
            FrameGeometry(
                x=frame.x if frame is not None else 0,
                y=frame.y if frame is not None else 0,
                width=frame.width if frame is not None else 0,
                height=frame.height if frame is not None else 0,
                angle_deg=frame.angle_deg if frame is not None else 0.0,
            ),
            rotation_deg=current.rotation_deg,
            size_mode="native",
        )
        return geometry.pixels

    def _render_positive_preview(
        self, preview_pixels: np.ndarray, *, frame_override: FrameResult | None = None
    ) -> np.ndarray:
        """Positive rendered from the preview already in memory, using campaign settings.

        Crop/deskew/rotation applied first (same geometry as the master
        preview) so the positive preview matches what the actual export will
        look like, not the raw unrotated negative.
        """
        assert self.session is not None
        config = self.session.campaign.exports.jpeg_positive
        manual = config.manual_settings
        framed_pixels = self._render_master_preview(preview_pixels, frame_override=frame_override)
        if frame_override is None:
            framed_pixels = self._apply_content_frame_preview(framed_pixels)
        # `imaging.positive.render_positive` expects 16-bit input; the preview
        # is 8-bit (`imaging.preview.Preview.pixels`) — exact rescale
        # (255 * 257 = 65535), no extra library needed.
        array16 = framed_pixels.astype(np.uint16) * 257
        positive16 = render_positive(
            array16,
            horizontal_flip=config.horizontal_flip,
            mode=config.mode,
            manual=ManualSettings(
                exposure_ev=manual.exposure_ev,
                contrast=manual.contrast,
                shadows=manual.shadows,
                highlights=manual.highlights,
            ),
        )
        positive8 = (positive16 // 257).astype(np.uint8)
        return np.stack([positive8, positive8, positive8], axis=-1)

    def _apply_content_frame_preview(self, framed_pixels: np.ndarray) -> np.ndarray:
        """Read-only reflection of the content frame already applied to the
        last `jpeg_positive` export (`session.state.current_image.
        content_framing`) — never recomputed here: detection only ever runs
        in the background export task (`imaging.content_framing`), never on
        this synchronous preview path. Skipped for the fast/downscaled
        preview (`frame_override` set): that path uses its own, unrelated
        scale, not `_current_preview_scale_factor`.

        Nothing to show before the first `jpeg_positive` export has run for
        this image (`content_framing` is still `None`) — the preview then
        falls back to the support-frame crop alone, same as before this was
        added.
        """
        session = self.session
        current = session.state.current_image if session is not None else None
        content_framing = current.content_framing if current is not None else None
        if content_framing is None or content_framing.outcome != "applied":
            return framed_pixels
        # `content_framing` is in reference/master-pixel space, same
        # convention as `framing` — `scale_factor` is reference/preview
        # (`imaging.preview.Preview.scale_factor`), so converting to this
        # preview's space divides, the opposite direction of
        # `_to_reference_space`.
        scale = self._current_preview_scale_factor
        height, width = framed_pixels.shape[:2]
        x0 = max(0, round(content_framing.x / scale))
        y0 = max(0, round(content_framing.y / scale))
        x1 = min(width, x0 + round(content_framing.width / scale))
        y1 = min(height, y0 + round(content_framing.height / scale))
        if x1 <= x0 or y1 <= y0:
            return framed_pixels
        return framed_pixels[y0:y1, x0:x1]

    def _apply_frame_result(
        self, name: str, journal_action: str, frame: FrameResult, *, rescued: bool = False
    ) -> None:
        session = self.session
        if session is None:
            return
        if frame.level == IMPOSSIBLE and journal_action == "auto":
            journal_action = "raw"
        reference = _to_reference_space(frame, self._current_preview_scale_factor)
        events: list[SessionEvent] = []
        with contextlib.suppress(IllegalTransitionError):
            events = session.apply_frame(
                name,
                x=reference.x,
                y=reference.y,
                width=reference.width,
                height=reference.height,
                angle_deg=frame.angle_deg,
                confidence=frame.confidence,
                source="raw" if frame.level == IMPOSSIBLE else "auto",
                journal_action=journal_action,
                components={
                    "c_fill": frame.components.c_fill,
                    "c_rect": frame.components.c_rect,
                    "c_size": frame.components.c_size,
                    "c_border": frame.components.c_border,
                    "c_solidity": frame.components.c_solidity,
                },
                level=frame.level,
                rescued=rescued,
            )
        self._dispatch(events)

    def recompute_frame(self) -> None:
        """C key: reruns automatic frame detection."""
        session = self.session
        current = session.state.current_image if session is not None else None
        if session is None or current is None:
            return
        self._load_preview(current.assigned_name, current.extension, journal_action="recomputed")

    # --- status line -------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self._status_timer.start(_STATUS_MESSAGE_DURATION_MS)

    def _set_mode(self, paused: bool) -> None:
        if paused:
            self.mode_label.setText(t("capture.mode_pause"))
            _set_role(self.mode_label, "warning")
        else:
            self.mode_label.setText(t("capture.mode_capture"))
            _set_role(self.mode_label, "ok")

    # --- keyboard shortcuts, CAPTURE context ------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Context priority order: text field > conflict > capture. Single
        # handler, no scattered `QShortcut` instances.
        if self.go_to_name_edit.isVisible() and self.go_to_name_edit.hasFocus():
            if event.key() == Qt.Key.Key_Escape:
                self._close_go_to_name()
                event.accept()
                return
            super().keyPressEvent(event)
            return

        if self._pending_conflict is not None:
            self._handle_conflict_key(event)
            return

        # Plain arrows, +/-/=, and Ctrl+arrows are reserved (shortcuts.py:
        # `_CAPTURE_RESERVED`) and always active — the crop's move/resize/
        # deskew, not a remappable pick-a-letter shortcut.
        key = event.key()
        modifiers = event.modifiers()
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        if ctrl and key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step_deg = (10 if shift else 1) * 0.1
            self._rotate_frame(step_deg if key == Qt.Key.Key_Right else -step_deg)
        elif key == Qt.Key.Key_Left:
            self._nudge_frame(dx=-(10 if shift else 1))
        elif key == Qt.Key.Key_Right:
            self._nudge_frame(dx=(10 if shift else 1))
        elif key == Qt.Key.Key_Up:
            self._nudge_frame(dy=-(10 if shift else 1))
        elif key == Qt.Key.Key_Down:
            self._nudge_frame(dy=(10 if shift else 1))
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._resize_frame(1 + (0.05 if shift else 0.01))
        elif key == Qt.Key.Key_Minus:
            self._resize_frame(1 - (0.05 if shift else 0.01))
        else:
            actions = self._shortcuts[CAPTURE]
            if matches(event, actions["finalize"]):
                self.finalize_current()
            elif matches(event, actions["reject"]):
                self.reject_current_image()
            elif matches(event, actions["rotate"]):
                self.rotate_image_action()
            elif matches_shifted(event, actions["rotate"]):
                self.rotate_image_action(direction=-1)
            elif matches(event, actions["recompute_frame"]):
                self.recompute_frame()
            elif matches(event, actions["toggle_guides"]):
                self._toggle_guides()
            elif matches(event, actions["positive_preview"]):
                self.toggle_positive_preview()
            elif matches(event, actions["master_preview"]):
                self.toggle_master_preview()
            elif matches(event, actions["cycle_preview"]):
                self.cycle_preview_action()
            elif matches_shifted(event, actions["cycle_preview"]):
                self.cycle_preview_action(direction=-1)
            elif matches(event, actions["go_to_name"]):
                self.open_go_to_name()
            elif matches(event, actions["pause_resume"]):
                self.toggle_pause()
            elif matches(event, actions["stop_capture"]):
                self.stop_capture()
            else:
                super().keyPressEvent(event)
                return
        event.accept()

    def _handle_conflict_key(self, event: QKeyEvent) -> None:
        """CONFLICT context: Escape always resolves as option 1 with an empty field."""
        if event.key() == Qt.Key.Key_Escape:
            self._resolve_conflict(1, None)
            event.accept()
            return

        focused = self.focusWidget()
        if focused in (self.conflict_option1_edit, self.conflict_option3_edit):
            super().keyPressEvent(event)
            return

        actions = self._shortcuts[NAME_CONFLICT]
        if matches(event, actions["option_1"]):
            self._select_conflict_option(1)
        elif matches(event, actions["option_2"]):
            self._select_conflict_option(2)
        elif matches(event, actions["option_3"]):
            self._select_conflict_option(3)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # --- crop editing: keyboard nudge/resize/rotate, always active in
    # capture, plus mouse drag on the frame overlay (`PreviewArea`) ------------

    def _ensure_negative_view_for_editing(self) -> None:
        """Framing is judged on the raw negative, not its inverted positive
        or an already-applied crop — switch back to it before editing."""
        if self._positive_preview_active or self._master_preview_active:
            self._positive_preview_active = False
            self._master_preview_active = False
            self._display_current_preview()

    def _nudge_frame(self, *, dx: int = 0, dy: int = 0) -> None:
        if self._current_frame_result is None:
            return
        self._ensure_negative_view_for_editing()
        frame = self._current_frame_result
        scale = self._current_preview_scale_factor
        new_frame = replace(frame, x=round(frame.x + dx * scale), y=round(frame.y + dy * scale))
        self._start_frame_edit(new_frame)

    def _resize_frame(self, factor: float) -> None:
        if self._current_frame_result is None:
            return
        self._ensure_negative_view_for_editing()
        frame = self._current_frame_result
        new_width = max(1, round(frame.width * factor))
        new_height = max(1, round(frame.height * factor))
        center_x = frame.x + frame.width / 2
        center_y = frame.y + frame.height / 2
        new_frame = replace(
            frame,
            x=round(center_x - new_width / 2),
            y=round(center_y - new_height / 2),
            width=new_width,
            height=new_height,
        )
        self._start_frame_edit(new_frame)

    def _rotate_frame(self, delta_deg: float) -> None:
        if self._current_frame_result is None:
            return
        self._ensure_negative_view_for_editing()
        frame = self._current_frame_result
        new_angle = max(-45.0, min(45.0, frame.angle_deg + delta_deg))
        self._start_frame_edit(replace(frame, angle_deg=new_angle))

    def _toggle_guides(self) -> None:
        """G key: rule-of-thirds guide lines within the frame."""
        self.preview_area.toggle_guides()

    def _start_frame_edit(self, new_frame: FrameResult) -> None:
        self._current_frame_result = new_frame
        self.preview_area.set_frame_overlay(new_frame)
        self._frame_commit_pending = True
        self._frame_commit_timer.start(_FRAME_COMMIT_DELAY_MS)

    def _commit_pending_frame(self) -> None:
        """Debounced, same pattern as rotation: a nudge/resize/rotate key, or
        a finished mouse drag, only calls `session.apply_frame()` (which
        re-queues all three exports) once the operator settles on a value —
        several quick successive adjustments collapse into a single export
        instead of flooding the queue with one full export per tiny change.
        Still never loses an edit: every screen exit point (`finalize_current`,
        `reject_current_image`, `stop`, `begin_shutdown`, `_load_preview`)
        calls this defensively first, so a pending edit is committed
        immediately rather than dropped if the operator moves on before the
        timer fires."""
        self._frame_commit_timer.stop()
        if not self._frame_commit_pending:
            return
        self._frame_commit_pending = False
        session = self.session
        current = session.state.current_image if session is not None else None
        frame = self._current_frame_result
        if session is None or current is None or frame is None:
            return
        reference = _to_reference_space(frame, self._current_preview_scale_factor)
        events: list[SessionEvent] = []
        with contextlib.suppress(IllegalTransitionError):
            events = session.apply_frame(
                current.assigned_name,
                x=reference.x,
                y=reference.y,
                width=reference.width,
                height=reference.height,
                angle_deg=frame.angle_deg,
                confidence=frame.confidence,
                source="manual",
                journal_action="manual",
                level=None,
            )
        self._dispatch(events)
        manual_frame = replace(frame, level="manual")
        self._current_frame_result = manual_frame
        self.preview_area.set_frame_overlay(manual_frame)
        self._update_confidence_label()

    def _on_frame_dragged(self, frame: FrameResult) -> None:
        self._current_frame_result = frame
        self._frame_commit_pending = True
        # The overlay is already redrawn by `PreviewArea` itself during the drag.

    def _on_frame_drag_finished(self) -> None:
        # Debounced like a keyboard edit (see `_commit_pending_frame`) rather
        # than committed immediately: several short drags in a row (fine-
        # tuning the crop) collapse into one export instead of one each.
        self._frame_commit_timer.start(_FRAME_COMMIT_DELAY_MS)

    def finalize_current(self) -> None:
        if self.session is None:
            return
        self._flush_pending_edits()  # the export must reflect the final rotation/crop
        try:
            events = self.session.validate_current()
        except IllegalTransitionError:
            return
        self._dispatch(events)
        self._refresh_banner()
        self._refresh_preview_state()

    def reject_current_image(self) -> None:
        if self.session is None:
            return
        self._flush_pending_edits()
        try:
            events = self.session.reject_current()
        except IllegalTransitionError:
            return
        except ValueError as exc:
            # Defense in depth (CLAUDE.md rule 4: no unhandled system error
            # during capture) — shouldn't happen once the CSV row is reset
            # to `todo` before the cursor move inside `reject_current()`.
            self._set_status(str(exc))
            return
        self._dispatch(events)
        self._refresh_banner()
        self._refresh_preview_state()

    def rotate_image_action(self, *, direction: int = 1) -> None:
        """V key (clockwise) / Shift+V (counter-clockwise): rotates the
        current image 90°, live preview updated immediately.

        Rotates the plain negative view itself (pixels and frame overlay
        both), rather than switching to the master preview — the operator
        stays on whichever view they were on, and the crop rectangle never
        disappears.

        Only the display updates right away: the actual re-export
        (`session.set_rotation`, cancel-and-re-queue) is debounced —
        rotating several times in a row shouldn't re-decode the RAW and
        produce a new TIFF/JPEG set after every intermediate press, only
        once the operator settles on a value.
        """
        session = self.session
        if session is None or session.state.current_image is None:
            return
        current = session.state.current_image
        if current.state != "IN_REVIEW":
            return  # matches the IllegalTransitionError set_rotation would raise
        base = (
            self._pending_rotation_deg
            if self._pending_rotation_deg is not None
            else current.rotation_deg
        )
        self._pending_rotation_deg = (base + 90 * direction) % 360
        self._display_current_preview()
        self._rotation_commit_timer.start(_ROTATION_COMMIT_DELAY_MS)
        self._set_status(t("capture.status_rotation", rotation_deg=self._pending_rotation_deg))

    def _commit_pending_rotation(self) -> None:
        """Debounced: applies whatever rotation the operator settled on, in
        one `set_rotation` call regardless of how many presses it took."""
        self._rotation_commit_timer.stop()
        pending = self._pending_rotation_deg
        self._pending_rotation_deg = None
        if self.session is None or pending is None:
            return
        try:
            events = self.session.set_rotation(pending)
        except IllegalTransitionError:
            return
        self._dispatch(events)

    def toggle_pause(self) -> None:
        if self.session is None:
            return
        if self.session.paused:
            self._dispatch(self.session.resume())
        else:
            self.session.pause()
        self._refresh_banner()
        self._refresh_preview_state()

    def stop_capture(self) -> None:
        self.stop()
        self.stopped.emit()

    # --- session history (correction side panel) ---------------------------

    def session_history(self) -> list[SessionHistoryEntry]:
        return self.session.session_history() if self.session is not None else []

    def reopen_image_for_correction(self, name: str) -> None:
        """Reopens a finalized image from the history panel for correction.

        Refuses (silently, status line only) if an image is already loaded —
        finalize or reject it first.
        """
        session = self.session
        if session is None:
            return
        try:
            events = session.reopen_for_correction(name)
        except IllegalTransitionError:
            self._set_status(t("capture.status_reopen_busy"))
            return
        except ValueError as exc:
            self._set_status(str(exc))
            return
        self._dispatch(events)
        current = session.state.current_image
        assert current is not None
        self._positive_preview_active = False
        self._master_preview_active = False
        self._load_preview_known_frame(current.assigned_name, current.extension, current.framing)
        self._refresh_banner()
        self._set_status(t("capture.status_reopened", name=name))

    def open_go_to_name(self) -> None:
        if self.session is None:
            return
        inventory = self.session.inventory
        todo_names = [
            row[inventory.name_column] for row in inventory.rows if row[STATUS_COLUMN] == "todo"
        ]
        completer = QCompleter(todo_names, self.go_to_name_edit)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.go_to_name_edit.setCompleter(completer)
        self.go_to_name_edit.clear()
        self.go_to_name_edit.setVisible(True)
        self.go_to_name_edit.setFocus()

    def _close_go_to_name(self) -> None:
        self.go_to_name_edit.setVisible(False)
        self.go_to_name_edit.clear()
        self.setFocus()

    def _submit_go_to_name(self) -> None:
        if self.session is None:
            return
        name = self.go_to_name_edit.text().strip()
        self._close_go_to_name()
        if not name:
            return
        try:
            self.session.go_to_name(name)
        except ValueError:
            self._set_status(t("capture.status_unknown_name", name=name))
            return
        self._refresh_banner()

    # --- name conflict -----------------------------------------------------

    def _show_conflict_panel(self, event: NameConflictDetected) -> None:
        self._pending_conflict = event
        self.conflict_label.setText(t("capture.conflict_title", name=event.name))
        self.conflict_option1_edit.setText(f"{event.name}_BIS")
        self.conflict_option3_edit.setText(f"{event.name}_OLD")
        self.conflict_use_next_free_button.setEnabled(self._next_free_name() is not None)
        self.conflict_panel.setVisible(True)

    def _hide_conflict_panel(self) -> None:
        self._pending_conflict = None
        self.conflict_panel.setVisible(False)
        self.setFocus()

    def _next_free_name(self) -> str | None:
        """Next `todo` row after the conflicting one — a suggestion for option 1.

        Read-only: unlike `Inventory.go_to_next_todo()`, doesn't move the
        cursor (this is just filling a text field, not resolving anything).
        """
        if self.session is None:
            return None
        inventory = self.session.inventory
        for row in inventory.rows[inventory.cursor + 1 :]:
            if row[STATUS_COLUMN] == "todo":
                return row[inventory.name_column]
        return None

    def _use_next_free_name(self) -> None:
        name = self._next_free_name()
        if name is not None:
            self.conflict_option1_edit.setText(name)
            self.conflict_option1_edit.setFocus()
            self.conflict_option1_edit.selectAll()

    def _select_conflict_option(self, option: int) -> None:
        if option == 1:
            self.conflict_option1_edit.setFocus()
            self.conflict_option1_edit.selectAll()
        elif option == 2:
            self.conflict_replace_button.setFocus()
        elif option == 3:
            self.conflict_option3_edit.setFocus()
            self.conflict_option3_edit.selectAll()

    def _resolve_conflict(self, option: int, new_name: str | None = None) -> None:
        if self.session is None or self._pending_conflict is None:
            return
        # Same reasoning as `finalize_current`/`reject_current_image`:
        # resolving the conflict can itself finalize whatever image is
        # still current (a new, till-now-conflicting file bumping it).
        self._flush_pending_edits()
        try:
            events = self.session.resolve_conflict(option, new_name=new_name or None)
        except ValueError as exc:
            self._set_status(str(exc))
            return
        self._hide_conflict_panel()
        self._dispatch(events)
        self._refresh_banner()
        self._refresh_preview_state()
