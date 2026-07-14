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

import numpy as np
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
    StabilizationTimedOut,
)
from scanassistant.core.events import Warning as WarningEvent
from scanassistant.core.export_runner import MasterExportRunner
from scanassistant.core.fs import FileSystem
from scanassistant.core.queue import ExportExecutor, ExportRunner, InlineExportExecutor
from scanassistant.core.session import CaptureSession, SessionHistoryEntry
from scanassistant.gui.errors import format_critical, format_warning
from scanassistant.gui.preview_worker import PreviewResult, PreviewWorker
from scanassistant.gui.widgets.preview_area import PreviewArea
from scanassistant.i18n import t
from scanassistant.imaging.framing import (
    IMPOSSIBLE,
    ConfidenceComponents,
    FrameResult,
    detect_frame,
)
from scanassistant.imaging.geometry import FrameGeometry, apply_geometry
from scanassistant.imaging.positive import ManualSettings, render_positive
from scanassistant.imaging.raw import RawDecoder, RawpyDecoder
from scanassistant.journal.journal import Journal
from scanassistant.metadata.writer import ExifToolMetadataWriter
from scanassistant.metadata.writer import is_available as is_exiftool_available
from scanassistant.project.campaign import Campaign
from scanassistant.project.inventory import STATUS_COLUMN, Inventory
from scanassistant.project.layout import CampaignPaths
from scanassistant.project.state import FramingState, ProjectState
from scanassistant.watcher.monitor import FolderMonitor
from scanassistant.watcher.stability import poll_interval_s

_STATUS_MESSAGE_DURATION_MS = 5000
_MIN_PUMP_INTERVAL_MS = 100

_LEVEL_LABELS = {
    "reliable": ("capture.confidence_reliable", "ok"),
    "review": ("capture.confidence_review", "warning"),
    "impossible": ("capture.confidence_impossible", "critical"),
    # "manual": GUI-only sentinel (frame edit mode, M key), not a possible
    # output of `imaging.framing.classify()`.
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
    ) -> None:
        super().__init__(parent)
        self.session: CaptureSession | None = None
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
        self._editing_frame = False
        self._edit_frame: FrameResult | None = None
        self._edit_original_frame: FrameResult | None = None
        self._positive_preview_active = False
        self._master_preview_active = False
        self._pending_conflict: NameConflictDetected | None = None

        self.preview_area = PreviewArea()

        self.name_label = QLabel()
        self.name_label.setStyleSheet("font-size: 28pt; font-weight: bold;")
        self.confidence_label = QLabel()
        self.progress_label = QLabel()
        self.next_label = QLabel()
        self.next_label.setProperty("role", "secondary")
        self.queue_label = QLabel()
        self.queue_label.setProperty("role", "secondary")

        banner_top = QHBoxLayout()
        banner_top.addWidget(self.name_label)
        banner_top.addSpacing(16)
        banner_top.addWidget(self.confidence_label)
        banner_top.addStretch(1)
        banner_top.addWidget(self.progress_label)
        banner_bottom = QHBoxLayout()
        banner_bottom.addWidget(self.next_label)
        banner_bottom.addStretch(1)
        banner_bottom.addWidget(self.queue_label)

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
        self.warning_banner.setVisible(False)
        self.warning_banner.clicked.connect(self._show_warning_detail)
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
        status_row = QHBoxLayout()
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.mode_label)

        layout = QVBoxLayout(self)
        layout.addWidget(self.preview_area, 1)
        layout.addLayout(banner_top)
        layout.addLayout(banner_bottom)
        layout.addWidget(self.go_to_name_edit)
        layout.addWidget(self.conflict_panel)
        layout.addWidget(self.warning_banner)
        layout.addWidget(self.critical_banner)
        layout.addLayout(status_row)

        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self.status_label.setText(""))

        self._pump_timer = QTimer(self)
        self._pump_timer.timeout.connect(self._pump)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

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
    ) -> None:
        monitor = FolderMonitor(
            Path(campaign.capture.watched_folder),
            campaign.capture.extensions,
            watch_mode="polling",
            stabilization_delay_s=campaign.capture.stabilization_delay_s,
            stabilization_timeout_s=campaign.capture.stabilization_timeout_s,
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
        )
        if was_stale:
            report = perform_crash_recovery(self.session)
            self._show_recovery_report(report)
        if not is_exiftool_available(exiftool_executable):
            # Persistent warning: exports will be produced without metadata.
            self._show_warning_banner("A-01", {"message": t("metadata.exiftool_unavailable")})
        self.session.initial_scan()
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

    def stop(self) -> None:
        self._pump_timer.stop()
        if self.session is not None:
            self.session.stop()  # already waits for the export queue to drain
            self.session.export_executor.shutdown()  # releases the background thread, if any
        self.session = None
        self._pending_conflict = None
        self.conflict_panel.setVisible(False)

    # --- loop ----------------------------------------------------------------

    def _pump(self) -> None:
        if self.session is None:
            return
        for event in self.session.pump(time.monotonic()):
            self._handle_event(event)
        self._refresh_banner()
        self._refresh_preview_state()
        self.queue_changed.emit()

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
        """Warning (yellow): persistent, clickable for detail.

        Only one banner shown at a time (most recent warning) rather than a
        stack per code: real warnings are rare and transient, a full queue
        would be unwarranted complexity here.
        """
        self._last_warning = (code, details)
        self.warning_banner.setText(format_warning(code, details))
        self.warning_banner.setVisible(True)

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
        session.resume_from_critical()
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
        self._loaded_preview_for = name
        self._current_frame_result = None
        self._update_confidence_label()
        raw_path = self.session.paths.raw_dir / f"{name}{extension}"

        worker = PreviewWorker(raw_path, self._decoder, self.session.campaign.framing, self)
        # Target name is bound into the connection closure, not re-read from
        # `self._loaded_preview_for` when the signal fires: if another image
        # was already loaded in the meantime, that field would have changed
        # and the (stale) result would otherwise land on the wrong image.
        worker.succeeded.connect(
            lambda result, n=name, ja=journal_action: self._on_preview_ready(result, n, ja)
        )
        worker.failed.connect(self._on_preview_failed)
        worker.finished.connect(worker.deleteLater)
        self._preview_worker = worker
        worker.start()

    def _on_preview_ready(self, result: PreviewResult, name: str, journal_action: str) -> None:
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
            self._apply_frame_result(name, journal_action, result.frame)

    def _on_preview_failed(self, error: str) -> None:
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

        worker = PreviewWorker(
            raw_path, self._decoder, self.session.campaign.framing, self, skip_detection=True
        )
        worker.succeeded.connect(
            lambda result, n=name, f=framing: self._on_known_frame_preview_ready(result, n, f)
        )
        worker.failed.connect(self._on_preview_failed)
        worker.finished.connect(worker.deleteLater)
        self._preview_worker = worker
        worker.start()

    def _on_known_frame_preview_ready(
        self, result: PreviewResult, name: str, framing: FramingState
    ) -> None:
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

    def _display_current_preview(self) -> None:
        """Switches between negative / positive (P) / master (T) preview in the same area."""
        pixels = self._current_preview_pixels
        if pixels is None:
            return
        if self._positive_preview_active:
            self.preview_area.show_image(self._render_positive_preview(pixels))
            self.preview_area.set_frame_overlay(None)  # not relevant to the inverted positive
        elif self._master_preview_active:
            self.preview_area.show_image(self._render_master_preview(pixels))
            self.preview_area.set_frame_overlay(None)  # frame is already applied, no need to repeat
        else:
            self.preview_area.show_image(pixels)
            self.preview_area.set_frame_overlay(self._current_frame_result)

    def _render_master_preview(self, preview_pixels: np.ndarray) -> np.ndarray:
        """Preview with the frame *and rotation* applied, on the preview already in
        memory — no RAW redecode.

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
        frame = self._current_frame_result
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

    def _render_positive_preview(self, preview_pixels: np.ndarray) -> np.ndarray:
        """Positive rendered from the preview already in memory, using campaign settings.

        Crop/deskew/rotation applied first (same geometry as the master
        preview, DECISIONS.md I-99) so the positive preview matches what the
        actual export will look like, not the raw unrotated negative.
        """
        assert self.session is not None
        config = self.session.campaign.exports.jpeg_positive
        manual = config.manual_settings
        framed_pixels = self._render_master_preview(preview_pixels)
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

    def _apply_frame_result(self, name: str, journal_action: str, frame: FrameResult) -> None:
        session = self.session
        if session is None:
            return
        if frame.level == IMPOSSIBLE and journal_action == "auto":
            journal_action = "raw"
        reference = _to_reference_space(frame, self._current_preview_scale_factor)
        with contextlib.suppress(IllegalTransitionError):
            session.apply_frame(
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
            )

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
        # Context priority order: text field > conflict > frame edit >
        # capture. Single handler, no scattered `QShortcut` instances.
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

        if self._editing_frame:
            self._handle_edit_mode_key(event)
            return

        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.finalize_current()
        elif key == Qt.Key.Key_R:
            self.reject_current_image()
        elif key == Qt.Key.Key_V:
            self.rotate_image_action()
        elif key == Qt.Key.Key_C:
            self.recompute_frame()
        elif key == Qt.Key.Key_M:
            self.enter_edit_mode()
        elif key == Qt.Key.Key_P:
            self.toggle_positive_preview()
        elif key == Qt.Key.Key_T:
            self.toggle_master_preview()
        elif key == Qt.Key.Key_Left:
            self.navigate(-1)
        elif key == Qt.Key.Key_Right:
            self.navigate(1)
        elif key == Qt.Key.Key_G:
            self.open_go_to_name()
        elif key == Qt.Key.Key_Space:
            self.toggle_pause()
        elif key == Qt.Key.Key_Escape:
            self.stop_capture()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # --- frame edit mode (M key) ------------------------------------------------

    def enter_edit_mode(self) -> None:
        if self.session is None or self.session.state.current_image is None:
            return
        if self._current_frame_result is None:
            return  # detection not available yet for the current image
        if self._positive_preview_active or self._master_preview_active:
            # Framing is judged on the raw negative, not on its inverted
            # positive or on a render where the frame is already applied.
            self._positive_preview_active = False
            self._master_preview_active = False
            self._display_current_preview()
        self._editing_frame = True
        self._edit_original_frame = self._current_frame_result
        self._edit_frame = self._current_frame_result
        self._update_edit_status()

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

        key = event.key()
        if key == Qt.Key.Key_1:
            self._select_conflict_option(1)
        elif key == Qt.Key.Key_2:
            self._select_conflict_option(2)
        elif key == Qt.Key.Key_3:
            self._select_conflict_option(3)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def _handle_edit_mode_key(self, event: QKeyEvent) -> None:
        key = event.key()
        modifiers = event.modifiers()
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)

        if ctrl and key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step_deg = (10 if shift else 1) * 0.1
            self._edit_rotate(step_deg if key == Qt.Key.Key_Right else -step_deg)
        elif key == Qt.Key.Key_Left:
            self._edit_move(dx=-(10 if shift else 1), dy=0)
        elif key == Qt.Key.Key_Right:
            self._edit_move(dx=(10 if shift else 1), dy=0)
        elif key == Qt.Key.Key_Up:
            self._edit_move(dx=0, dy=-(10 if shift else 1))
        elif key == Qt.Key.Key_Down:
            self._edit_move(dx=0, dy=(10 if shift else 1))
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._edit_resize(1 + (0.05 if shift else 0.01))
        elif key == Qt.Key.Key_Minus:
            self._edit_resize(1 - (0.05 if shift else 0.01))
        elif key == Qt.Key.Key_C:
            self._edit_recompute()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._confirm_edit()
        elif key == Qt.Key.Key_Escape:
            self._cancel_edit()
        else:
            return
        event.accept()

    def _edit_move(self, *, dx: int, dy: int) -> None:
        if self._edit_frame is None:
            return
        scale = self._current_preview_scale_factor
        self._edit_frame = replace(
            self._edit_frame,
            x=round(self._edit_frame.x + dx * scale),
            y=round(self._edit_frame.y + dy * scale),
        )
        self._show_edit_overlay()

    def _edit_resize(self, factor: float) -> None:
        if self._edit_frame is None:
            return
        frame = self._edit_frame
        new_width = max(1, round(frame.width * factor))
        new_height = max(1, round(frame.height * factor))
        center_x = frame.x + frame.width / 2
        center_y = frame.y + frame.height / 2
        self._edit_frame = replace(
            frame,
            x=round(center_x - new_width / 2),
            y=round(center_y - new_height / 2),
            width=new_width,
            height=new_height,
        )
        self._show_edit_overlay()

    def _edit_rotate(self, delta_deg: float) -> None:
        if self._edit_frame is None:
            return
        new_angle = max(-45.0, min(45.0, self._edit_frame.angle_deg + delta_deg))
        self._edit_frame = replace(self._edit_frame, angle_deg=new_angle)
        self._show_edit_overlay()

    def _edit_recompute(self) -> None:
        """C key in edit mode: reruns detection, replaces the frame being edited."""
        if self._current_preview_pixels is None or self.session is None:
            return
        config = self.session.campaign.framing
        self._edit_frame = detect_frame(
            self._current_preview_pixels,
            margin_pct=config.margin_pct,
            max_deskew_deg=config.max_deskew_deg,
            reliable_threshold=config.reliable_threshold,
            review_threshold=config.review_threshold,
            threshold_bias=config.threshold_bias,
        )
        self._show_edit_overlay()

    def _show_edit_overlay(self) -> None:
        self.preview_area.set_frame_overlay(self._edit_frame)
        self._update_edit_status()

    def _update_edit_status(self) -> None:
        frame = self._edit_frame
        if frame is None:
            return
        self.status_label.setText(
            t(
                "capture.edit_status",
                width=frame.width,
                height=frame.height,
                angle=f"{frame.angle_deg:.1f}",
            )
        )

    def _confirm_edit(self) -> None:
        session = self.session
        current = session.state.current_image if session is not None else None
        frame = self._edit_frame
        if session is not None and current is not None and frame is not None:
            reference = _to_reference_space(frame, self._current_preview_scale_factor)
            with contextlib.suppress(IllegalTransitionError):
                session.apply_frame(
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
            manual_frame = replace(frame, level="manual")
            self._current_frame_result = manual_frame
            self.preview_area.set_frame_overlay(manual_frame)
            self._update_confidence_label()
        self._exit_edit_mode()

    def _cancel_edit(self) -> None:
        self.preview_area.set_frame_overlay(self._edit_original_frame)
        self._exit_edit_mode()

    def _exit_edit_mode(self) -> None:
        self._editing_frame = False
        self._edit_frame = None
        self._edit_original_frame = None
        self.status_label.setText("")

    def finalize_current(self) -> None:
        if self.session is None:
            return
        try:
            self.session.validate_current()
        except IllegalTransitionError:
            return
        self._refresh_banner()
        self._refresh_preview_state()

    def reject_current_image(self) -> None:
        if self.session is None:
            return
        try:
            self.session.reject_current()
        except IllegalTransitionError:
            return
        self._refresh_banner()
        self._refresh_preview_state()

    def rotate_image_action(self) -> None:
        """V key: rotates the current image 90° clockwise, live preview updated immediately.

        The plain negative view never rotates (it mirrors the raw scan, so
        the frame overlay stays meaningful); switches to the master preview
        (T) instead, which is the only rendering that actually applies
        rotation — that's what makes the effect visible right away.
        """
        if self.session is None or self.session.state.current_image is None:
            return
        try:
            self.session.rotate_current()
        except IllegalTransitionError:
            return
        if not self._master_preview_active:
            self._master_preview_active = True
            self._positive_preview_active = False
        self._display_current_preview()
        rotation_deg = self.session.state.current_image.rotation_deg
        self._set_status(t("capture.status_rotation", rotation_deg=rotation_deg))

    def navigate(self, direction: int) -> None:
        if self.session is None:
            return
        moved = (
            self.session.go_to_next_name() if direction > 0 else self.session.go_to_previous_name()
        )
        if moved:
            self._refresh_banner()

    def toggle_pause(self) -> None:
        if self.session is None:
            return
        if self.session.paused:
            self.session.resume()
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
            session.reopen_for_correction(name)
        except IllegalTransitionError:
            self._set_status(t("capture.status_reopen_busy"))
            return
        except ValueError as exc:
            self._set_status(str(exc))
            return
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
        try:
            self.session.resolve_conflict(option, new_name=new_name or None)
        except ValueError as exc:
            self._set_status(str(exc))
            return
        self._hide_conflict_panel()
        self._refresh_banner()
        self._refresh_preview_state()
