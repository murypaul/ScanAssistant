"""Verified ingestion: name assignment + move.

Single copy of the RAW: the file leaves the watched folder for `RAW/`
under its inventory name, either by atomic rename (same volume) or by
verified copy + swap (different volumes). The source is never removed
before verification succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scanassistant.core.errors import IntegrityCheckFailedError
from scanassistant.core.fs import FileSystem
from scanassistant.project.layout import CampaignPaths

INGEST_TEMP_PREFIX = ".ingest_"

_CONFLICT_DIRS = ("raw_dir", "tiff_dir", "jpeg_master_dir", "jpeg_positive_dir")


@dataclass(frozen=True)
class IngestResult:
    name: str
    extension: str
    via: str  # "rename" | "copy_verified"
    sha256: str | None = None
    source_removed: bool = True


def find_conflicting_path(name: str, paths: CampaignPaths, fs: FileSystem) -> Path | None:
    """Global uniqueness: `name` (any extension) across RAW/TIFF/JPEG_*/."""
    for attr in _CONFLICT_DIRS:
        directory = getattr(paths, attr)
        for entry in fs.list_dir(directory):
            if entry.stem == name:
                return entry
    return None


def ingest_file(
    source_path: Path,
    *,
    name: str,
    paths: CampaignPaths,
    fs: FileSystem,
    verify_checksum: bool,
    same_volume: bool | None = None,
) -> IngestResult:
    """Moves `source_path` to `RAW/<name><ext>` (single copy).

    Doesn't log anything: the caller (`core.session`) has the current
    image's context. Raises `IntegrityCheckFailedError` (E-04) if a
    mismatch persists after a single retry; in that case the source in
    the watched folder stays intact.
    """
    extension = source_path.suffix
    destination = paths.raw_dir / f"{name}{extension}"

    if same_volume is None:
        same_volume = _same_filesystem(source_path.parent, paths.raw_dir)

    if same_volume:
        fs.rename(source_path, destination)
        return IngestResult(name=name, extension=extension, via="rename")

    return _ingest_cross_volume(
        source_path,
        destination,
        paths=paths,
        fs=fs,
        verify_checksum=verify_checksum,
        name=name,
        extension=extension,
    )


def _ingest_cross_volume(
    source_path: Path,
    destination: Path,
    *,
    paths: CampaignPaths,
    fs: FileSystem,
    verify_checksum: bool,
    name: str,
    extension: str,
    _retry: bool = False,
) -> IngestResult:
    temp_path = paths.raw_dir / f"{INGEST_TEMP_PREFIX}{name}{extension}"
    fs.copy_verified(source_path, temp_path)

    source_stat = fs.stat(source_path)
    temp_stat = fs.stat(temp_path)
    ok = source_stat.size == temp_stat.size

    sha256: str | None = None
    if ok and verify_checksum:
        sha256 = fs.sha256(source_path)
        ok = sha256 == fs.sha256(temp_path)

    if not ok:
        fs.remove(temp_path)
        if _retry:
            raise IntegrityCheckFailedError(name)
        return _ingest_cross_volume(
            source_path,
            destination,
            paths=paths,
            fs=fs,
            verify_checksum=verify_checksum,
            name=name,
            extension=extension,
            _retry=True,
        )

    fs.replace(temp_path, destination)

    source_removed = True
    try:
        fs.remove_verified_source(source_path)
    except OSError:
        source_removed = False

    return IngestResult(
        name=name,
        extension=extension,
        via="copy_verified",
        sha256=sha256,
        source_removed=source_removed,
    )


def _same_filesystem(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False
