"""Filesystem abstraction used by the core.

All I/O in `core`/`metadata` goes through this interface, injected rather
than called directly: production uses `RealFileSystem`, tests can wrap it
with a guard that immediately fails any operation violating the
anti-data-loss invariants — that guard lives in `tests/`, not here.

`remove_verified_source` is deliberately distinct from `remove`: it's the
only legitimate way to remove a source file from the watched folder after
verified ingestion; a test guard can therefore allow one and forbid the
other without having to guess the caller's intent.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from scanassistant.utils.atomic import atomic_write_text

_HASH_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class FileStat:
    size: int
    mtime: float


class FileSystem(Protocol):
    """File operations needed for ingestion, rejection, and conflicts."""

    def exists(self, path: Path) -> bool: ...
    def stat(self, path: Path) -> FileStat: ...
    def rename(self, src: Path, dst: Path) -> None: ...
    def copy_verified(self, src: Path, dst: Path) -> None: ...
    def replace(self, src: Path, dst: Path) -> None: ...
    def remove(self, path: Path) -> None: ...
    def remove_verified_source(self, path: Path) -> None: ...
    def touch_and_remove(self, path: Path) -> None: ...
    def sha256(self, path: Path) -> str: ...
    def write_text(self, path: Path, text: str) -> None: ...
    def read_text(self, path: Path) -> str: ...
    def list_dir(self, path: Path) -> list[Path]: ...
    def free_space_gb(self, path: Path) -> float: ...


class RealFileSystem:
    """Production implementation: real disk I/O."""

    def exists(self, path: Path) -> bool:
        return path.exists()

    def stat(self, path: Path) -> FileStat:
        st = path.stat()
        return FileStat(size=st.st_size, mtime=st.st_mtime)

    def rename(self, src: Path, dst: Path) -> None:
        """Atomic `os.rename`; refuses to overwrite an existing target."""
        if dst.exists():
            raise FileExistsError(f"Destination already exists: {dst}")
        os.rename(src, dst)

    def copy_verified(self, src: Path, dst: Path) -> None:
        """Copies `src` to `dst` (new file) with `fsync`."""
        if dst.exists():
            raise FileExistsError(f"Destination already exists: {dst}")
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst)
            fdst.flush()
            os.fsync(fdst.fileno())

    def replace(self, src: Path, dst: Path) -> None:
        os.replace(src, dst)

    def remove(self, path: Path) -> None:
        path.unlink()

    def remove_verified_source(self, path: Path) -> None:
        path.unlink()

    def touch_and_remove(self, path: Path) -> None:
        """Access probe file (`.scanassistant_probe`).

        Clears a leftover probe from a previous run first: this only checks
        that the folder is currently writable, not that no earlier crash
        left the probe behind between its own touch and unlink.
        """
        path.unlink(missing_ok=True)
        path.touch()
        path.unlink()

    def sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(_HASH_CHUNK_SIZE):
                digest.update(chunk)
        return digest.hexdigest()

    def write_text(self, path: Path, text: str) -> None:
        atomic_write_text(path, text)

    def read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def list_dir(self, path: Path) -> list[Path]:
        if not path.exists():
            return []
        return list(path.iterdir())

    def free_space_gb(self, path: Path) -> float:
        """Free space on the volume holding `path`, in decimal GB."""
        return shutil.disk_usage(path).free / 1_000_000_000
