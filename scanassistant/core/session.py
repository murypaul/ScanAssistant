"""Capture orchestrator: per-image state machine.

`CaptureSession` is the only module that knows the full lifecycle of an
image, from detection in the watched folder to completed exports: it ties
together `watcher.monitor`, `core.ingest`, `core.queue`,
`project.inventory`/`state`, `journal.journal`. No dependency on PySide6:
`pump()` and operator actions (`reject_current`, `validate_current`,
`resolve_conflict`, `pause`/`resume`) return lists of `SessionEvent`; the
GUI subscribes to these and renders them via `scanassistant.i18n.t()`.

Clock is injectable: `pump(now)` receives `now` from the caller, same as
`watcher.monitor.FolderMonitor.tick()`.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scanassistant.core.errors import IllegalTransitionError, IntegrityCheckFailedError
from scanassistant.core.events import (
    CriticalError,
    CriticalResolved,
    FramingApplied,
    ImageDetected,
    ImageErrored,
    ImageIngested,
    ImageRejected,
    ImageStabilized,
    ImageStateChanged,
    NameConflictDetected,
    NameConflictResolved,
    OrientationToggled,
    SessionEvent,
    StabilizationTimedOut,
)
from scanassistant.core.events import (
    Warning as WarningEvent,
)
from scanassistant.core.fs import FileSystem
from scanassistant.core.ingest import find_conflicting_path, ingest_file
from scanassistant.core.queue import (
    EXPORT_TASK_KINDS,
    ExportContext,
    ExportFailure,
    ExportQueue,
    ExportResult,
    ExportRunner,
    ExportTask,
)
from scanassistant.core.recovery import rebuild_export_context
from scanassistant.journal.journal import Journal
from scanassistant.metadata.xmp_sidecar import render_raw_sidecar
from scanassistant.project.campaign import Campaign
from scanassistant.project.inventory import Inventory, validate_name
from scanassistant.project.layout import CampaignPaths
from scanassistant.project.state import (
    CurrentImageState,
    ErrorImage,
    FramingState,
    IgnoredFile,
    ProjectState,
    save_state,
)
from scanassistant.watcher.monitor import Detected, FolderMonitor, Stabilized, is_candidate_file
from scanassistant.watcher.monitor import StabilizationTimedOut as MonitorStabilizationTimedOut

_DERIVATIVE_DIRS = ("tiff_dir", "jpeg_master_dir", "jpeg_positive_dir")
_AUTOSURVEILLANCE_INTERVAL_S = 10.0  # folder accessibility + free disk space check interval
_EXPORT_QUEUE_WARN_THRESHOLD = 20  # queue size that triggers "the processing queue is growing"
_PUMP_TIME_BUDGET_S = 0.08  # never block the UI thread for more than ~100 ms


@dataclass
class _PendingConflict:
    path: Path
    name: str
    existing_path: Path


class CaptureSession:
    """State machine driving capture for an open campaign."""

    def __init__(
        self,
        *,
        paths: CampaignPaths,
        campaign: Campaign,
        inventory: Inventory,
        state: ProjectState,
        journal: Journal,
        fs: FileSystem,
        monitor: FolderMonitor,
        export_runner: ExportRunner,
        now_wall: type[datetime] = datetime,
        disk_warn_gb: float = 10.0,
        disk_critical_gb: float = 2.0,
    ) -> None:
        self.paths = paths
        self.campaign = campaign
        self.inventory = inventory
        self.state = state
        self.journal = journal
        self.fs = fs
        self.monitor = monitor
        self.export_runner = export_runner
        self._now_wall = now_wall
        self._disk_warn_gb = disk_warn_gb
        self._disk_critical_gb = disk_critical_gb

        self.export_queue = ExportQueue.from_state_entries(state.export_queue)
        self._pending_ingest: deque[Path] = deque()
        self._conflict: _PendingConflict | None = None
        self._export_pending_kinds: dict[str, set[str]] = {}
        self._exports_ready: set[str] = set()
        self._awaiting_export: set[str] = set()
        self._exhaustion_signaled = False
        self.paused = state.mode == "pause"
        self._suspended_code: str | None = None
        self._disk_warned = False
        self._queue_growth_warned = False
        self._last_autosurveillance_check: float | None = None
        self._csv_write_suspended = False
        self._inventory_mtime = self._safe_mtime(paths.inventory_csv)

    # --- main loop -------------------------------------------------------

    def pump(self, now: float) -> list[SessionEvent]:
        """Advance monitoring and processing by one tick; returns the resulting events."""
        events: list[SessionEvent] = []
        deadline = self._new_deadline()
        events.extend(self._check_autosurveillance(now))
        for monitor_event in self.monitor.tick(now):
            events.extend(self._handle_monitor_event(monitor_event))
        events.extend(self._drain_pending_ingest(deadline))
        if self._suspended_code is None and len(self.export_queue):
            events.extend(self._drain_exports(deadline))
        events.extend(self._check_queue_growth())
        return events

    def _new_deadline(self) -> float:
        """Real-clock deadline bounding a `pump()` call.

        Distinct from the injectable `now` passed to `pump()`: this uses a
        real `time.monotonic()` timer, since it's the actual Qt thread that
        must never freeze, regardless of the clock used in tests.
        """
        return time.monotonic() + _PUMP_TIME_BUDGET_S

    def _check_queue_growth(self) -> list[SessionEvent]:
        """Warns once the export queue backs up beyond the threshold."""
        size = len(self.export_queue)
        if size > _EXPORT_QUEUE_WARN_THRESHOLD:
            if self._queue_growth_warned:
                return []
            self._queue_growth_warned = True
            self.journal.log(
                "SYSTEM", "error", level="warn", details={"code": "E-15", "queue_size": size}
            )
            return [WarningEvent(code="E-15", details={"queue_size": size})]
        self._queue_growth_warned = False
        return []

    def _handle_monitor_event(
        self, event: Detected | Stabilized | MonitorStabilizationTimedOut
    ) -> list[SessionEvent]:
        if isinstance(event, Detected):
            self.journal.log("CAPTURE", "detected", details={"size": event.size})
            return [ImageDetected(path=event.path, size=event.size)]

        if isinstance(event, Stabilized):
            self.journal.log("CAPTURE", "stabilized", details={"duration_s": event.duration_s})
            events: list[SessionEvent] = [
                ImageStabilized(path=event.path, duration_s=event.duration_s)
            ]
            if self.paused or self._suspended_code is not None:
                # Detections keep queuing up during a pause/suspension, using
                # the same pause_queue either way; ingestion resumes identically.
                self.state.pause_queue.append(str(event.path))
                self.journal.log("CAPTURE", "pause_queued", details={"path": str(event.path)})
            else:
                self._pending_ingest.append(event.path)
            return events

        # MonitorStabilizationTimedOut: stabilization timed out.
        self.journal.log(
            "CAPTURE",
            "timeout",
            level="warn",
            details={"path": str(event.path)},
            result="error",
        )
        return [
            StabilizationTimedOut(path=event.path),
            WarningEvent(code="E-03", details={"path": str(event.path)}),
        ]

    # --- ingestion ---------------------------------------------------------

    def _drain_pending_ingest(self, deadline: float | None = None) -> list[SessionEvent]:
        events: list[SessionEvent] = []
        processed = 0
        while (
            self._pending_ingest
            and self._conflict is None
            and not self.paused
            and self._suspended_code is None
        ):
            if deadline is not None and processed and time.monotonic() >= deadline:
                break  # remaining items wait for the next pump() call rather than blocking here.

            if self.inventory.is_exhausted():
                if not self._exhaustion_signaled:
                    self.journal.log(
                        "SYSTEM", "error", level="error", details={"code": "E-12"}, result="error"
                    )
                    self._exhaustion_signaled = True
                events.append(CriticalError(code="E-12"))
                break

            source_path = self._pending_ingest[0]
            name = self.inventory.current_name()
            assert name is not None  # guaranteed by is_exhausted() above

            conflict_path = find_conflicting_path(name, self.paths, self.fs)
            if conflict_path is not None:
                self._pending_ingest.popleft()
                self._conflict = _PendingConflict(
                    path=source_path, name=name, existing_path=conflict_path
                )
                self.journal.log(
                    "NAMING",
                    "conflict_detected",
                    image=name,
                    level="warn",
                    details={"existing_path": str(conflict_path)},
                )
                events.append(NameConflictDetected(name=name, existing_path=str(conflict_path)))
                break

            events.extend(self._ingest_one(source_path, name, csv_row=name, deadline=deadline))
            self._pending_ingest.popleft()
            processed += 1

        return events

    def _ingest_one(
        self,
        source_path: Path,
        name: str,
        *,
        csv_row: str | None,
        deadline: float | None = None,
    ) -> list[SessionEvent]:
        """Moves `source_path` into `RAW/<name>` and installs it as the current image.

        `csv_row`: inventory row to advance (name + cursor + status). `None`
        for an off-list name ingested without touching any CSV row (name
        conflict resolution, option 1: the original row stays `todo`) — as
        opposed to a free-form name, which adds a new row.
        """
        events: list[SessionEvent] = []
        source_stat = self.fs.stat(source_path)

        try:
            result = ingest_file(
                source_path,
                name=name,
                paths=self.paths,
                fs=self.fs,
                verify_checksum=self.campaign.capture.verify_checksum,
            )
        except IntegrityCheckFailedError:
            self.journal.log(
                "FILE",
                "checksum_mismatch",
                image=name,
                level="error",
                details={"source_file": source_path.name},
                result="error",
            )
            events.append(WarningEvent(code="E-04", details={"name": name}))
            return events

        events.extend(self._finalize_current_as_validated())

        if csv_row is not None:
            self.inventory.set_source_file(csv_row, source_path.name)
        self.journal.log(
            "FILE",
            "ingested",
            image=name,
            details={
                "from": source_path.name,
                "size": source_stat.size,
                "mtime": source_stat.mtime,
                "via": result.via,
                **({"sha256": result.sha256} if result.sha256 else {}),
            },
        )
        self.journal.log(
            "NAMING", "assigned", image=name, details={"source_file": source_path.name}
        )
        events.append(ImageIngested(name=name, source_file=source_path.name, via=result.via))

        if self.campaign.options.raw_xmp_sidecar:
            self._write_raw_sidecar(name, source_path)

        if not result.source_removed:
            self.state.ignored_files.append(
                IgnoredFile(
                    name=source_path.name,
                    size=source_stat.size,
                    mtime=source_stat.mtime,
                    reason="leftover",
                )
            )
            self.journal.log(
                "CAPTURE",
                "ignored",
                level="warn",
                details={"reason": "leftover", "path": str(source_path)},
            )
            events.append(WarningEvent(code="E-08", details={"name": source_path.name}))

        if csv_row is not None:
            before_cursor = self.inventory.cursor
            self.inventory.advance_to_next_todo()
            self.state.csv_cursor = self.inventory.cursor
            self.journal.log(
                "CSV",
                "cursor",
                details={
                    "before": before_cursor,
                    "after": self.inventory.cursor,
                    "cause": "ingested",
                },
            )

        self.state.current_image = CurrentImageState(
            assigned_name=name,
            source_file=source_path.name,
            extension=result.extension,
            state="IN_REVIEW",
        )
        events.append(ImageStateChanged(name=name, previous="INGESTED", new="IN_REVIEW"))

        # Persisted *before* exports are queued/processed (which can take
        # several seconds): if the process is killed during export,
        # state.json/inventory.csv must already reflect that the RAW is
        # ingested and the image is IN_REVIEW — otherwise crash recovery
        # (`crash_recovery._finalize_in_review_image`) can't find the image,
        # and the next capture retries the same name (a blocking false conflict).
        if csv_row is not None:
            events.extend(self._save_inventory())
        self._persist_state()

        self._enqueue_exports(name)
        events.extend(self._drain_exports(deadline))
        self._persist_state()
        return events

    def _write_raw_sidecar(self, name: str, source_path: Path) -> None:
        """Writes the `RAW/<name>.xmp` sidecar: always follows the RAW file, never modifies it."""
        sidecar_path = self.paths.raw_dir / f"{name}.xmp"
        content = render_raw_sidecar(
            identifier=name, source=source_path.name, creator=self.campaign.operator
        )
        try:
            self.fs.write_text(sidecar_path, content)
        except OSError as exc:
            self.journal.log(
                "METADATA",
                "missing",
                image=name,
                level="warn",
                details={"reason": str(exc), "file": str(sidecar_path)},
                result="error",
            )

    # --- exports -------------------------------------------------------------

    def _enqueue_exports(self, name: str) -> None:
        kinds = [kind for kind in EXPORT_TASK_KINDS if self._export_enabled(kind)]
        if not kinds:
            # Nothing to wait for: the image is immediately "export-ready"
            # (otherwise `validate_current` would wait forever for a drain
            # that will never happen).
            self._exports_ready.add(name)
            return
        self.export_queue.enqueue(name, kinds, self._build_export_context(name))
        self._export_pending_kinds[name] = set(kinds)

    def _build_export_context(self, name: str) -> ExportContext:
        """Snapshot of the current image for export — see `ExportTask`."""
        current = self.state.current_image
        assert current is not None and current.assigned_name == name
        framing = current.framing
        return ExportContext(
            raw_path=self.paths.raw_dir / f"{name}{current.extension}",
            extension=current.extension,
            source_file=current.source_file,
            orientation=current.orientation,
            x=framing.x,
            y=framing.y,
            width=framing.width,
            height=framing.height,
            angle_deg=framing.angle_deg,
        )

    def _export_enabled(self, kind: str) -> bool:
        exports = self.campaign.exports
        return {
            "tiff": exports.tiff.enabled,
            "jpeg_master": exports.jpeg_master.enabled,
            "jpeg_positive": exports.jpeg_positive.enabled,
        }[kind]

    def _drain_exports(self, deadline: float | None = None) -> list[SessionEvent]:
        """Runs the export queue (real `ExportRunner` or `FakeExportRunner`).

        Bounded by `deadline`: whatever's left is processed on the next
        call — `state.export_queue` keeps an exact record of it.
        """
        events: list[SessionEvent] = []
        if self._suspended_code is not None:
            # Critical suspension: the queue stays as-is, resumed on the next
            # non-suspended `pump()` (or explicitly via `resume_from_critical`).
            self.state.export_queue = self.export_queue.to_state_entries()
            return events
        for task, result in self.export_queue.drain(self.export_runner, deadline=deadline):
            if isinstance(result, ExportFailure):
                events.extend(self._flag_export_failure(task, result))
                continue
            self._log_export(task, result)
            remaining = self._export_pending_kinds.get(task.name)
            if remaining is None:
                continue
            remaining.discard(task.kind)
            if remaining:
                continue
            del self._export_pending_kinds[task.name]
            if task.name in self._awaiting_export:
                self._awaiting_export.discard(task.name)
                events.append(
                    ImageStateChanged(name=task.name, previous="VALIDATED", new="COMPLETED")
                )
            else:
                self._exports_ready.add(task.name)
        self.state.export_queue = self.export_queue.to_state_entries()
        return events

    def _flag_export_failure(self, task: ExportTask, failure: ExportFailure) -> list[SessionEvent]:
        """Flags the image as ERROR; any other pending tasks for it are cancelled."""
        self.export_queue.cancel(task.name)
        self._export_pending_kinds.pop(task.name, None)
        self._exports_ready.discard(task.name)
        self._awaiting_export.discard(task.name)
        self.state.error_images = [e for e in self.state.error_images if e.name != task.name]
        self.state.error_images.append(
            ErrorImage(name=task.name, code=failure.code, message=failure.message, kind=task.kind)
        )
        return [ImageErrored(name=task.name, code=failure.code, message=failure.message)]

    def retry_error_image(self, name: str) -> list[SessionEvent]:
        """Retries an image in ERROR state: re-runs all of its exports.

        Rebuilds the frame/orientation from the journal (`core.recovery`)
        since the image is usually no longer the current one. Does nothing
        (empty list) if the RAW or its frame can't be reconstructed — the
        `error_images` entry is kept as-is.
        """
        context = rebuild_export_context(name, self.paths, self.fs)
        if context is None:
            return []
        self.state.error_images = [e for e in self.state.error_images if e.name != name]
        self.enqueue_export_context(name, list(EXPORT_TASK_KINDS), context)
        events = self._drain_exports(self._new_deadline())
        if name in self._exports_ready:
            self._exports_ready.discard(name)
            events.append(ImageStateChanged(name=name, previous="ERROR", new="COMPLETED"))
        self._persist_state()
        return events

    def enqueue_export_context(self, name: str, kinds: list[str], context: ExportContext) -> None:
        """Queues export tasks with an already-known context (regeneration, crash recovery)."""
        self.export_queue.enqueue(name, kinds, context)
        self._export_pending_kinds[name] = set(kinds)

    def _log_export(self, task: ExportTask, result: ExportResult | None) -> None:
        """Logs an `EXPORT` journal entry: effective frame + scale factor."""
        details: dict[str, object] = {}
        if task.context is not None:
            details.update(
                x=task.context.x,
                y=task.context.y,
                width=task.context.width,
                height=task.context.height,
                angle_deg=task.context.angle_deg,
                orientation=task.context.orientation,
            )
        warn = False
        if result is not None:
            details["scale_factor"] = result.scale_factor
            if result.bounds_adjusted:
                details["bounds_adjusted"] = True
            warn = result.scale_factor > 1.0 or result.bounds_adjusted
        self.journal.log(
            "EXPORT", task.kind, image=task.name, level="warn" if warn else "info", details=details
        )

    def _mark_completed_if_ready(self, name: str, events: list[SessionEvent]) -> None:
        if name in self._exports_ready:
            self._exports_ready.discard(name)
            events.append(ImageStateChanged(name=name, previous="VALIDATED", new="COMPLETED"))
        else:
            self._awaiting_export.add(name)

    # --- validation ----------------------------------------------------------

    def _finalize_current_as_validated(self) -> list[SessionEvent]:
        """Validates the current image if there is one (triggered by the next arrival)."""
        current = self.state.current_image
        if current is None:
            return []
        return self.validate_current()

    def validate_current(self) -> list[SessionEvent]:
        """Validates the current image — used on next-image arrival, capture exit, and shutdown."""
        current = self.state.current_image
        if current is None:
            return []
        if current.state != "IN_REVIEW":
            raise IllegalTransitionError(current.state, "VALIDATED")

        name = current.assigned_name
        has_row = self.inventory.row(name) is not None
        if has_row:
            self.inventory.set_status(name, "done")
            self.journal.log(
                "CSV",
                "status",
                image=name,
                details={"row": name, "before": "todo", "after": "done"},
            )
        events: list[SessionEvent] = [
            ImageStateChanged(name=name, previous="IN_REVIEW", new="VALIDATED")
        ]
        self._mark_completed_if_ready(name, events)

        self.state.current_image = None
        if has_row:
            events.extend(self._save_inventory())
        self._persist_state()
        return events

    # --- rejection -------------------------------------------------------------

    def reject_current(self) -> list[SessionEvent]:
        """Rejects the current image (R key). Effects run in a specific, exact order."""
        current = self.state.current_image
        if current is None or current.state != "IN_REVIEW":
            raise IllegalTransitionError(current.state if current else "NONE", "REJECTED")

        name = current.assigned_name
        events: list[SessionEvent] = []

        cancelled = self.export_queue.cancel(name)
        self._export_pending_kinds.pop(name, None)
        self._exports_ready.discard(name)
        self._awaiting_export.discard(name)
        self.state.export_queue = self.export_queue.to_state_entries()

        deleted = self._delete_derivatives(name)

        camera_stem = Path(current.source_file).stem if current.source_file else name
        raw_path = self.paths.raw_dir / f"{name}{current.extension}"
        rejected_name = f"{camera_stem}__{name}{current.extension}"
        destination = self.paths.rejected_dir / rejected_name
        self.fs.rename(raw_path, destination)

        sidecar_path = self.paths.raw_dir / f"{name}.xmp"
        if self.fs.exists(sidecar_path):
            self.fs.rename(sidecar_path, self.paths.rejected_dir / f"{camera_stem}__{name}.xmp")

        has_row = self.inventory.row(name) is not None
        if has_row:
            self.inventory.set_source_file(name, "")
            before_cursor = self.inventory.cursor
            self.inventory.go_to_name(name)
            self.state.csv_cursor = self.inventory.cursor
            self.journal.log(
                "CSV",
                "cursor",
                details={
                    "before": before_cursor,
                    "after": self.inventory.cursor,
                    "cause": "rejected",
                },
            )

        self.journal.log(
            "REJECT",
            "rejected",
            image=name,
            details={
                "to": str(destination),
                "deleted_exports": deleted,
                "cancelled_tasks": [c.kind for c in cancelled],
            },
        )
        events.append(ImageRejected(name=name))
        events.append(ImageStateChanged(name=name, previous="IN_REVIEW", new="REJECTED"))

        self.state.current_image = None
        if has_row:
            events.extend(self._save_inventory())
        self._persist_state()
        return events

    def _delete_derivatives(self, name: str) -> list[str]:
        """Deletes already-produced (regenerable) derivatives for `name`."""
        deleted: list[str] = []
        for attr in _DERIVATIVE_DIRS:
            directory: Path = getattr(self.paths, attr)
            for entry in self.fs.list_dir(directory):
                if entry.stem == name:
                    self.fs.remove(entry)
                    deleted.append(str(entry))
        return deleted

    # --- orientation (V key) --------------------------------------------------

    def toggle_orientation(self) -> list[SessionEvent]:
        """Toggles portrait/landscape orientation of the current image (V key).

        Any export tasks already queued are cancelled and re-queued with the
        new orientation.
        """
        current = self.state.current_image
        if current is None or current.state != "IN_REVIEW":
            raise IllegalTransitionError(current.state if current else "NONE", "orientation")

        name = current.assigned_name
        before = current.orientation
        after = "vertical" if before == "horizontal" else "horizontal"
        current.orientation = after
        self.journal.log(
            "FRAMING",
            "orientation",
            image=name,
            details={"orientation": {"before": before, "after": after}},
        )
        events: list[SessionEvent] = [OrientationToggled(name=name, orientation=after)]

        self._export_pending_kinds.pop(name, None)
        self.export_queue.cancel(name)
        self._exports_ready.discard(name)
        self._enqueue_exports(name)
        events.extend(self._drain_exports(self._new_deadline()))

        self._persist_state()
        return events

    # --- frame -----------------------------------------------------------------

    def apply_frame(
        self,
        name: str,
        *,
        x: int,
        y: int,
        width: int,
        height: int,
        angle_deg: float,
        confidence: float,
        source: str,  # auto | manual | raw
        journal_action: str,  # auto | manual | raw | recomputed
        components: dict[str, float] | None = None,
        level: str | None = None,
    ) -> list[SessionEvent]:
        """Records a detected/recomputed/edited frame for the current image.

        Knows nothing about `imaging.framing` (primitive types only — `core`
        stays independent from the imaging pipeline); the caller (GUI)
        translates a `FrameResult` into plain parameters before calling this.
        """
        current = self.state.current_image
        if current is None or current.assigned_name != name or current.state != "IN_REVIEW":
            raise IllegalTransitionError(current.state if current else "NONE", "framing")

        current.framing = FramingState(
            x=x,
            y=y,
            width=width,
            height=height,
            angle_deg=angle_deg,
            confidence=confidence,
            source=source,
        )
        details: dict[str, object] = {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "angle_deg": angle_deg,
            "confidence": confidence,
        }
        if components:
            details["components"] = components
        self.journal.log("FRAMING", journal_action, image=name, details=details)

        events: list[SessionEvent] = [
            FramingApplied(name=name, source=source, level=level, confidence=confidence)
        ]
        # Always invalidates and re-queues, idempotently: the first call
        # (auto/raw) arrives *after* the synchronous export already
        # triggered by `_ingest_one()` with the default frame (frame
        # detection runs asynchronously via `PreviewWorker`), so it must
        # also regenerate the pixels — otherwise the detected frame would
        # never actually make it into the export.
        self._export_pending_kinds.pop(name, None)
        self.export_queue.cancel(name)
        self._exports_ready.discard(name)
        self._enqueue_exports(name)
        events.extend(self._drain_exports(self._new_deadline()))

        self._persist_state()
        return events

    # --- name conflict -----------------------------------------------------

    def resolve_conflict(self, option: int, *, new_name: str | None = None) -> list[SessionEvent]:
        """Resolves a pending name conflict (inline panel, not a popup)."""
        conflict = self._conflict
        if conflict is None:
            raise ValueError("no pending name conflict")

        if option == 1:
            events = self._resolve_conflict_rename_incoming(conflict, new_name)
        elif option == 2:
            events = self._resolve_conflict_replace_existing(conflict)
        elif option == 3:
            events = self._resolve_conflict_rename_existing(conflict, new_name)
        else:
            raise ValueError(f"invalid conflict resolution option: {option!r}")

        self._conflict = None
        return events

    def _resolve_conflict_rename_incoming(
        self, conflict: _PendingConflict, new_name: str | None
    ) -> list[SessionEvent]:
        target = new_name or f"{conflict.name}_BIS"
        validate_name(target)
        if find_conflicting_path(target, self.paths, self.fs) is not None:
            raise ValueError(f"Name already in use: {target!r}")

        self.journal.log(
            "NAMING",
            "conflict_resolved",
            details={"option": 1, "old": conflict.name, "new": target},
        )
        events: list[SessionEvent] = [NameConflictResolved(option=1, old=conflict.name, new=target)]
        events.extend(self._ingest_one(conflict.path, target, csv_row=None))
        return events

    def _resolve_conflict_replace_existing(self, conflict: _PendingConflict) -> list[SessionEvent]:
        stamp = self._now_wall.now().strftime("%Y%m%dT%H%M%S")
        for attr in ("raw_dir", *_DERIVATIVE_DIRS):
            directory: Path = getattr(self.paths, attr)
            for entry in self.fs.list_dir(directory):
                if entry.stem == conflict.name:
                    destination = self.paths.backup_dir / f"{stamp}__{entry.name}"
                    self.fs.rename(entry, destination)

        self.journal.log(
            "NAMING",
            "conflict_resolved",
            details={"option": 2, "old": conflict.name, "new": conflict.name},
        )
        events: list[SessionEvent] = [
            NameConflictResolved(option=2, old=conflict.name, new=conflict.name)
        ]
        events.extend(self._ingest_one(conflict.path, conflict.name, csv_row=conflict.name))
        return events

    def _resolve_conflict_rename_existing(
        self, conflict: _PendingConflict, new_name: str | None
    ) -> list[SessionEvent]:
        """Option 3: the existing file is renamed... into `BACKUP/`.

        A file in `RAW/` is never modified or renamed in place: only moves
        to `REJECTED/`/`BACKUP/` are legitimate. The `<NAME>_OLD` name
        becomes the file's name once it lands in `BACKUP/`.
        """
        target = new_name or f"{conflict.name}_OLD"
        validate_name(target)
        if find_conflicting_path(target, self.paths, self.fs) is not None:
            raise ValueError(f"Name already in use: {target!r}")

        for attr in ("raw_dir", *_DERIVATIVE_DIRS):
            directory: Path = getattr(self.paths, attr)
            for entry in self.fs.list_dir(directory):
                if entry.stem == conflict.name:
                    destination = self.paths.backup_dir / f"{target}{entry.suffix}"
                    self.fs.rename(entry, destination)

        self.journal.log(
            "NAMING",
            "conflict_resolved",
            details={"option": 3, "old": conflict.name, "new": target},
        )
        events: list[SessionEvent] = [NameConflictResolved(option=3, old=conflict.name, new=target)]
        events.extend(self._ingest_one(conflict.path, conflict.name, csv_row=conflict.name))
        return events

    # --- pause -----------------------------------------------------------------

    def pause(self) -> None:
        self.paused = True
        self.state.mode = "pause"
        self._persist_state()

    def resume(self) -> list[SessionEvent]:
        self.paused = False
        self.state.mode = "capture"
        for raw_path in self.state.pause_queue:
            self._pending_ingest.append(Path(raw_path))
        self.state.pause_queue = []
        events = self._drain_pending_ingest(self._new_deadline())
        self._persist_state()
        return events

    # --- self-monitoring (disk space / folder accessibility) -------------------

    def _check_autosurveillance(self, now: float) -> list[SessionEvent]:
        """Checks folder accessibility + free disk space, every 10 s during capture."""
        if (
            self._last_autosurveillance_check is not None
            and now - self._last_autosurveillance_check < _AUTOSURVEILLANCE_INTERVAL_S
        ):
            return []
        self._last_autosurveillance_check = now

        if not self._probe_accessible(self.paths.root):
            return self._enter_critical("E-02")
        if not self._probe_accessible(self.monitor.folder):
            return self._enter_critical("E-07")

        free_gb = self._safe_free_space_gb(self.paths.root)
        if free_gb is None:
            return []
        if free_gb < self._disk_critical_gb:
            return self._enter_critical("E-01", details={"free_gb": free_gb})
        if free_gb < self._disk_warn_gb:
            return self._warn_disk_space(free_gb)
        return []

    def _probe_accessible(self, folder: Path) -> bool:
        try:
            self.fs.touch_and_remove(folder / ".scanassistant_probe")
        except OSError:
            return False
        return True

    def _safe_free_space_gb(self, path: Path) -> float | None:
        try:
            return self.fs.free_space_gb(path)
        except OSError:
            return None

    def _safe_mtime(self, path: Path) -> float | None:
        try:
            return self.fs.stat(path).mtime
        except OSError:
            return None

    def _enter_critical(
        self, code: str, *, details: dict[str, object] | None = None
    ) -> list[SessionEvent]:
        if self._suspended_code == code:
            return []  # already flagged, don't spam on every tick
        self._suspended_code = code
        payload = details or {}
        self.journal.log(
            "SYSTEM", "error", level="error", details={"code": code, **payload}, result="error"
        )
        return [CriticalError(code=code, details=payload)]

    def _warn_disk_space(self, free_gb: float) -> list[SessionEvent]:
        if self._disk_warned:
            return []
        self._disk_warned = True
        self.journal.log(
            "SYSTEM", "error", level="warn", details={"code": "E-01", "free_gb": free_gb}
        )
        return [WarningEvent(code="E-01", details={"free_gb": free_gb})]

    def resume_from_critical(self) -> list[SessionEvent]:
        """Resumes from a critical suspension (disk space / folder accessibility)."""
        if self._suspended_code is None:
            return []
        code = self._suspended_code
        self._suspended_code = None
        self._disk_warned = False
        self._last_autosurveillance_check = None  # re-check immediately on the next pump()
        self.journal.log("SYSTEM", "resumed", details={"code": code})
        events: list[SessionEvent] = [CriticalResolved(code=code)]
        deadline = self._new_deadline()
        if not self.paused:
            for raw_path in self.state.pause_queue:
                self._pending_ingest.append(Path(raw_path))
            self.state.pause_queue = []
            events.extend(self._drain_pending_ingest(deadline))
        events.extend(self._drain_exports(deadline))
        self._persist_state()
        return events

    # --- inventory: writes + external modification detection -------------------

    def _save_inventory(self) -> list[SessionEvent]:
        """Writes `inventory.csv`, detecting any external modification first."""
        current_mtime = self._safe_mtime(self.paths.inventory_csv)
        if (
            not self._csv_write_suspended
            and self._inventory_mtime is not None
            and current_mtime is not None
            and current_mtime != self._inventory_mtime
        ):
            self._csv_write_suspended = True
            self.journal.log(
                "SYSTEM", "error", level="error", details={"code": "E-13"}, result="error"
            )
            return [CriticalError(code="E-13")]
        if self._csv_write_suspended:
            return []  # writes stay suspended until resolved (reload / overwrite)
        self.inventory.save(self.paths.inventory_csv)
        self._inventory_mtime = self._safe_mtime(self.paths.inventory_csv)
        return []

    def force_overwrite_inventory(self) -> list[SessionEvent]:
        """Overwrites the external modification with the app's own state."""
        if not self._csv_write_suspended:
            return []
        self._csv_write_suspended = False
        self.inventory.save(self.paths.inventory_csv)
        self._inventory_mtime = self._safe_mtime(self.paths.inventory_csv)
        self.journal.log("SYSTEM", "resumed", details={"code": "E-13"})
        return [CriticalResolved(code="E-13")]

    # --- navigation --------------------------------------------------------

    def go_to_previous_name(self) -> bool:
        return self.inventory.go_to_previous_todo()

    def go_to_next_name(self) -> bool:
        return self.inventory.go_to_next_todo()

    def go_to_name(self, name: str) -> None:
        self.inventory.go_to_name(name)

    # --- startup / shutdown ------------------------------------------------

    def initial_scan(self, *, process: set[str] | None = None) -> list[SessionEvent]:
        """Initial scan: files already present in the watched folder at startup.

        `process`: file names (relative to the watched folder) to process
        normally; every other candidate is ignored by default.
        """
        process = process or set()
        events: list[SessionEvent] = []
        extensions = {e.lower() for e in self.campaign.capture.extensions}
        candidates = [
            p for p in self.fs.list_dir(self.monitor.folder) if is_candidate_file(p, extensions)
        ]
        known_ignored = {f.name for f in self.state.ignored_files}
        to_seed: list[Path] = []

        for path in candidates:
            if path.name in known_ignored:
                continue
            if path.name in process:
                to_seed.append(path)
                continue
            try:
                stat = self.fs.stat(path)
            except OSError:
                continue
            self.state.ignored_files.append(
                IgnoredFile(name=path.name, size=stat.size, mtime=stat.mtime, reason="initial_scan")
            )
            self.journal.log(
                "CAPTURE", "ignored", details={"reason": "initial_scan", "path": str(path)}
            )

        if to_seed:
            self.monitor.seed(to_seed)
        self._persist_state()
        return events

    def stop(self) -> list[SessionEvent]:
        """Exits capture mode / clean shutdown: finalizes the current image.

        Fully drains the export queue, blocking (`deadline=None`), before
        leaving capture mode: unlike the periodic bounded drain in `pump()`,
        shutdown must wait for the queue to finish rather than abandon
        in-flight tasks.
        """
        events = self.validate_current()
        events.extend(self._drain_exports())
        self.state.mode = "preparation"
        self._persist_state()
        return events

    # --- introspection (used by the CLI) ------------------------------------

    @property
    def is_idle(self) -> bool:
        """True once no further processing can happen without a new event.

        Used by the CLI (`__main__.py`) to know when to stop automatically
        (CSV exhausted, nothing pending, no unresolved conflict).
        """
        return (
            not self._pending_ingest
            and self._conflict is None
            and self.state.current_image is None
            and self.inventory.is_exhausted()
        )

    # --- persistence -------------------------------------------------------

    def persist_state(self) -> None:
        """Writes `state.json` (public: used by `core.crash_recovery`)."""
        self._persist_state()

    def _persist_state(self) -> None:
        self.state.export_queue = self.export_queue.to_state_entries()
        save_state(self.state, self.paths.state_json)
