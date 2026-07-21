"""Reset a campaign back to its just-created state (Project ▸ Reset campaign).

Never deletes a captured negative: `RAW/` and `REJECTED/` are archived into
a timestamped `BACKUP/<stamp>/` subfolder instead, same as any other
conflict-resolution move elsewhere in the project. `TIFF/`, `JPEG_MASTER/`,
`JPEG_POSITIVE/` are fully regenerable from the RAW + campaign settings, so
their contents are deleted outright. Campaign settings (`campaign.json`)
are never touched — only the inventory/state that tracks *progress*
through the campaign.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scanassistant.core.fs import FileSystem
from scanassistant.journal.journal import Journal
from scanassistant.project.inventory import SOURCE_FILE_COLUMN, STATUS_COLUMN, Inventory
from scanassistant.project.layout import CampaignPaths
from scanassistant.project.state import ProjectState, save_state

_ARCHIVED_DIRS = ("raw_dir", "rejected_dir")
_REGENERABLE_DIRS = ("tiff_dir", "jpeg_master_dir", "jpeg_positive_dir")


@dataclass(frozen=True)
class ResetResult:
    backup_dir: Path
    archived_count: int
    deleted_count: int


def reset_campaign(
    paths: CampaignPaths,
    inventory: Inventory,
    state: ProjectState,
    journal: Journal,
    fs: FileSystem,
    *,
    now: datetime | None = None,
) -> ResetResult:
    """Archives every captured negative, deletes regenerable exports, and
    resets the CSV/state to a fresh, never-started campaign.

    Meant to be called only from the Project screen (preparation mode) —
    there is no running `CaptureSession` to coordinate with here, so this
    must never run against a campaign currently in capture.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    backup_subdir = paths.backup_dir / stamp
    backup_subdir.mkdir(parents=True, exist_ok=True)

    archived_count = 0
    for attr in _ARCHIVED_DIRS:
        directory: Path = getattr(paths, attr)
        for entry in fs.list_dir(directory):
            fs.rename(entry, backup_subdir / entry.name)
            archived_count += 1

    deleted_count = 0
    for attr in _REGENERABLE_DIRS:
        directory: Path = getattr(paths, attr)
        for entry in fs.list_dir(directory):
            fs.remove(entry)
            deleted_count += 1

    for row in inventory.rows:
        row[STATUS_COLUMN] = "todo"
        row[SOURCE_FILE_COLUMN] = ""
    inventory.cursor = 0
    inventory.save(paths.inventory_csv)

    state.csv_cursor = 0
    state.current_image = None
    state.export_queue = []
    state.ignored_files = []
    state.pause_queue = []
    state.error_images = []
    save_state(state, paths.state_json)

    journal.log(
        "PROJECT",
        "reset",
        details={
            "backup_dir": str(backup_subdir),
            "archived": archived_count,
            "deleted": deleted_count,
        },
    )
    return ResetResult(
        backup_dir=backup_subdir, archived_count=archived_count, deleted_count=deleted_count
    )
