"""Stabilization detection for a file being copied.

Pure core, no thread or I/O: `StabilizationTracker.poll()` advances a
state machine from a (size, mtime) snapshot and a clock supplied by the
caller. `watcher/monitor.py` provides the real snapshots and call cadence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


@dataclass(frozen=True)
class FileSnapshot:
    size: int
    mtime: float


class PollResult(Enum):
    PENDING = auto()
    STABLE = auto()
    TIMED_OUT = auto()


@dataclass
class StabilizationTracker:
    """Stabilization state for a single file, advanced by `poll()`.

    - Zero size: never stable, the window resets.
    - Stable = (size, mtime) unchanged over a continuous window
      ≥ `stabilization_delay_s`.
    - Times out after `stabilization_timeout_s` from the first snapshot.
    """

    stabilization_delay_s: float
    stabilization_timeout_s: float
    started_at: float
    last_snapshot: FileSnapshot | None = None
    stable_since: float | None = None

    def poll(self, snapshot: FileSnapshot, now: float) -> PollResult:
        if now - self.started_at > self.stabilization_timeout_s:
            return PollResult.TIMED_OUT

        if snapshot.size == 0:
            self.last_snapshot = None
            self.stable_since = None
            return PollResult.PENDING

        if self.last_snapshot is not None and snapshot == self.last_snapshot:
            if self.stable_since is None:
                self.stable_since = now
            if now - self.stable_since >= self.stabilization_delay_s:
                return PollResult.STABLE
            return PollResult.PENDING

        self.last_snapshot = snapshot
        self.stable_since = now
        return PollResult.PENDING

    def duration_s(self, now: float) -> float:
        return now - self.started_at


def poll_interval_s(stabilization_delay_s: float) -> float:
    """Recheck cadence: `max(0.5s, delay/2)`."""
    return max(0.5, stabilization_delay_s / 2)
