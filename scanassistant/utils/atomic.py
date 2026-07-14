"""Atomic file writes.

Used for every state file that must survive an unclean shutdown:
``campaign.json``, ``state.json``, ``inventory.csv``, the global
configuration, and the journal.
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Writes `data` to `path` atomically (tmp + fsync + replace)."""
    path = Path(path)
    tmp_path = path.parent / f"{path.name}.tmp"
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
