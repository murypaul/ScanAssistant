"""Background export queue.

`ExportRunner` is the pluggable interface: `FakeExportRunner` produces no
pixels and is used by tests; the production implementation
(`core.export_runner.MasterExportRunner`) assembles `imaging.master`,
`imaging.positive`, and `metadata.writer` without this module depending
on any of them.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

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
    orientation: str  # horizontal | vertical
    x: int
    y: int
    width: int
    height: int
    angle_deg: float


@dataclass(frozen=True)
class ExportResult:
    """Effective details of a successful export, for the `EXPORT` journal entry."""

    scale_factor: float = 1.0
    bounds_adjusted: bool = False


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


@dataclass
class ExportQueue:
    """FIFO queue, persisted by the caller in `state.json:export_queue`."""

    _tasks: deque[ExportTask] = field(default_factory=deque)

    def enqueue(self, name: str, kinds: list[str], context: ExportContext | None = None) -> None:
        for kind in kinds:
            self._tasks.append(ExportTask(name=name, kind=kind, context=context))

    def cancel(self, name: str) -> list[ExportTask]:
        """Removes pending tasks for `name` (used on rejection)."""
        kept: deque[ExportTask] = deque()
        cancelled: list[ExportTask] = []
        for task in self._tasks:
            (cancelled if task.name == name else kept).append(task)
        self._tasks = kept
        return cancelled

    def __len__(self) -> int:
        return len(self._tasks)

    def pending_tasks(self) -> list[ExportTask]:
        """Read-only snapshot of pending tasks (for the "Export queue" panel)."""
        return list(self._tasks)

    def drain(
        self, runner: ExportRunner, *, deadline: float | None = None
    ) -> list[tuple[ExportTask, ExportResult | ExportFailure | None]]:
        """Runs pending tasks, in FIFO order, up to `deadline`.

        `deadline` (real clock `time.monotonic()`, distinct from the
        injectable `now` of `session.pump()`): `None` triggers a full,
        blocking drain, reserved for campaign shutdown. Otherwise the drain
        stops as soon as the deadline is exceeded *between two tasks* — at
        least one task always runs if the queue is non-empty, to guarantee
        progress — and the remaining tasks wait for the next call. This
        bounds how long a single call can block the calling thread,
        regardless of how many tasks are queued.
        """
        completed: list[tuple[ExportTask, ExportResult | ExportFailure | None]] = []
        while self._tasks:
            if deadline is not None and completed and time.monotonic() >= deadline:
                break
            task = self._tasks.popleft()
            result = runner.run(task)
            completed.append((task, result))
        return completed

    def to_state_entries(self) -> list[ExportQueueEntry]:
        """Groups pending tasks by name, in arrival order."""
        grouped: dict[str, list[str]] = {}
        for task in self._tasks:
            grouped.setdefault(task.name, []).append(task.kind)
        return [ExportQueueEntry(name=name, tasks=kinds) for name, kinds in grouped.items()]

    @classmethod
    def from_state_entries(cls, entries: list[ExportQueueEntry]) -> ExportQueue:
        queue = cls()
        for entry in entries:
            queue.enqueue(entry.name, entry.tasks)
        return queue
