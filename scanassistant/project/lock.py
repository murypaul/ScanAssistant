"""Instance lock."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scanassistant.project.errors import ProjectAlreadyOpenError
from scanassistant.utils.atomic import atomic_write_text


@dataclass
class LockInfo:
    pid: int
    host: str
    ts: str


@dataclass
class ProjectLock:
    """Lock held on a campaign; release via `release()` (or `with`)."""

    path: Path
    info: LockInfo
    was_stale: bool = False
    """True if a lock left by a dead PID was recovered: triggers crash
    recovery (`core.crash_recovery`)."""
    _released: bool = False

    def release(self) -> None:
        """Releases the lock (clean shutdown)."""
        if self._released:
            return
        self.path.unlink(missing_ok=True)
        self._released = True

    def __enter__(self) -> ProjectLock:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def acquire_lock(path: Path) -> ProjectLock:
    """Acquires a campaign's `.lock` file.

    Raises `ProjectAlreadyOpenError` (E-14) if a live PID on the same host
    already holds it. A lock with a dead PID (or a different host — a
    network case that can't be arbitrated locally) is recovered silently,
    with `ProjectLock.was_stale = True`: this triggers full crash recovery
    (`core.crash_recovery`).
    """
    path = Path(path)
    hostname = socket.gethostname()
    was_stale = False

    if path.exists():
        existing = _read_lock(path)
        if existing.host == hostname and _is_pid_alive(existing.pid):
            raise ProjectAlreadyOpenError(existing.host, existing.pid)
        was_stale = True

    info = LockInfo(pid=os.getpid(), host=hostname, ts=datetime.now().isoformat(timespec="seconds"))
    atomic_write_text(path, json.dumps(info.__dict__, indent=2) + "\n")
    return ProjectLock(path=path, info=info, was_stale=was_stale)


def _read_lock(path: Path) -> LockInfo:
    data = json.loads(path.read_text(encoding="utf-8"))
    return LockInfo(pid=data["pid"], host=data["host"], ts=data["ts"])


def _is_pid_alive(pid: int) -> bool:
    """Tests whether `pid` is a live process, on both POSIX and Windows."""
    if os.name == "nt":
        return _is_pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by another user
    return True


def _is_pid_alive_windows(pid: int) -> bool:  # pragma: no cover — requires Windows
    import ctypes

    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
        process_query_limited_information, False, pid
    )
    if handle == 0:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    return True
