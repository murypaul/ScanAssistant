"""Watched-folder monitoring.

`FolderMonitor.tick()` is the pure, testable function: candidate
detection (directory listing or already-accumulated watchdog events) +
advancing stabilization (`watcher.stability`) for a given `now`, no
thread or `sleep` involved. `start()`/`stop()` wrap `tick()` in a real
thread for production use.

`core.session` only ever sees the events emitted here (`Detected`,
`Stabilized`, `StabilizationTimedOut`); it never imports watchdog.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scanassistant.watcher.stability import (
    FileSnapshot,
    PollResult,
    StabilizationTracker,
    poll_interval_s,
)

IGNORED_NAME_PREFIXES = (".", "~")
IGNORED_NAME_SUFFIXES = (".tmp", ".part", ".crdownload")


def is_candidate_file(path: Path, extensions: set[str]) -> bool:
    """Detection filter: accepted extension, not hidden/temporary."""
    name = path.name
    if name.startswith(IGNORED_NAME_PREFIXES):
        return False
    if name.lower().endswith(IGNORED_NAME_SUFFIXES):
        return False
    return path.suffix.lower() in extensions


@dataclass(frozen=True)
class Detected:
    path: Path
    size: int


@dataclass(frozen=True)
class Stabilized:
    path: Path
    duration_s: float


@dataclass(frozen=True)
class StabilizationTimedOut:
    path: Path


MonitorEvent = Detected | Stabilized | StabilizationTimedOut


def _default_stat(path: Path) -> FileSnapshot:
    st = path.stat()
    return FileSnapshot(size=st.st_size, mtime=st.st_mtime)


class FolderMonitor:
    """Detects candidate files and tracks their stabilization.

    `watch_mode` ∈ {auto, native, polling}: in `auto` mode, falls back to
    polling if the path is a network mount (best-effort heuristic) or if
    the native observer fails to start.
    """

    def __init__(
        self,
        folder: Path,
        extensions: list[str],
        *,
        watch_mode: str = "auto",
        stabilization_delay_s: float = 2.0,
        stabilization_timeout_s: float = 120.0,
        stat_fn: Callable[[Path], FileSnapshot] = _default_stat,
        on_mode_resolved: Callable[[str], None] | None = None,
    ) -> None:
        self.folder = Path(folder)
        self._extensions = {e.lower() for e in extensions}
        self._stabilization_delay_s = stabilization_delay_s
        self._stabilization_timeout_s = stabilization_timeout_s
        self._stat_fn = stat_fn
        self._on_mode_resolved = on_mode_resolved

        self._known: set[Path] = set()
        self._known_lock = threading.Lock()
        self._handled: set[Path] = set()
        self._trackers: dict[Path, StabilizationTracker] = {}
        self._events: queue.Queue[MonitorEvent] = queue.Queue()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer: Any = None

        self.effective_mode = self._resolve_mode(watch_mode)
        if self._on_mode_resolved:
            self._on_mode_resolved(self.effective_mode)

    # --- mode resolution ---------------------------------------------------

    def _resolve_mode(self, watch_mode: str) -> str:
        if watch_mode == "polling":
            return "polling"
        if watch_mode == "native":
            return "native"
        if _is_network_path(self.folder):
            return "polling"
        return "native"

    # --- pure core, testable without a thread -----------------------------

    def seed(self, paths: list[Path]) -> None:
        """Adds known paths without waiting for a native event/scan."""
        with self._known_lock:
            self._known.update(paths)

    def tick(self, now: float) -> list[MonitorEvent]:
        """Advances detection and stabilization by one tick; returns the events.

        Pure with respect to time (`now` supplied by the caller); only the
        stat I/O (`stat_fn`) and, in polling mode, the directory listing,
        actually touch disk.
        """
        emitted: list[MonitorEvent] = []

        if self.effective_mode != "native":
            self._scan_directory()

        with self._known_lock:
            self._handled = {p for p in self._handled if self._still_present(p)}
            candidates = {
                p
                for p in self._known
                if p not in self._handled and is_candidate_file(p, self._extensions)
            }

        for path in candidates:
            if path not in self._trackers:
                try:
                    snapshot = self._stat_fn(path)
                except OSError:
                    continue
                self._trackers[path] = StabilizationTracker(
                    stabilization_delay_s=self._stabilization_delay_s,
                    stabilization_timeout_s=self._stabilization_timeout_s,
                    started_at=now,
                )
                emitted.append(Detected(path=path, size=snapshot.size))

        for path in list(self._trackers):
            tracker = self._trackers[path]
            try:
                snapshot = self._stat_fn(path)
            except OSError:
                del self._trackers[path]
                continue

            result = tracker.poll(snapshot, now)
            if result is PollResult.STABLE:
                emitted.append(Stabilized(path=path, duration_s=tracker.duration_s(now)))
                del self._trackers[path]
                with self._known_lock:
                    self._handled.add(path)
            elif result is PollResult.TIMED_OUT:
                emitted.append(StabilizationTimedOut(path=path))
                del self._trackers[path]
                with self._known_lock:
                    self._known.discard(path)

        for event in emitted:
            self._events.put(event)
        return emitted

    def _still_present(self, path: Path) -> bool:
        try:
            self._stat_fn(path)
            return True
        except OSError:
            return False

    def _scan_directory(self) -> None:
        try:
            entries = [p for p in self.folder.iterdir() if p.is_file()]
        except OSError:
            return
        with self._known_lock:
            self._known.update(entries)

    def _on_native_event(self, path: Path) -> None:
        with self._known_lock:
            self._known.add(path)

    # --- background execution (production) --------------------------------

    def start(self) -> None:
        if self.effective_mode == "native":
            try:
                self._start_native()
                self._start_ticker()
                return
            except Exception:
                self.effective_mode = "polling"
                if self._on_mode_resolved:
                    self._on_mode_resolved(self.effective_mode)
        self._start_ticker()

    def _start_native(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        monitor = self

        class Handler(FileSystemEventHandler):
            def on_created(self, event: Any) -> None:
                if not event.is_directory:
                    monitor._on_native_event(Path(event.src_path))

            def on_moved(self, event: Any) -> None:
                if not event.is_directory:
                    monitor._on_native_event(Path(event.dest_path))

            def on_modified(self, event: Any) -> None:
                if not event.is_directory:
                    monitor._on_native_event(Path(event.src_path))

        self._observer = Observer()
        self._observer.schedule(Handler(), str(self.folder), recursive=False)
        self._observer.start()

    def _start_ticker(self) -> None:
        interval = poll_interval_s(self._stabilization_delay_s)

        def loop() -> None:
            while not self._stop_event.is_set():
                self.tick(time.monotonic())
                self._stop_event.wait(interval)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def get_event(self, timeout: float = 0.0) -> MonitorEvent | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None


def _is_network_path(path: Path) -> bool:
    """Best-effort heuristic: UNC path or POSIX network mount."""
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    return _is_posix_network_mount(path)


def _is_posix_network_mount(path: Path) -> bool:
    network_filesystems = {"nfs", "nfs4", "cifs", "smbfs", "smb2", "9p"}
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return False

    resolved = str(path.resolve()) if path.exists() else str(path)
    best_match_len = -1
    best_fs_type = ""
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point, fs_type = parts[1], parts[2]
        if resolved.startswith(mount_point) and len(mount_point) > best_match_len:
            best_match_len = len(mount_point)
            best_fs_type = fs_type
    return best_fs_type in network_filesystems
