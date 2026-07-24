"""Background export queue.

`ExportRunner` is the pluggable interface: `FakeExportRunner` produces no
pixels and is used by tests; the production implementation
(`core.export_runner.MasterExportRunner`) assembles `imaging.master`,
`imaging.print_engine`, and `metadata.writer` without this module depending
on any of them.

`ExportExecutor` decides *where* `ExportRunner.run()` actually executes.
`InlineExportExecutor` runs it synchronously, on the calling thread —
used by the CLI and by every test unless a real background executor is
wired in explicitly (only `gui.main_window` does). `ThreadedExportExecutor`
(stdlib `threading`, never PySide6) runs it on a single dedicated
background thread, so the caller (the Qt thread, in the GUI) is never
blocked by a slow export. A single worker, not a pool: (1)
`MasterExportRunner` caches the developed RAW master on `self` across the
three tasks of one image — safe only if a single thread ever calls
`run()`; (2) one worker is enough to fix the UI freeze this was built for
(DECISIONS.md I-92/I-98) without the added risk of a real worker pool.
"""

from __future__ import annotations

import contextlib
import queue as queue_module
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from scanassistant.journal.techlog import get_logger
from scanassistant.project.state import ExportQueueEntry

EXPORT_TASK_KINDS = ("tiff", "jpeg_master", "jpeg_positive")


@dataclass(frozen=True)
class ExportContext:
    """Snapshot of the parameters needed for export, frozen at queue time.

    Needed because a task can run after the current image has changed
    (`state.current_image` no longer describes it) — the context must
    therefore travel with the task rather than being re-read from current
    state at drain time.
    """

    raw_path: Path
    extension: str
    source_file: str
    rotation_deg: int  # 0 | 90 | 180 | 270, clockwise
    x: int
    y: int
    width: int
    height: int
    angle_deg: float
    # `jpeg_positive` only, both `None` by default (automatic): an operator's
    # manual choice from the "Recadrage des positifs" screen, applied for
    # this one regeneration — `core.recovery.rebuild_export_context` also
    # replays it from `project.positive_overrides` on any later, unrelated
    # regeneration, so an explicit value passed here (this one call) still
    # wins if it ever diverges from what's persisted. Mirrors
    # `imaging.print_engine.ManualPrintOverrides`, redefined in primitives
    # here rather than imported: same principle as `ContentFrameOutcome`
    # below, which must not import the `imaging` type it mirrors either.
    # Each `None` means that group stays automatic.
    manual_print_dmin: tuple[float, float, float] | None = None
    manual_print_exposure_shift: float | None = None
    manual_print_contrast: float | None = None
    manual_print_paper_black: float | None = None
    manual_print_paper_soft_clip: float | None = None
    manual_print_content_frame: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class ContentFrameOutcome:
    """`jpeg_positive`-only: the content-frame crop actually applied to the
    rendered positive (`imaging.content_framing`) — never the TIFF/JPEG
    master. Primitive fields only, not the `imaging` result type itself:
    this module must not import anything from `imaging` (see module
    docstring)."""

    x: int
    y: int
    width: int
    height: int
    fill: float
    area_ratio: float
    source: str = "auto"  # auto | manual — mirrors `project.state.FramingState.source`
    # Same crop as x/y/width/height, as fractions of `master.pixels`' own
    # width/height — lets a reviewer re-open this exact crop later without
    # knowing what resolution `master.pixels` was at this particular export
    # (see `ExportContext.manual_print_content_frame` above for why fractions).
    fraction: tuple[float, float, float, float] | None = None
    # `imaging.print_engine.PrintResult.flagged`: the density/tonal
    # estimate itself was unconfident (contrast capped, or exposure shift
    # beyond tolerance) — independent of crop confidence, an image can have
    # a perfectly good crop and still need a tonal review. Drives
    # `CaptureSession._log_positive_framing`'s `deferred` classification
    # alongside crop confidence, not just it.
    tonal_flagged: bool = False


@dataclass(frozen=True)
class ExportResult:
    """Effective details of a successful export, for the `EXPORT` journal entry."""

    scale_factor: float = 1.0
    bounds_adjusted: bool = False
    # `jpeg_positive` only; `None` means no confident crop was applied
    # (deferred) — logged either way (`POSITIVE_FRAMING`, `core.session`).
    content_frame: ContentFrameOutcome | None = None


@dataclass(frozen=True)
class ExportFailure:
    """Failure of an export task: the image is flagged ERROR."""

    code: str  # E-05 | E-06
    message: str


@dataclass(frozen=True)
class ExportTask:
    """`context` is `None` only for a task rebuilt cold from `state.json`
    (`ExportQueue.from_state_entries`): a real `ExportRunner` must treat
    this case as non-regenerable and log a warning rather than crash.
    """

    name: str
    kind: str  # tiff | jpeg_master | jpeg_positive
    context: ExportContext | None = None


class ExportRunner(Protocol):
    """Runs a single export task."""

    def run(self, task: ExportTask) -> ExportResult | ExportFailure | None: ...


class FakeExportRunner:
    """Test double: marks the task as processed without producing any pixels."""

    def __init__(self) -> None:
        self.completed: list[ExportTask] = []

    def run(self, task: ExportTask) -> ExportResult | ExportFailure | None:
        self.completed.append(task)
        return None


class ExportExecutor(Protocol):
    """Where `ExportRunner.run()` actually executes — inline or on a thread."""

    def submit(self, task: ExportTask, runner: ExportRunner) -> None:
        """Hands a task off for execution. Never blocks on the work itself."""
        ...

    def collect_completed(
        self,
    ) -> list[tuple[ExportTask, ExportResult | ExportFailure | None]]:
        """Returns whatever has finished since the last call. Never blocks."""
        ...

    def wait_idle(self) -> None:
        """Blocks until every submitted task has finished (clean shutdown)."""
        ...

    def shutdown(self) -> None:
        """Releases any background resources (thread, ...). Waits for idle first."""
        ...


class InlineExportExecutor:
    """Runs each task synchronously, as soon as it is submitted.

    Deterministic and thread-free: the default for `CaptureSession`, used
    by the CLI and by every test unless a real background executor is
    explicitly wired in (only the live GUI does, via `ThreadedExportExecutor`).
    """

    def __init__(self) -> None:
        self._completed: list[tuple[ExportTask, ExportResult | ExportFailure | None]] = []

    def submit(self, task: ExportTask, runner: ExportRunner) -> None:
        self._completed.append((task, runner.run(task)))

    def collect_completed(
        self,
    ) -> list[tuple[ExportTask, ExportResult | ExportFailure | None]]:
        completed, self._completed = self._completed, []
        return completed

    def wait_idle(self) -> None:
        return  # `submit()` already ran synchronously: nothing is ever in flight.

    def shutdown(self) -> None:
        return


class ThreadedExportExecutor:
    """Runs tasks on a single dedicated background thread.

    Keeps the calling thread (Qt) free while a slow export (RAW decode +
    TIFF/JPEG write + `exiftool`) runs — see the module docstring for why
    this is exactly one worker, not a pool.
    """

    def __init__(self) -> None:
        self._inbox: queue_module.Queue[tuple[ExportTask, ExportRunner] | None] = (
            queue_module.Queue()
        )
        self._outbox: queue_module.Queue[tuple[ExportTask, ExportResult | ExportFailure | None]] = (
            queue_module.Queue()
        )
        self._idle = threading.Condition()
        self._pending = 0
        self._thread = threading.Thread(
            target=self._worker_loop, name="scanassistant-export", daemon=True
        )
        self._thread.start()

    def submit(self, task: ExportTask, runner: ExportRunner) -> None:
        with self._idle:
            self._pending += 1
        self._inbox.put((task, runner))

    def _worker_loop(self) -> None:
        while True:
            item = self._inbox.get()
            if item is None:
                return
            task, runner = item
            try:
                result = runner.run(task)
            except Exception as exc:  # defensive: a real `ExportRunner` catches
                # its own failures (E-05/E-06) and never raises — an unexpected
                # exception here must not silently kill the worker thread.
                get_logger().exception(
                    "export task %s/%s crashed unexpectedly", task.name, task.kind
                )
                result = ExportFailure(code="E-06", message=str(exc))
            self._outbox.put((task, result))
            with self._idle:
                self._pending -= 1
                if self._pending == 0:
                    self._idle.notify_all()

    def collect_completed(
        self,
    ) -> list[tuple[ExportTask, ExportResult | ExportFailure | None]]:
        completed: list[tuple[ExportTask, ExportResult | ExportFailure | None]] = []
        while True:
            try:
                completed.append(self._outbox.get_nowait())
            except queue_module.Empty:
                break
        return completed

    def wait_idle(self) -> None:
        with self._idle:
            while self._pending > 0:
                self._idle.wait()

    def shutdown(self) -> None:
        self.wait_idle()
        self._inbox.put(None)
        self._thread.join()


class PooledExportExecutor:
    """Runs tasks on `worker_count` background threads sharing one inbox —
    unlike `ThreadedExportExecutor`, which is deliberately capped at one
    (see its docstring: `MasterExportRunner` caches state on `self` across
    a single image's tasks, safe only under a single caller thread).

    The `ExportRunner` given to `submit()` must therefore be safe to call
    concurrently from multiple threads — `core.positive_finalize_runner
    .PositiveFinalizeRunner` is (holds no cross-call state on `self`);
    `MasterExportRunner` is not, and must never be submitted here.

    Built for the positive-finalize pass: a separate pool from the
    single-worker master/quick-positive export path, so a
    deliberately more expensive finalize pass can never slow down the
    TIFF/JPEG master export, which must never fall behind capture.
    """

    def __init__(self, worker_count: int = 3) -> None:
        if worker_count < 1:
            raise ValueError("worker_count must be at least 1")
        self._inbox: queue_module.Queue[tuple[ExportTask, ExportRunner] | None] = (
            queue_module.Queue()
        )
        self._outbox: queue_module.Queue[tuple[ExportTask, ExportResult | ExportFailure | None]] = (
            queue_module.Queue()
        )
        self._idle = threading.Condition()
        self._pending = 0
        self._threads = [
            threading.Thread(
                target=self._worker_loop, name=f"scanassistant-positive-finalize-{i}", daemon=True
            )
            for i in range(worker_count)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, task: ExportTask, runner: ExportRunner) -> None:
        with self._idle:
            self._pending += 1
        self._inbox.put((task, runner))

    def _worker_loop(self) -> None:
        while True:
            item = self._inbox.get()
            if item is None:
                return
            task, runner = item
            try:
                result = runner.run(task)
            except Exception as exc:  # defensive, same contract as `ThreadedExportExecutor`
                get_logger().exception(
                    "positive-finalize task %s/%s crashed unexpectedly", task.name, task.kind
                )
                result = ExportFailure(code="E-06", message=str(exc))
            self._outbox.put((task, result))
            with self._idle:
                self._pending -= 1
                if self._pending == 0:
                    self._idle.notify_all()

    def collect_completed(
        self,
    ) -> list[tuple[ExportTask, ExportResult | ExportFailure | None]]:
        completed: list[tuple[ExportTask, ExportResult | ExportFailure | None]] = []
        while True:
            try:
                completed.append(self._outbox.get_nowait())
            except queue_module.Empty:
                break
        return completed

    def wait_idle(self) -> None:
        with self._idle:
            while self._pending > 0:
                self._idle.wait()

    def shutdown(self) -> None:
        self.wait_idle()
        for _ in self._threads:
            self._inbox.put(None)
        for thread in self._threads:
            thread.join()


@dataclass
class ExportQueue:
    """FIFO queue, persisted by the caller in `state.json:export_queue`.

    `_in_flight` holds tasks handed off to an `ExportExecutor` but not yet
    completed: they still count towards `len()`/`to_state_entries()` so a
    crash never drops a task that is merely still running (it becomes a
    regular backlog task again on the next `from_state_entries()`).
    """

    _tasks: deque[ExportTask] = field(default_factory=deque)
    _in_flight: deque[ExportTask] = field(default_factory=deque)

    def enqueue(self, name: str, kinds: list[str], context: ExportContext | None = None) -> None:
        """Coalesces a repeat request for the same (name, kind) that hasn't
        been checked out yet into a single task carrying the *latest*
        context, instead of appending a second one behind it — the last
        confirmation always wins. A task already checked out (running on a
        worker, possibly already writing a file) can't be touched from
        here, but since this still appends fresh work for that (name,
        kind), it's guaranteed to run again afterwards with this latest
        context: whichever in-flight write happens first ends up
        superseded by the one that runs last, never the other way round.
        """
        for kind in kinds:
            self._tasks = deque(
                task for task in self._tasks if not (task.name == name and task.kind == kind)
            )
            self._tasks.append(ExportTask(name=name, kind=kind, context=context))

    def cancel(self, name: str) -> list[ExportTask]:
        """Removes pending (not yet checked out) tasks for `name` (used on rejection).

        A task already checked out (`checkout_next()`) may already be
        running on an executor's worker thread — possibly mid-write — and
        cannot be safely interrupted from here, same limit as before this
        queue supported background execution at all.
        """
        kept: deque[ExportTask] = deque()
        cancelled: list[ExportTask] = []
        for task in self._tasks:
            (cancelled if task.name == name else kept).append(task)
        self._tasks = kept
        return cancelled

    def __len__(self) -> int:
        return len(self._tasks) + len(self._in_flight)

    def has_backlog(self) -> bool:
        """True if a not-yet-checked-out task remains."""
        return bool(self._tasks)

    def in_flight_count(self, name: str) -> int:
        return sum(1 for task in self._in_flight if task.name == name)

    def pending_tasks(self) -> list[ExportTask]:
        """Read-only snapshot of pending + in-flight tasks (for the "Export queue" panel)."""
        return [*self._in_flight, *self._tasks]

    def checkout_next(self) -> ExportTask | None:
        """Pops the next backlog task and marks it in-flight until `complete()`."""
        if not self._tasks:
            return None
        task = self._tasks.popleft()
        self._in_flight.append(task)
        return task

    def complete(self, task: ExportTask) -> None:
        """Un-marks a task as in-flight once its result has been collected."""
        with contextlib.suppress(ValueError):  # already completed/removed — tolerate defensively
            self._in_flight.remove(task)

    def drain(
        self, runner: ExportRunner, *, deadline: float | None = None
    ) -> list[tuple[ExportTask, ExportResult | ExportFailure | None]]:
        """Runs pending tasks synchronously, in FIFO order, up to `deadline`.

        `deadline` (real clock `time.monotonic()`, distinct from the
        injectable `now` of `session.pump()`): `None` triggers a full,
        blocking drain, reserved for campaign shutdown. Otherwise the drain
        stops as soon as the deadline is exceeded *between two tasks* — at
        least one task always runs if the queue is non-empty, to guarantee
        progress — and the remaining tasks wait for the next call. This
        bounds how long a single call can block the calling thread,
        regardless of how many tasks are queued.

        Convenience for direct/synchronous use (e.g. tests): `CaptureSession`
        itself now goes through an `ExportExecutor` instead (`checkout_next`/
        `complete`), so a real background executor can process tasks off
        the calling thread.
        """
        completed: list[tuple[ExportTask, ExportResult | ExportFailure | None]] = []
        while self.has_backlog():
            if deadline is not None and completed and time.monotonic() >= deadline:
                break
            task = self.checkout_next()
            assert task is not None
            result = runner.run(task)
            self.complete(task)
            completed.append((task, result))
        return completed

    def to_state_entries(self) -> list[ExportQueueEntry]:
        """Groups pending + in-flight tasks by name, in arrival order."""
        grouped: dict[str, list[str]] = {}
        for task in (*self._in_flight, *self._tasks):
            grouped.setdefault(task.name, []).append(task.kind)
        return [ExportQueueEntry(name=name, tasks=kinds) for name, kinds in grouped.items()]

    @classmethod
    def from_state_entries(cls, entries: list[ExportQueueEntry]) -> ExportQueue:
        queue = cls()
        for entry in entries:
            queue.enqueue(entry.name, entry.tasks)
        return queue
