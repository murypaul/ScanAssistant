"""Completeness check + regeneration.

Used by the Statistics screen, accessible both during and outside of
capture — never depends on a running queue: every check re-reads state
from disk, every regeneration rebuilds its context from the journal
(`core.recovery`) and drains immediately, reusing the same logic as
`CaptureSession.retry_error_image`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scanassistant.core.events import SessionEvent
from scanassistant.core.session import CaptureSession
from scanassistant.project.inventory import STATUS_COLUMN


@dataclass(frozen=True)
class CompletenessGap:
    """A gap found for a `done` row."""

    name: str
    raw_missing: bool
    missing_kinds: list[str] = field(default_factory=list)


def missing_export_kinds(session: CaptureSession, name: str) -> list[str]:
    """Kinds enabled in the campaign with no existing file for `name`."""
    exports = session.campaign.exports
    missing: list[str] = []
    if exports.tiff.enabled and not session.fs.exists(session.paths.tiff_dir / f"{name}.tif"):
        missing.append("tiff")
    if exports.jpeg_master.enabled and not session.fs.exists(
        session.paths.jpeg_master_dir / f"{name}.jpg"
    ):
        missing.append("jpeg_master")
    if exports.jpeg_positive.enabled and not session.fs.exists(
        session.paths.jpeg_positive_dir / f"{name}_POS.jpg"
    ):
        missing.append("jpeg_positive")
    return missing


def raw_exists(session: CaptureSession, name: str) -> bool:
    return any(p.stem == name for p in session.fs.list_dir(session.paths.raw_dir) if p.is_file())


def check_completeness(session: CaptureSession) -> list[CompletenessGap]:
    """For each `done` row: renamed RAW present + every enabled export."""
    gaps: list[CompletenessGap] = []
    for row in session.inventory.rows:
        if row.get(STATUS_COLUMN) != "done":
            continue
        name = row[session.inventory.name_column]
        raw_missing = not raw_exists(session, name)
        kinds = [] if raw_missing else missing_export_kinds(session, name)
        if raw_missing or kinds:
            gaps.append(CompletenessGap(name=name, raw_missing=raw_missing, missing_kinds=kinds))
    return gaps


def regenerate_selection(session: CaptureSession, names: list[str]) -> list[SessionEvent]:
    """ "Regenerate selection": replays `retry_error_image` for each checked name.

    Frame read back from the journal; a name with no RAW or no logged
    framing event is silently skipped here — the "automatic detection
    re-run" fallback for that specific case is deferred.
    """
    events: list[SessionEvent] = []
    for name in names:
        events.extend(session.retry_error_image(name))
    return events
