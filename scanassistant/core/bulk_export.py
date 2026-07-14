"""Bulk copy of already-produced derivatives to an external destination.

Read-only on the campaign: copies out of `TIFF/`, `JPEG_MASTER/`,
`JPEG_POSITIVE/` — never touches `RAW/` or the watched folder (absolute
rule 1/2). No dependency on PySide6; the project screen wires this to a
"Export copies" panel (Folders tab).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from scanassistant.project.layout import CampaignPaths

LAYOUT_FLAT = "flat"  # every file directly under `destination`
LAYOUT_BY_TYPE = "by_type"  # one subfolder per kind, mirroring the campaign layout

_KIND_DIRS = (
    ("tiff_dir", "TIFF"),
    ("jpeg_master_dir", "JPEG_MASTER"),
    ("jpeg_positive_dir", "JPEG_POSITIVE"),
)


@dataclass(frozen=True)
class BulkExportResult:
    copied: int
    skipped_existing: int


def export_derivatives(paths: CampaignPaths, destination: Path, *, layout: str) -> BulkExportResult:
    """Copies every produced TIFF/JPEG master/JPEG positive file to `destination`.

    `layout=flat`: all files land directly under `destination` (JPEG master
    and JPEG positive never collide there since the positive always carries
    its configured suffix, unless an operator deliberately empties it).
    `layout=by_type`: replicates one subfolder per kind (`TIFF/`,
    `JPEG_MASTER/`, `JPEG_POSITIVE/`), same names as the campaign layout.
    Never overwrites an existing file at the destination — counted as
    skipped instead, so re-running an export after adding new images is
    safe and cheap.
    """
    destination = Path(destination)
    copied = 0
    skipped = 0
    for attr, subdir_name in _KIND_DIRS:
        source_dir: Path = getattr(paths, attr)
        if not source_dir.is_dir():
            continue
        target_dir = destination / subdir_name if layout == LAYOUT_BY_TYPE else destination
        entries = sorted(p for p in source_dir.iterdir() if p.is_file())
        if not entries:
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            target = target_dir / entry.name
            if target.exists():
                skipped += 1
                continue
            shutil.copy2(entry, target)
            copied += 1
    return BulkExportResult(copied=copied, skipped_existing=skipped)
