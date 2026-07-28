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
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from scanassistant.core.errors import (
    IllegalTransitionError,
    IntegrityCheckFailedError,
    NameConflictError,
)
from scanassistant.core.events import (
    CriticalError,
    CriticalResolved,
    FramingApplied,
    ImageDetected,
    ImageErrored,
    ImageIngested,
    ImageRejected,
    ImageRenamed,
    ImageStabilized,
    ImageStateChanged,
    NameConflictDetected,
    NameConflictResolved,
    RotationChanged,
    SessionEvent,
    StabilizationTimedOut,
)
from scanassistant.core.events import (
    Warning as WarningEvent,
)
from scanassistant.core.fs import FileSystem
from scanassistant.core.ingest import find_conflicting_path, ingest_file
from scanassistant.core.positive_review import reconstruct_content_framing_state
from scanassistant.core.queue import (
    EXPORT_TASK_KINDS,
    ContentFrameOutcome,
    ExportContext,
    ExportExecutor,
    ExportFailure,
    ExportQueue,
    ExportResult,
    ExportRunner,
    ExportTask,
    InlineExportExecutor,
    PooledExportExecutor,
)
from scanassistant.core.recovery import read_journal_entries, rebuild_export_context
from scanassistant.journal.journal import Journal
from scanassistant.metadata.xmp_sidecar import render_raw_sidecar
from scanassistant.project.campaign import Campaign
from scanassistant.project.inventory import (
    MAX_NAME_LENGTH,
    STATUS_COLUMN,
    Inventory,
    validate_name,
)
from scanassistant.project.layout import CampaignPaths
from scanassistant.project.positive_overrides import (
    PositiveOverride,
    load_positive_overrides,
    set_positive_print_overrides,
    write_positive_override,
)
from scanassistant.project.state import (
    ContentFramingState,
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


@dataclass(frozen=True)
class SessionHistoryEntry:
    """Snapshot of a finalized image, for the correction side panel.

    In-memory only (not persisted to `state.json`): lost if the app itself
    closes, but — carried across capture-mode stop/start by
    `CaptureScreen._session_history_by_root` — not just because the
    operator went back to the project screen and re-entered capture. Scoped
    to "the operator has this campaign open right now", not the campaign's
    entire history (that's the journal's job) nor a single capture-mode run.
    """

    name: str
    source_file: str
    extension: str
    rotation_deg: int
    framing: FramingState


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
        export_executor: ExportExecutor | None = None,
        positive_finalize_runner: ExportRunner | None = None,
        positive_finalize_executor: ExportExecutor | None = None,
        now_wall: type[datetime] = datetime,
        disk_warn_gb: float = 10.0,
        disk_critical_gb: float = 2.0,
        max_name_length: int = MAX_NAME_LENGTH,
        export_queue_warn_threshold: int = _EXPORT_QUEUE_WARN_THRESHOLD,
        session_history: list[SessionHistoryEntry] | None = None,
    ) -> None:
        self.paths = paths
        self.campaign = campaign
        self.inventory = inventory
        self.state = state
        self.journal = journal
        self.fs = fs
        self.monitor = monitor
        self.export_runner = export_runner
        # Inline (synchronous) by default: the CLI and every test rely on
        # this determinism. Only the live GUI wires in a `ThreadedExportExecutor`
        # (`gui.main_window`), so a slow export never freezes the Qt thread
        # (DECISIONS.md I-92/I-98).
        self.export_executor = export_executor or InlineExportExecutor()
        # Dedicated pool for jpeg_positive: measured at ~16.7s for a real
        # image (RAW decode + density-domain render) — routing it through
        # `self.export_executor` (the same single worker as tiff/jpeg_master)
        # would make the master export queue fall behind capture, the one
        # thing it must never do. `None` (the default: CLI, tests) keeps
        # jpeg_positive on the regular path, unchanged from before this
        # existed.
        self._positive_finalize_runner = positive_finalize_runner
        self._positive_finalize_executor = positive_finalize_executor
        self._now_wall = now_wall
        self._disk_warn_gb = disk_warn_gb
        self._disk_critical_gb = disk_critical_gb
        self._max_name_length = max_name_length
        self._export_queue_warn_threshold = export_queue_warn_threshold

        self.export_queue = ExportQueue.from_state_entries(state.export_queue)
        if isinstance(self._positive_finalize_executor, PooledExportExecutor):
            # Only once a real multi-worker pool is actually consuming
            # `jpeg_positive` tasks concurrently (`ExportQueue.
            # checkout_next`'s own docstring) — never unconditionally, or
            # every test/CLI configuration routing `jpeg_positive` through
            # a single executor would pay for a protection it has nothing
            # to protect against.
            self.export_queue.serialized_kinds = frozenset({"jpeg_positive"})
        # Carried over from a previous `CaptureSession` for this same
        # campaign (`gui.screens.capture.CaptureScreen` keeps it across a
        # stop/start capture-mode cycle, keyed by campaign root) — the
        # correction side panel is meant to span the whole time the
        # operator has this campaign open, not just one capture-mode run.
        self._session_history: list[SessionHistoryEntry] = list(session_history or [])
        self._pending_ingest: deque[Path] = deque()
        self._conflict: _PendingConflict | None = None
        self._export_pending_kinds: dict[str, set[str]] = {}
        self._exports_ready: set[str] = set()
        self._awaiting_export: set[str] = set()
        # Tasks superseded while still in flight (rotation/reframe/retry
        # re-queuing exports for a name that already has a task running, or
        # rejection): FIFO on a single-worker executor guarantees these
        # complete before anything queued after them even starts, so the
        # count of stale completions still owed for a name is known exactly
        # at re-queuing time (`_queue_exports`/`reject_current`). Consumed
        # in `_drain_exports`, which must not credit them to the fresh
        # pending-kinds tracking (`_export_pending_kinds`) set up afterwards.
        self._stale_completions: dict[str, int] = {}
        # Subset of the above where the file the stale task just wrote is
        # also an orphan to delete (rejection only — rotation/reframe let
        # the fresh, correct export overwrite it naturally).
        self._stale_completions_cleanup: set[str] = set()
        self._exhaustion_signaled = False
        self.paused = state.mode == "pause"
        # Set by `reopen_for_correction` when *it* is the one pausing capture
        # (i.e. the operator wasn't already paused) — lets `validate_current`/
        # `reject_current` lift that pause automatically once the reopened
        # slot is closed again, instead of leaving capture paused forever.
        # Not persisted: on a crash mid-correction, capture simply stays
        # paused after recovery (same as today), rather than resuming into a
        # state the operator never confirmed.
        self._correction_auto_paused = False
        self._suspended_code: str | None = None
        self._disk_warned = False
        self._queue_growth_warned = False
        self._last_autosurveillance_check: float | None = None
        self._csv_write_suspended = False
        self._inventory_mtime = self._safe_mtime(paths.inventory_csv)
        self._reconcile_export_queue_context()

    def _reconcile_export_queue_context(self) -> None:
        """`self.export_queue`, just loaded from `state.json`, holds tasks
        without an `ExportContext` — `ExportQueueEntry` only ever persists
        `(name, kinds)`, never the geometry. Left as-is, such a task can't
        regenerate anything when it runs (`ExportRunner.run` now returns an
        explicit failure for it rather than a silent no-op).

        Rebuilds each pending entry's context from the journal before
        anything is submitted to a worker — the same reconstruction
        `core.crash_recovery._rebuild_pending_export_queue` already does
        after an unclean shutdown, but run unconditionally on every
        construction: a queue can just as well be non-empty after an
        ordinary stop/reopen, since exports drain on a background thread
        and nothing currently guarantees the queue is flushed before the
        campaign closes.
        """
        entries = self.export_queue.to_state_entries()
        if not entries:
            return
        journal_entries = read_journal_entries(self.paths, self.fs)
        for entry in entries:
            self.export_queue.cancel(entry.name)
        for entry in entries:
            context = rebuild_export_context(
                entry.name,
                self.paths,
                self.fs,
                self.campaign.capture.extensions,
                entries=journal_entries,
            )
            if context is None:
                continue  # RAW gone or never framed: nothing to rebuild
            self.enqueue_export_context(entry.name, entry.tasks, context)
        self.state.export_queue = self.export_queue.to_state_entries()

    # --- main loop -------------------------------------------------------

    def pump(
        self, now: float, *, before_finalize_current: Callable[[], None] | None = None
    ) -> list[SessionEvent]:
        """Advance monitoring and processing by one tick; returns the resulting events.

        `before_finalize_current`, if given, runs immediately before this
        tick implicitly finalizes whatever image is still `current` (a new
        stabilized file bumping it) — never on ticks where that doesn't
        happen. The GUI uses this to flush a debounced rotation/crop edit
        exactly when it's about to matter, not on every tick regardless
        (which would defeat the debounce it's there to provide): the edit
        lives only in the GUI's own state until committed, so skipping the
        flush here would export the stale, pre-edit value.
        """
        events: list[SessionEvent] = []
        deadline = self._new_deadline()
        events.extend(self._check_autosurveillance(now))
        for monitor_event in self.monitor.tick(now):
            events.extend(self._handle_monitor_event(monitor_event))
        events.extend(
            self._drain_pending_ingest(deadline, before_finalize_current=before_finalize_current)
        )
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
        if size > self._export_queue_warn_threshold:
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

    def _drain_pending_ingest(
        self,
        deadline: float | None = None,
        *,
        before_finalize_current: Callable[[], None] | None = None,
    ) -> list[SessionEvent]:
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

            events.extend(
                self._ingest_one(
                    source_path,
                    name,
                    csv_row=name,
                    deadline=deadline,
                    before_finalize_current=before_finalize_current,
                )
            )
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
        before_finalize_current: Callable[[], None] | None = None,
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

        if before_finalize_current is not None:
            before_finalize_current()
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
            # Campaign default is only a starting point; V then cycles the
            # image's own rotation freely (0/90/180/270), independently.
            rotation_deg=90 if self.campaign.framing.default_orientation == "vertical" else 0,
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

        # No export queued here: the archival tiff/jpeg_master/jpeg_positive
        # only need to reflect the *final* framing/rotation the operator
        # settles on, never an intermediate one — queuing now (the default,
        # un-detected frame) only to immediately re-queue again once auto-
        # detection or a manual edit lands would waste a full render that's
        # thrown away before it's ever looked at. `validate_current()` is
        # the single point that actually queues, once, right before the
        # image is left for good.
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
        self._queue_exports(name, kinds, self._build_export_context(name))

    def _queue_exports(self, name: str, kinds: list[str], context: ExportContext | None) -> None:
        """Queues export tasks for `name`, (re)setting its pending-kinds tracker.

        If a task for `name` is still in flight from a previous, now
        superseded queuing (rotation/reframe/retry before the first export
        finished), its eventual completion must not be credited against
        *this* fresh set of pending kinds — `_drain_exports` discards it
        instead (`_stale_completions`).

        `in_flight_count(name)` already includes any task marked stale by an
        *earlier* superseded queuing that hasn't completed yet (two rotations
        in a row before the first one's exports finish) — so the tracker is
        set to that count, not incremented by it, to avoid double-counting.
        """
        in_flight = self.export_queue.in_flight_count(name)
        if in_flight > self._stale_completions.get(name, 0):
            self._stale_completions[name] = in_flight
        self.export_queue.enqueue(name, kinds, context)
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
            rotation_deg=current.rotation_deg,
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

    def _executor_and_runner_for(self, kind: str) -> tuple[ExportExecutor, ExportRunner]:
        """Routes `jpeg_positive` to the dedicated finalize pool when one was
        configured — every other kind stays on the regular single-worker
        path."""
        if (
            kind == "jpeg_positive"
            and self._positive_finalize_executor is not None
            and self._positive_finalize_runner is not None
        ):
            return self._positive_finalize_executor, self._positive_finalize_runner
        return self.export_executor, self.export_runner

    def _drain_exports(
        self, deadline: float | None = None, *, wait: bool = True
    ) -> list[SessionEvent]:
        """Hands pending export tasks to `self.export_executor` and processes
        whatever it has completed so far (real `ExportRunner` or
        `FakeExportRunner`, inline or on a background thread).

        Bounded by `deadline` (submission only — never the work itself):
        whatever's left in the backlog is submitted on the next call.
        `state.export_queue` keeps an exact record of both the backlog and
        anything still in flight, so a crash never drops a task that is
        merely still running (`ExportQueue.to_state_entries()`).

        """
        events: list[SessionEvent] = []
        if self._suspended_code is not None:
            # Critical suspension: the queue stays as-is, resumed on the next
            # non-suspended `pump()` (or explicitly via `resume_from_critical`).
            self.state.export_queue = self.export_queue.to_state_entries()
            return events

        submitted_any = False
        while self.export_queue.has_backlog():
            if deadline is not None and submitted_any and time.monotonic() >= deadline:
                break
            task = self.export_queue.checkout_next()
            if task is None:
                # Everything left in the backlog is a `jpeg_positive` task
                # blocked behind a same-name one still in flight
                # (`ExportQueue.checkout_next`'s own docstring) — nothing
                # more to submit until that one completes and this is
                # called again.
                break
            executor, runner = self._executor_and_runner_for(task.kind)
            executor.submit(task, runner)
            submitted_any = True

        if deadline is None and wait:
            # Campaign shutdown: must wait for the queue to finish rather
            # than abandon in-flight tasks (unchanged contract, I-83) —
            # unless the caller explicitly opted out (`wait=False`,
            # `processing.drain_on_exit`).
            self.export_executor.wait_idle()
            if self._positive_finalize_executor is not None:
                self._positive_finalize_executor.wait_idle()

        completed = list(self.export_executor.collect_completed())
        if self._positive_finalize_executor is not None:
            completed += self._positive_finalize_executor.collect_completed()
        for task, result in completed:
            self.export_queue.complete(task)
            stale_count = self._stale_completions.get(task.name)
            if stale_count:
                remaining_stale = stale_count - 1
                if remaining_stale > 0:
                    self._stale_completions[task.name] = remaining_stale
                else:
                    del self._stale_completions[task.name]
                if task.name in self._stale_completions_cleanup:
                    # Rejected while this task was already in flight: the
                    # file it just wrote (if any) is an orphan for an image
                    # that no longer exists in this form.
                    self._delete_derivatives(task.name)
                    if remaining_stale == 0:
                        self._stale_completions_cleanup.discard(task.name)
                continue
            if isinstance(result, ExportFailure):
                events.extend(self._flag_export_failure(task, result))
                continue
            # A successful export makes a previously recorded error for
            # this exact (name, kind) stale — matched by kind too, unlike
            # `_flag_export_failure`'s name-only clear, so a tiff failure
            # from the same capture batch isn't wiped out by an unrelated
            # jpeg_positive success completing right after it.
            self.state.error_images = [
                e
                for e in self.state.error_images
                if not (e.name == task.name and e.kind == task.kind)
            ]
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
        context = rebuild_export_context(
            name, self.paths, self.fs, self.campaign.capture.extensions
        )
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
        self._queue_exports(name, kinds, context)

    def regenerate_positive(self, name: str) -> list[SessionEvent]:
        """Re-runs only the `jpeg_positive` export for `name` — never `tiff`/
        `jpeg_master`, whose geometry (the support frame) this never
        changes. For the "Recadrage des positifs" screen: adjusting the
        content frame or exposure for one image must not re-touch its
        master derivatives.

        Rebuilds the frame from the journal (`core.recovery`), same as
        `retry_error_image` — works whether or not `name` is still the
        current image. Does nothing (empty list) if the RAW or its frame
        can't be reconstructed.
        """
        context = rebuild_export_context(
            name, self.paths, self.fs, self.campaign.capture.extensions
        )
        if context is None:
            return []
        self.enqueue_export_context(name, ["jpeg_positive"], context)
        events = self._drain_exports(self._new_deadline())
        self._persist_state()
        return events

    def apply_manual_print_overrides(
        self,
        name: str,
        *,
        dmin: tuple[float, float, float] | None = None,
        exposure_shift: float | None = None,
        contrast: float | None = None,
        paper_black: float | None = None,
        paper_soft_clip: float | None = None,
    ) -> list[SessionEvent]:
        """Regenerates `jpeg_positive` for `name` using an operator's manual
        print_engine overrides from the calibration screen — each `None`
        means that group stays automatic. Persisted (`project.
        positive_overrides.set_positive_print_overrides`) — never touches
        TIFF/JPEG master.

        Same journal-rebuild + jpeg_positive-only scope as
        `regenerate_positive`; does nothing (empty list) if the RAW or its
        support frame can't be reconstructed. Touches only the tonal half
        of the entry — the crop (`print_content_frame`) is preserved as-is,
        whatever it currently is, never reset to auto by a tonal-only call;
        setting the crop itself is the calibration screen's own direct
        `project.positive_overrides.set_positive_print_overrides` call."""
        context = rebuild_export_context(
            name, self.paths, self.fs, self.campaign.capture.extensions
        )
        if context is None:
            return []
        existing = load_positive_overrides(self.paths, self.fs).get(name)
        content_frame = existing.print_content_frame if existing else None
        content_frame_angle_deg = existing.print_content_frame_angle_deg if existing else 0.0
        set_positive_print_overrides(
            self.paths,
            self.fs,
            name,
            dmin=dmin,
            exposure_shift=exposure_shift,
            contrast=contrast,
            paper_black=paper_black,
            paper_soft_clip=paper_soft_clip,
            content_frame=content_frame,
            content_frame_angle_deg=content_frame_angle_deg,
        )
        context = replace(
            context,
            manual_print_dmin=dmin,
            manual_print_exposure_shift=exposure_shift,
            manual_print_contrast=contrast,
            manual_print_paper_black=paper_black,
            manual_print_paper_soft_clip=paper_soft_clip,
            manual_print_content_frame=content_frame,
            manual_print_content_frame_angle_deg=content_frame_angle_deg,
        )
        self.enqueue_export_context(name, ["jpeg_positive"], context)
        events = self._drain_exports(self._new_deadline())
        self._persist_state()
        return events

    def rotate_reviewed_image(self, name: str, *, direction: int = 1) -> list[SessionEvent]:
        """Corrects a finalized image's 90° orientation from the positive
        calibration screen (`direction=1` clockwise, `-1` counter-
        clockwise, cycling through 0/90/180/270 — same convention as
        `rotate_current`'s V/Shift+V) — for a rotation missed during
        capture, caught only once the operator judges tone/framing later.
        Unlike `apply_manual_print_overrides`/`regenerate_positive`,
        re-runs all three derivatives (tiff/jpeg_master/jpeg_positive): the
        support frame's own orientation, unlike a content-frame or tonal
        change, touches every one of them.

        Any content-frame crop already confirmed for this image is cleared
        (tonal settings are kept): its fractions are relative to the
        support frame's own output dimensions, which swap on a 90°/270°
        change — keeping it would silently misalign the print instead of
        falling back to a fresh automatic detection.

        Rebuilds the frame from the journal (`core.recovery`), same as
        `regenerate_positive`; does nothing (empty list) if the RAW or its
        support frame can't be reconstructed.
        """
        context = rebuild_export_context(
            name, self.paths, self.fs, self.campaign.capture.extensions
        )
        if context is None:
            return []
        before = context.rotation_deg
        after = (before + 90 * direction) % 360
        self.journal.log(
            "FRAMING",
            "rotation",
            image=name,
            details={"rotation_deg": {"before": before, "after": after}},
        )
        existing = load_positive_overrides(self.paths, self.fs).get(name)
        set_positive_print_overrides(
            self.paths,
            self.fs,
            name,
            dmin=existing.print_dmin if existing else None,
            exposure_shift=existing.print_exposure_shift if existing else None,
            contrast=existing.print_contrast if existing else None,
            paper_black=existing.print_paper_black if existing else None,
            paper_soft_clip=existing.print_paper_soft_clip if existing else None,
            content_frame=None,
            content_frame_angle_deg=0.0,
        )
        context = replace(context, rotation_deg=after)
        self.enqueue_export_context(name, list(EXPORT_TASK_KINDS), context)
        events = self._drain_exports(self._new_deadline())
        events.append(RotationChanged(name=name, rotation_deg=after))
        self._persist_state()
        return events

    def propagate_print_overrides(
        self, source_name: str, target_names: list[str], *, include_dmin: bool = False
    ) -> list[SessionEvent]:
        """Copies `source_name`'s persisted print_engine overrides to every
        name in `target_names` ("Apply to selection") — `source_name` must
        already have a confirmed override (usually via
        `apply_manual_print_overrides` on it first); nothing to propagate
        otherwise (empty list).

        Dmin excluded by default (`include_dmin`): a physical measurement
        local to *that* negative's own border — propagating it without
        discernment would reintroduce a color cast rather than correct one.
        Paper-model settings (contrast, exposure, black, soft-clip) are an
        aesthetic choice, consistent to propagate across one film/box.

        Regenerates `jpeg_positive` for exactly the targets that could be
        rebuilt, journals a dedicated event listing them (whether or not
        every target succeeded), and silently skips any name whose RAW or
        support frame can't be reconstructed rather than aborting the
        whole batch."""
        source = load_positive_overrides(self.paths, self.fs).get(source_name)
        if source is None:
            return []
        events: list[SessionEvent] = []
        applied: list[str] = []
        for target in target_names:
            # Not `if apply_manual_print_overrides(...):` — a successful
            # regeneration of an image that's no longer current produces no
            # SessionEvent at all (no VALIDATED->COMPLETED transition to
            # report), so an empty return doesn't mean "skipped". Checking
            # the context directly is the only reliable signal here.
            context = rebuild_export_context(
                target, self.paths, self.fs, self.campaign.capture.extensions
            )
            if context is None:
                continue
            applied.append(target)
            events.extend(
                self.apply_manual_print_overrides(
                    target,
                    dmin=source.print_dmin if include_dmin else None,
                    exposure_shift=source.print_exposure_shift,
                    contrast=source.print_contrast,
                    paper_black=source.print_paper_black,
                    paper_soft_clip=source.print_paper_soft_clip,
                )
            )
        self.journal.log(
            "POSITIVE_CALIBRATION",
            "propagated",
            details={"source": source_name, "targets": applied, "include_dmin": include_dmin},
        )
        return events

    def restore_positive_override(
        self, name: str, override: PositiveOverride | None
    ) -> list[SessionEvent]:
        """Replaces `name`'s override wholesale with an exact prior snapshot
        and regenerates `jpeg_positive` — the calibration screen's Ctrl+Z/
        Ctrl+Y primitive: unlike `apply_manual_print_overrides`, which only
        ever touches its own half of the entry, this overwrites the whole
        entry at once from a snapshot the caller captured before the change
        being undone. `override=None` removes the entry entirely (undoing
        the very first override ever set for `name`).

        Does nothing (empty list) if the RAW or its support frame can't be
        reconstructed, same as every other regeneration entry point."""
        context = rebuild_export_context(
            name, self.paths, self.fs, self.campaign.capture.extensions
        )
        if context is None:
            return []
        write_positive_override(self.paths, self.fs, name, override)
        context = replace(
            context,
            manual_print_dmin=override.print_dmin if override else None,
            manual_print_exposure_shift=override.print_exposure_shift if override else None,
            manual_print_contrast=override.print_contrast if override else None,
            manual_print_paper_black=override.print_paper_black if override else None,
            manual_print_paper_soft_clip=override.print_paper_soft_clip if override else None,
            manual_print_content_frame=override.print_content_frame if override else None,
            manual_print_content_frame_angle_deg=(
                override.print_content_frame_angle_deg if override else 0.0
            ),
        )
        self.enqueue_export_context(name, ["jpeg_positive"], context)
        events = self._drain_exports(self._new_deadline())
        self._persist_state()
        return events

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
                rotation_deg=task.context.rotation_deg,
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
        if task.kind == "jpeg_positive":
            self._log_positive_framing(task.name, result.content_frame if result else None)

    def _log_positive_framing(self, name: str, content_frame: ContentFrameOutcome | None) -> None:
        """Logs `POSITIVE_FRAMING` on **every** `jpeg_positive` export, applied
        or not (03 §4): a reviewer tool needs to tell "processed, nothing to
        flag" apart from "never processed", which it can't do from an absent
        entry alone. Also updates `state.json` for the image still current —
        once it's moved to history, the journal is the only durable copy."""
        if content_frame is not None:
            # "manual" (operator-confirmed, either an explicit override or
            # simply having viewed the image on the calibration screen and
            # moved on) always wins: once an operator has looked at an
            # image, it must not keep reappearing in the "needs review"
            # list just because the automatic tonal estimate is out of its
            # confidence range — that estimate was already on screen for
            # the operator to judge. `tonal_flagged` only demotes an
            # otherwise-automatic ("applied") outcome to "deferred".
            if content_frame.source == "manual":
                outcome = "manual"
            elif content_frame.tonal_flagged:
                outcome = "deferred"
            else:
                outcome = "applied"
            state = ContentFramingState(
                x=content_frame.x,
                y=content_frame.y,
                width=content_frame.width,
                height=content_frame.height,
                fill=content_frame.fill,
                area_ratio=content_frame.area_ratio,
                outcome=outcome,
                content_frame_fraction=content_frame.fraction,
                angle_deg=content_frame.angle_deg,
            )
        else:
            state = ContentFramingState(outcome="deferred")
        if self.state.current_image is not None and self.state.current_image.assigned_name == name:
            self.state.current_image.content_framing = state
        self.journal.log(
            "POSITIVE_FRAMING",
            state.outcome,
            image=name,
            details={
                "x": state.x,
                "y": state.y,
                "width": state.width,
                "height": state.height,
                "fill": state.fill,
                "area_ratio": state.area_ratio,
                "content_frame_fraction": state.content_frame_fraction,
                "angle_deg": state.angle_deg,
            },
        )

    def mark_positive_reviewed(
        self,
        name: str,
        *,
        x: int,
        y: int,
        width: int,
        height: int,
        content_frame_fraction: tuple[float, float, float, float] | None,
        angle_deg: float,
    ) -> None:
        """Marks `name` as operator-reviewed on the calibration screen
        without a `jpeg_positive` re-render: the operator looked at whatever
        was already on screen and moved on without changing anything, so
        the pixels already on disk are still exactly right — only the
        `POSITIVE_FRAMING` outcome needs to flip to `manual` so the image
        stops reappearing in the "needs review" list. `x`/`y`/`width`/
        `height`/`content_frame_fraction`/`angle_deg` are the geometry
        already shown (the calibration screen's own `_current_print_frame`),
        not recomputed here — paying for a full RAW decode + density render
        just to reproduce an unchanged result is exactly what
        `regenerate_positive` exists to avoid for a real edit."""
        state = ContentFramingState(
            x=x,
            y=y,
            width=width,
            height=height,
            outcome="manual",
            content_frame_fraction=content_frame_fraction,
            angle_deg=angle_deg,
        )
        if self.state.current_image is not None and self.state.current_image.assigned_name == name:
            self.state.current_image.content_framing = state
        self.journal.log(
            "POSITIVE_FRAMING",
            "manual",
            image=name,
            details={
                "x": state.x,
                "y": state.y,
                "width": state.width,
                "height": state.height,
                "fill": state.fill,
                "area_ratio": state.area_ratio,
                "content_frame_fraction": state.content_frame_fraction,
                "angle_deg": state.angle_deg,
            },
        )
        self._persist_state()

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
        """Validates the current image — used on next-image arrival, capture exit, and shutdown.

        The single point that actually queues the archival exports (tiff/
        jpeg_master/jpeg_positive): `apply_frame()`/`set_rotation()` only
        ever update `current.framing`/`.rotation_deg` and the journal, so by
        the time this runs the operator has already settled on a final
        value — queuing here, once, avoids a full re-render for every
        intermediate detection/edit made while still reviewing the image.
        """
        current = self.state.current_image
        if current is None:
            return []
        if current.state != "IN_REVIEW":
            raise IllegalTransitionError(current.state, "VALIDATED")

        name = current.assigned_name
        row = self.inventory.row(name)
        has_row = row is not None
        if row is not None:
            before_status = row[STATUS_COLUMN]
            self.inventory.set_status(name, "done")
            self.journal.log(
                "CSV",
                "status",
                image=name,
                details={"row": name, "before": before_status, "after": "done"},
            )
        events: list[SessionEvent] = [
            ImageStateChanged(name=name, previous="IN_REVIEW", new="VALIDATED")
        ]
        self._enqueue_exports(name)
        events.extend(self._drain_exports(self._new_deadline()))
        self._mark_completed_if_ready(name, events)

        self._record_session_history(current)
        self.state.current_image = None
        events.extend(self._resume_if_correction_auto_paused())
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
        in_flight = self.export_queue.in_flight_count(name)
        if in_flight:
            # Already running on the executor (possibly mid-write): can't be
            # cancelled from here — cleaned up in `_drain_exports` instead,
            # once its (now orphan) completion comes in. `in_flight_count`
            # already includes anything marked stale by an earlier
            # superseded queuing (see `_queue_exports`), hence `max`, not `+=`.
            self._stale_completions[name] = max(self._stale_completions.get(name, 0), in_flight)
            self._stale_completions_cleanup.add(name)
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

        row = self.inventory.row(name)
        has_row = row is not None
        if row is not None:
            before_status = row[STATUS_COLUMN]
            if before_status != "todo":
                # Rejecting an image reopened via `reopen_for_correction`
                # (I-88): its row is still `done`, untouched by the reopen
                # itself. `go_to_name` below requires `todo`, and F-11 says
                # a rejected name always returns to the reserve regardless
                # of how it got here.
                self.inventory.set_status(name, "todo")
                self.journal.log(
                    "CSV",
                    "status",
                    image=name,
                    details={"row": name, "before": before_status, "after": "todo"},
                )
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
        events.extend(self._resume_if_correction_auto_paused())
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

    # --- rename (menu-only in full mode; primary interaction in simple mode) ---

    def _vacate_row(self, name: str) -> None:
        """Resets `name`'s inventory row (if any) to `todo` — status and
        source file both — for a name whose files no longer sit under it
        (renamed away, or just backed up out of the way): the document
        hasn't actually been captured under this name after all, so the
        row must be free to be consumed normally the next time something
        does land under it, not raise `add_free_name`'s "already exists"
        guard."""
        row = self.inventory.row(name)
        if row is None:
            return
        before_status = row[STATUS_COLUMN]
        if before_status != "todo":
            self.inventory.set_status(name, "todo")
            self.journal.log(
                "CSV",
                "status",
                image=name,
                details={"row": name, "before": before_status, "after": "todo"},
            )
        self.inventory.set_source_file(name, "")

    def _backup_matching_files(self, name: str, *, destination_name: str | None = None) -> None:
        """Moves every file named `name` across RAW/TIFF/JPEG_* to
        `BACKUP/` — same non-negotiable as a rejection: a file already in
        `RAW/` is never modified or deleted, only moved. `destination_name`
        gives the backup an explicit name (`<name>_OLD`, operator-chosen);
        without it, a timestamp prefix keeps it unique instead."""
        stamp = self._now_wall.now().strftime("%Y%m%dT%H%M%S")
        for attr in ("raw_dir", *_DERIVATIVE_DIRS):
            directory: Path = getattr(self.paths, attr)
            for entry in self.fs.list_dir(directory):
                if entry.stem != name:
                    continue
                destination = (
                    self.paths.backup_dir / f"{destination_name}{entry.suffix}"
                    if destination_name is not None
                    else self.paths.backup_dir / f"{stamp}__{entry.name}"
                )
                self.fs.rename(entry, destination)

    def rename_current(
        self,
        new_name: str,
        *,
        replace_existing: bool = False,
        backup_existing_as: str | None = None,
    ) -> list[SessionEvent]:
        """Renames the current in-review image (RAW + sidecar + any already-
        produced derivatives, all moved in place — never modified in
        content). Raises `IllegalTransitionError` if there is no current
        image or it isn't `IN_REVIEW`; `ValueError` for every operator-input
        problem (identical name, invalid name, name already in use).

        `replace_existing`/`backup_existing_as`: the operator's explicit
        resolution once a first call raises for an already-used `new_name`
        — the GUI's duplicate-name panel, reused here from the incoming-
        file conflict flow (`resolve_conflict`) for the same reason: an
        operator reusing a name on purpose (redoing a bad shot) needs an
        explicit way to say so, not a dead end. `replace_existing` moves
        whatever already carries `new_name` (RAW/TIFF/JPEG_*) to `BACKUP/`
        under a timestamped name; `backup_existing_as` moves it there
        instead under an explicit name the operator chose (typically
        `<new_name>_OLD`) — `replace_existing` wins if both are given.
        Either way the freed name's own inventory row (if any) reverts to
        `todo` first, so it's consumed normally below rather than hitting
        `add_free_name`'s own "already exists" guard.
        """
        current = self.state.current_image
        if current is None or current.state != "IN_REVIEW":
            raise IllegalTransitionError(current.state if current else "NONE", "renamed")

        old_name = current.assigned_name
        if new_name == old_name:
            raise ValueError("new name is identical to the current name")
        validate_name(new_name, max_name_length=self._max_name_length)
        conflict_path = find_conflicting_path(new_name, self.paths, self.fs)
        if conflict_path is not None:
            if replace_existing:
                self._backup_matching_files(new_name)
            elif backup_existing_as is not None:
                validate_name(backup_existing_as, max_name_length=self._max_name_length)
                if find_conflicting_path(backup_existing_as, self.paths, self.fs) is not None:
                    raise ValueError(f"Name already in use: {backup_existing_as!r}")
                self._backup_matching_files(new_name, destination_name=backup_existing_as)
            else:
                raise NameConflictError(new_name, str(conflict_path))
            self._vacate_row(new_name)
            self.journal.log(
                "NAMING",
                "rename_conflict_resolved",
                image=new_name,
                details={"backed_up_as": backup_existing_as},
            )

        events: list[SessionEvent] = []

        # Neutralize in-flight/queued exports for `old_name` — identical to
        # `reject_current`'s handling, and always a no-op in simple mode
        # (nothing is ever queued there).
        exports_were_ready = old_name in self._exports_ready
        cancelled = self.export_queue.cancel(old_name)
        in_flight = self.export_queue.in_flight_count(old_name)
        if in_flight:
            self._stale_completions[old_name] = max(
                self._stale_completions.get(old_name, 0), in_flight
            )
            self._stale_completions_cleanup.add(old_name)
        self._export_pending_kinds.pop(old_name, None)
        self._exports_ready.discard(old_name)
        self._awaiting_export.discard(old_name)
        self.state.export_queue = self.export_queue.to_state_entries()

        # RAW + sidecar: always a move, never a modification of content.
        self.fs.rename(
            self.paths.raw_dir / f"{old_name}{current.extension}",
            self.paths.raw_dir / f"{new_name}{current.extension}",
        )
        old_sidecar = self.paths.raw_dir / f"{old_name}.xmp"
        if self.fs.exists(old_sidecar):
            self.fs.rename(old_sidecar, self.paths.raw_dir / f"{new_name}.xmp")

        renamed_outputs = self._rename_derivatives(old_name, new_name)

        # Old row: vacated, exactly like a rejection (F-11) — the document
        # hasn't actually been captured under this name after all.
        old_row = self.inventory.row(old_name)
        if old_row is not None:
            self._vacate_row(old_name)
            before_cursor = self.inventory.cursor
            self.inventory.go_to_name(old_name)
            self.state.csv_cursor = self.inventory.cursor
            self.journal.log(
                "CSV",
                "cursor",
                details={
                    "before": before_cursor,
                    "after": self.inventory.cursor,
                    "cause": "renamed",
                },
            )

        # Target row: consumed like a normal ingestion if it's a pending
        # inventory row (same fork as `_resolve_conflict_rename_incoming`),
        # otherwise added as a free name — the CSV must end up reflecting
        # what was actually digitized, not left out of sync.
        target_row = self.inventory.row(new_name)
        if target_row is not None and target_row[STATUS_COLUMN] == "todo":
            self.inventory.set_source_file(new_name, current.source_file)
            before_cursor = self.inventory.cursor
            self.inventory.go_to_name(new_name)
            self.inventory.advance_to_next_todo()
            self.state.csv_cursor = self.inventory.cursor
            self.journal.log(
                "CSV",
                "cursor",
                details={
                    "before": before_cursor,
                    "after": self.inventory.cursor,
                    "cause": "renamed",
                },
            )
        else:
            self.inventory.add_free_name(new_name, current.source_file)
            self.journal.log("CSV", "row_added_live", details={"name": new_name})

        current.assigned_name = new_name
        if exports_were_ready:
            self._exports_ready.add(new_name)

        self.journal.log(
            "NAMING",
            "renamed",
            image=new_name,
            details={
                "old": old_name,
                "new": new_name,
                "outputs_renamed": renamed_outputs,
                "cancelled_tasks": [c.kind for c in cancelled],
            },
        )
        events.append(ImageRenamed(old=old_name, new=new_name))

        events.extend(self._save_inventory())
        self._persist_state()
        return events

    def _rename_derivatives(self, old_name: str, new_name: str) -> list[str]:
        """Moves already-produced (never regenerated) derivatives for
        `old_name` to `new_name` — not `_delete_derivatives`'s simpler
        `entry.stem == name` match: `JPEG_POSITIVE/<name><suffix>.jpg`'s stem
        is `<name><suffix>`, never `== name`, so that directory needs the
        suffix-aware form too."""
        suffix = self.campaign.exports.jpeg_positive.suffix
        renamed: list[str] = []
        for attr in _DERIVATIVE_DIRS:
            directory: Path = getattr(self.paths, attr)
            for entry in self.fs.list_dir(directory):
                if entry.stem == old_name:
                    destination = directory / f"{new_name}{entry.suffix}"
                elif attr == "jpeg_positive_dir" and entry.stem == f"{old_name}{suffix}":
                    destination = directory / f"{new_name}{suffix}{entry.suffix}"
                else:
                    continue
                self.fs.rename(entry, destination)
                renamed.append(str(destination))
        return renamed

    def resolve_rename_conflict(
        self, new_name: str, option: int, *, alternate_name: str | None = None
    ) -> list[SessionEvent]:
        """Resolves a `rename_current(new_name)` call that raised
        `NameConflictError` — the same inline duplicate-name panel
        `resolve_conflict` shows for an incoming file's naming conflict,
        reused here for the currently-reviewed image. `new_name` is the
        name that conflicted; `alternate_name` is whatever the operator
        typed into the panel's option 1/3 field, same convention as
        `resolve_conflict`.

        Option 1: renames the current image to `alternate_name` instead
        (a different name, sidestepping the conflict entirely — the
        `rename_current`/`_BIS` counterpart to "rename incoming" in the
        ingest-conflict flow, applied to the image already being renamed
        rather than one arriving). Option 2: replaces whatever already
        carries `new_name` (backed up to `BACKUP/`). Option 3: backs up
        whatever already carries `new_name` under `alternate_name`
        instead (typically `<new_name>_OLD`), then uses `new_name`.
        """
        if option == 1:
            return self.rename_current(alternate_name or f"{new_name}_BIS")
        if option == 2:
            return self.rename_current(new_name, replace_existing=True)
        if option == 3:
            return self.rename_current(
                new_name, backup_existing_as=alternate_name or f"{new_name}_OLD"
            )
        raise ValueError(f"invalid rename conflict resolution option: {option!r}")

    # --- rotation (V key) --------------------------------------------------

    def rotate_current(self, *, direction: int = 1) -> list[SessionEvent]:
        """Rotates the current image 90° (`direction=1` clockwise, `-1`
        counter-clockwise), cycling through 0/90/180/270 (V key / Shift+V).
        """
        current = self.state.current_image
        if current is None or current.state != "IN_REVIEW":
            raise IllegalTransitionError(current.state if current else "NONE", "rotation")
        return self.set_rotation((current.rotation_deg + 90 * direction) % 360)

    def set_rotation(self, rotation_deg: int) -> list[SessionEvent]:
        """Sets the current image's rotation to an absolute value in one shot
        — journaling exactly once, regardless of how many V/Shift+V presses
        it took to get there (the GUI debounces `rotate_current` and calls
        this once the operator settles on a value, rather than once per
        intermediate press). Never triggers an export itself — the archival
        tiff/jpeg_master/jpeg_positive only need to reflect whatever
        rotation the operator has settled on when the image is actually
        left; `validate_current()` is the single point that queues them.
        """
        current = self.state.current_image
        if current is None or current.state != "IN_REVIEW":
            raise IllegalTransitionError(current.state if current else "NONE", "rotation")

        name = current.assigned_name
        before = current.rotation_deg
        after = rotation_deg % 360
        if after == before:
            return []
        current.rotation_deg = after
        # The content frame (if any) was computed against the previous
        # rotation and no longer lines up with the pixels it would be
        # cropping — cleared until the export queued at validate time
        # recomputes it.
        current.content_framing = None
        self.journal.log(
            "FRAMING",
            "rotation",
            image=name,
            details={"rotation_deg": {"before": before, "after": after}},
        )
        events: list[SessionEvent] = [RotationChanged(name=name, rotation_deg=after)]

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
        Never triggers an export itself: `validate_current()` is the single
        point that queues the archival tiff/jpeg_master/jpeg_positive, once,
        against whatever frame the operator has settled on by the time the
        image is actually left — not against every intermediate detection
        or edit along the way.
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
        # The content frame (if any) was computed against the previous
        # support frame and no longer lines up with the pixels it would be
        # cropping — cleared until the export queued at validate time
        # recomputes it.
        current.content_framing = None
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
        """Option 1: renames the incoming file.

        The conflicting row (`conflict.name`) stays `todo`, untouched, for a
        later capture — unless `target` happens to be a real pending row
        itself (e.g. the "use next free name" suggestion in the GUI), in
        which case it's consumed like a normal ingestion (row marked, cursor
        advanced past it) rather than left dangling as an off-list name.
        """
        target = new_name or f"{conflict.name}_BIS"
        validate_name(target, max_name_length=self._max_name_length)
        if find_conflicting_path(target, self.paths, self.fs) is not None:
            raise ValueError(f"Name already in use: {target!r}")

        self.journal.log(
            "NAMING",
            "conflict_resolved",
            details={"option": 1, "old": conflict.name, "new": target},
        )
        events: list[SessionEvent] = [NameConflictResolved(option=1, old=conflict.name, new=target)]
        target_row = self.inventory.row(target)
        if target_row is not None and target_row[STATUS_COLUMN] == "todo":
            self.inventory.go_to_name(target)
            events.extend(self._ingest_one(conflict.path, target, csv_row=target))
        else:
            events.extend(self._ingest_one(conflict.path, target, csv_row=None))
        return events

    def resolve_exhaustion(self, new_name: str) -> list[SessionEvent]:
        """Assigns an explicit name to the RAW file stuck waiting for
        ingestion because the inventory ran out of `todo` rows (E-12) —
        the capture screen opens its name-entry field automatically the
        moment that happens, the operator types a name on the spot instead
        of having to add a row to the CSV and wait for the next pump to
        pick it up. Ingested off-list (`csv_row=None`), same fallback
        `_resolve_conflict_rename_incoming` uses when the given name isn't
        itself a pending `todo` row: there's no CSV row here to consume in
        the first place, exhaustion means there wasn't one to begin with.

        Raises `ValueError` for an invalid or already-used name (same
        validation `resolve_conflict` applies) rather than silently
        ignoring it — the caller (the GUI's submit handler) is expected to
        catch it and let the operator correct the field, not lose the
        image still waiting in `_pending_ingest`.
        """
        if not self._pending_ingest:
            raise ValueError("no image waiting for a name")
        validate_name(new_name, max_name_length=self._max_name_length)
        if find_conflicting_path(new_name, self.paths, self.fs) is not None:
            raise ValueError(f"Name already in use: {new_name!r}")
        source_path = self._pending_ingest.popleft()
        self.journal.log(
            "NAMING", "exhaustion_resolved", image=new_name, details={"name": new_name}
        )
        return self._ingest_one(source_path, new_name, csv_row=None)

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
        validate_name(target, max_name_length=self._max_name_length)
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

    def _resume_if_correction_auto_paused(self) -> list[SessionEvent]:
        """Lifts a `reopen_for_correction`-induced pause once its slot closes."""
        if not self._correction_auto_paused:
            return []
        self._correction_auto_paused = False
        return self.resume()

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

    # --- session history (correction side panel) --------------------------

    def _record_session_history(self, current: CurrentImageState) -> None:
        """Snapshots a just-validated image, updating its earlier entry in
        place if there is one — a correction re-finalizing an image already
        in history must not bump it to the newest slot, or the panel's
        chronological order breaks the moment the operator fixes anything
        but the very last capture."""
        name = current.assigned_name
        entry = SessionHistoryEntry(
            name=name,
            source_file=current.source_file,
            extension=current.extension,
            rotation_deg=current.rotation_deg,
            framing=replace(current.framing),
        )
        for i, existing in enumerate(self._session_history):
            if existing.name == name:
                self._session_history[i] = entry
                return
        self._session_history.append(entry)

    def session_history(self) -> list[SessionHistoryEntry]:
        """Images finalized during this run, oldest first (history side panel)."""
        return list(self._session_history)

    def reopen_for_correction(self, name: str) -> list[SessionEvent]:
        """Reopens a finalized image from this session's history for correction.

        For catching a mistake (wrong rotation, bad crop) right after making
        it: doesn't change the CSV status (still `done`) or the image's
        position in the inventory — `validate_current()` re-finalizes it
        normally once the correction is confirmed. Pauses capture as a side
        effect so an incoming file can't collide with the reopened slot;
        `validate_current()`/`reject_current()` lift that pause again once
        the slot closes, provided the operator wasn't already paused before
        reopening (in which case it's their call to resume, not ours).
        """
        if self.state.current_image is not None:
            raise IllegalTransitionError(self.state.current_image.state, "reopen_for_correction")
        entry = next((e for e in self._session_history if e.name == name), None)
        if entry is None:
            raise ValueError(f"{name!r} is not in this session's history")
        raw_path = self.paths.raw_dir / f"{entry.name}{entry.extension}"
        if not self.fs.exists(raw_path):
            raise ValueError(f"RAW file for {name!r} is missing")

        if not self.paused:
            self.pause()
            self._correction_auto_paused = True

        self.state.current_image = CurrentImageState(
            assigned_name=entry.name,
            source_file=entry.source_file,
            extension=entry.extension,
            state="IN_REVIEW",
            rotation_deg=entry.rotation_deg,
            framing=replace(entry.framing),
            # `SessionHistoryEntry` never carries this — reconstructed from
            # the journal instead, so a reopened image shows the same crop
            # the operator last saw, instead of silently resetting to
            # "deferred" (unset).
            content_framing=reconstruct_content_framing_state(self.paths, self.fs, name),
        )
        self.journal.log("CAPTURE", "reopened_for_correction", image=name)
        self._persist_state()
        return [ImageStateChanged(name=name, previous="COMPLETED", new="IN_REVIEW")]

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
        extra_ignored_suffixes = tuple(self.campaign.capture.extra_ignored_suffixes)
        candidates = [
            p
            for p in self.fs.list_dir(self.monitor.folder)
            if is_candidate_file(p, extensions, extra_ignored_suffixes=extra_ignored_suffixes)
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

    def stop(self, *, wait_for_exports: bool = True) -> list[SessionEvent]:
        """Exits capture mode / clean shutdown: finalizes the current image.

        `wait_for_exports=True` (default) fully drains the export queue,
        blocking (`deadline=None`), before leaving capture mode: unlike the
        periodic bounded drain in `pump()`, shutdown must wait for the queue
        to finish rather than abandon in-flight tasks. `wait_for_exports=False`
        (GUI-only: `processing.drain_on_exit`) still submits the whole
        backlog but returns immediately — safe because whatever is still in
        flight stays recorded in `state.export_queue` either way (`ExportQueue`
        tracks in-flight tasks) and is simply re-run from the untouched RAW
        on the next launch.
        """
        events = self.validate_current()
        events.extend(self._drain_exports(wait=wait_for_exports))
        self.state.mode = "preparation"
        self._persist_state()
        return events

    def wait_for_pending_exports(self) -> list[SessionEvent]:
        """Blocks until every export currently queued or in flight (either
        executor) has actually finished and been journaled/logged.

        `regenerate_positive`/`apply_manual_print_overrides`/etc. all submit through
        `_drain_exports` with a short, bounded deadline — by design, meant
        for a caller with its own periodic pump (`CaptureScreen`'s pump
        timer) that will collect the result on a *later* call, not this
        one. A caller with no periodic pump of its own (the positive
        calibration screen) has no later call coming — without this, a
        journal/state read immediately after one of those methods returns
        can silently see stale data (the regenerate is still running on the
        executor's own worker thread)."""
        events = self._drain_exports(deadline=None)
        self._persist_state()
        return events

    def shutdown_executors(self) -> None:
        """Releases both background executors' worker thread(s), if any —
        `self.export_executor` and, when configured, the print_engine
        finalize pool. Call after `stop()`."""
        self.export_executor.shutdown()
        if self._positive_finalize_executor is not None:
            self._positive_finalize_executor.shutdown()

    def collect_export_progress(self) -> list[SessionEvent]:
        """Non-blocking check-in while waiting out a `stop(wait_for_exports=False)`.

        Collects whatever the background executor has finished since the
        last call (bookkeeping: journal, CSV status, `state.export_queue`)
        without submitting anything new or blocking — the backlog was
        already fully submitted by `stop()`. Persists the shrunk queue so a
        crash during the wait never re-does more than what's genuinely
        still in flight.
        """
        events = self._drain_exports(wait=False)
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
