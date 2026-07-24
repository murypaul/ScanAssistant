"""Atomic file writes.

Used for every state file that must survive an unclean shutdown:
``campaign.json``, ``state.json``, ``inventory.csv``, the global
configuration, and the journal.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Writes `data` to `path` atomically (tmp + fsync + replace).

    The temp file name is unique per call (PID + thread id), not a fixed
    `<name>.tmp` — `state.json` in particular can legitimately be written
    from more than one thread close together (a background export
    finishing its own `_persist_state()` at the same moment a screen's
    periodic poll calls `collect_export_progress()`, confirmed in real
    use): a fixed shared name meant the second writer's own `os.replace`
    could find nothing left to rename — the first writer had already
    moved it out from under it (`[Errno 2] ... '<path>.tmp' -> '<path>'`)
    — and, worse, both writers' `os.open(..., O_TRUNC)` could target the
    very same inode, corrupting either write's content. A unique name per
    call means concurrent writers each fully own their own temp file; the
    last `os.replace` to run simply wins, same as before.
    """
    path = Path(path)
    tmp_path = path.parent / f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    _fsync_dir(path.parent)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Text variant of `atomic_write_bytes`."""
    atomic_write_bytes(path, text.encode(encoding))


def _fsync_dir(dir_path: Path) -> None:
    """Fsyncs the parent directory (POSIX only, best-effort).

    Best-effort: some network mounts (SMB/NFS) refuse a directory fsync;
    the bulk of the durability guarantee already comes from fsyncing the
    temp file and from `os.replace`.
    """
    if os.name != "posix":
        return
    try:
        dir_fd = os.open(dir_path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)
