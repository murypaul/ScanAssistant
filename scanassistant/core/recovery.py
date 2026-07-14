"""Rebuilding export context from the journal.

For an image that's no longer the current one (`state.current_image`
doesn't describe it anymore), the JSONL journal is the only durable
source of the applied frame/orientation: this module replays the events
for an image to rebuild a complete `ExportContext`, used to regenerate a
failed export (`CaptureSession.retry_error_image`) or recover after an
unclean shutdown (`core.crash_recovery`).
"""

from __future__ import annotations

import json

from scanassistant.core.fs import FileSystem
from scanassistant.core.queue import ExportContext
from scanassistant.project.layout import CampaignPaths

# A FRAMING or EXPORT journal entry carries all these fields in `details`
# when it describes an effective frame — present together only on these
# two event types.
_FRAME_DETAIL_KEYS = ("x", "y", "width", "height", "angle_deg")


def read_journal_entries(paths: CampaignPaths, fs: FileSystem) -> list[dict]:
    """Replays every `LOGS/events_*.jsonl` file, in chronological order."""
    entries: list[dict] = []
    for log_path in sorted(fs.list_dir(paths.logs_dir)):
        if log_path.suffix != ".jsonl":
            continue
        for line in fs.read_text(log_path).splitlines():
            line = line.strip()
            if not line:
                continue
            entries.append(json.loads(line))
    return entries


def rebuild_export_context(
    name: str, paths: CampaignPaths, fs: FileSystem, *, entries: list[dict] | None = None
) -> ExportContext | None:
    """Rebuilds `name`'s export context from the journal.

    Returns `None` if the RAW is no longer in `RAW/` or if no frame was
    ever logged for this image (nothing to rebuild).
    """
    raw_candidates = [p for p in fs.list_dir(paths.raw_dir) if p.is_file() and p.stem == name]
    if not raw_candidates:
        return None
    raw_path = raw_candidates[0]

    own_entries = [
        e for e in (entries or read_journal_entries(paths, fs)) if e.get("image") == name
    ]

    source_file = ""
    orientation = "horizontal"
    frame: dict[str, object] | None = None
    for entry in own_entries:
        details = entry.get("details") or {}
        if entry.get("type") == "NAMING" and entry.get("action") == "assigned":
            source_file = details.get("source_file", source_file)
        if entry.get("type") == "FRAMING" and entry.get("action") == "orientation":
            orientation = details.get("orientation", {}).get("after", orientation)
        if all(key in details for key in _FRAME_DETAIL_KEYS):
            frame = details  # most recent wins (entries are chronological)

    if frame is None:
        return None

    return ExportContext(
        raw_path=raw_path,
        extension=raw_path.suffix,
        source_file=source_file,
        orientation=orientation,
        x=int(frame["x"]),  # type: ignore[call-overload]
        y=int(frame["y"]),  # type: ignore[call-overload]
        width=int(frame["width"]),  # type: ignore[call-overload]
        height=int(frame["height"]),  # type: ignore[call-overload]
        angle_deg=float(frame["angle_deg"]),  # type: ignore[arg-type]
    )
