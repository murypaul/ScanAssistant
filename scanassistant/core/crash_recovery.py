"""Recovery after an unclean shutdown.

Called once, right after constructing a `CaptureSession`, when
`project.lock.acquire_lock()` recovered a lock left by a dead PID
(`ProjectLock.was_stale`). Operates only through `CaptureSession`'s public
surface (state, export queue, journal) — no data of its own, everything
lives in `session.state` to stay consistent with the rest of the core.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scanassistant.core.completeness import missing_export_kinds
from scanassistant.core.ingest import INGEST_TEMP_PREFIX
from scanassistant.core.queue import EXPORT_TASK_KINDS, ExportContext
from scanassistant.core.recovery import read_journal_entries, rebuild_export_context
from scanassistant.core.session import CaptureSession
from scanassistant.project.inventory import STATUS_COLUMN
from scanassistant.project.state import IgnoredFile


@dataclass(frozen=True)
class RecoveryReport:
    """Recovery summary: 3-5 lines for the "Recovery" panel."""

    orphaned_temp_files: list[str] = field(default_factory=list)
    duplicate_leftovers: list[str] = field(default_factory=list)
    finalized_image: str | None = None
    requeued_for_regeneration: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.orphaned_temp_files
            or self.duplicate_leftovers
            or self.finalized_image
            or self.requeued_for_regeneration
        )

    def summary_lines(self) -> list[str]:
        """Short summary lines, e.g. "1 image to finalize, 2 exports to regen"."""
        lines: list[str] = []
        if self.finalized_image:
            lines.append(f"1 image to finalize: {self.finalized_image}")
        if self.requeued_for_regeneration:
            lines.append(f"{len(self.requeued_for_regeneration)} export(s) to regenerate")
        if self.duplicate_leftovers:
            lines.append(
                f"{len(self.duplicate_leftovers)} duplicate source file(s) marked as processed"
            )
        if self.orphaned_temp_files:
            lines.append(f"{len(self.orphaned_temp_files)} orphaned temp file(s) removed")
        return lines or ["Nothing to recover."]


def perform_crash_recovery(session: CaptureSession) -> RecoveryReport:
    """Diagnoses and cleans up after recovering a lock left by a dead PID."""
    orphaned = _clean_orphaned_temp_files(session)
    duplicates = _detect_duplicate_leftovers(session)
    finalized = _finalize_in_review_image(session)
    # `finalized` was just queued with an already-known context (read
    # directly from `CurrentImageState`, not from the journal): exclude it
    # from the reconstruction below, which would otherwise overwrite it
    # with a context it can't find if no FRAMING event has been logged yet
    # for this image (the normal case right after ingestion).
    already_handled = {finalized} if finalized else set()
    journal_entries = read_journal_entries(session.paths, session.fs)
    requeued = _rebuild_pending_export_queue(session, journal_entries, exclude=already_handled)
    already_handled |= set(requeued)
    requeued += _requeue_incomplete_done_rows(session, journal_entries, exclude=already_handled)

    report = RecoveryReport(
        orphaned_temp_files=orphaned,
        duplicate_leftovers=duplicates,
        finalized_image=finalized,
        requeued_for_regeneration=requeued,
    )
    session.journal.log("SYSTEM", "crash_recovery", details={"report": report.summary_lines()})
    session.persist_state()
    return report


def _clean_orphaned_temp_files(session: CaptureSession) -> list[str]:
    """Removes orphaned `RAW/.ingest_*` files (unverified cross-volume copy)."""
    removed: list[str] = []
    for path in session.fs.list_dir(session.paths.raw_dir):
        if path.name.startswith(INGEST_TEMP_PREFIX):
            session.fs.remove(path)
            removed.append(path.name)
    return removed


def _detect_duplicate_leftovers(session: CaptureSession) -> list[str]:
    """Move completed but source not removed — crash happened in between."""
    watched = session.monitor.folder
    if not session.fs.exists(watched):
        return []
    ingested = [
        e.get("details", {})
        for e in read_journal_entries(session.paths, session.fs)
        if e.get("type") == "FILE" and e.get("action") == "ingested"
    ]
    known_ignored = {f.name for f in session.state.ignored_files}
    duplicates: list[str] = []
    for path in session.fs.list_dir(watched):
        if not path.is_file() or path.name in known_ignored:
            continue
        try:
            stat = session.fs.stat(path)
        except OSError:
            continue
        matched = any(
            details.get("from") == path.name
            and details.get("size") == stat.size
            and details.get("mtime") == stat.mtime
            for details in ingested
        )
        if not matched:
            continue
        session.state.ignored_files.append(
            IgnoredFile(name=path.name, size=stat.size, mtime=stat.mtime, reason="leftover")
        )
        session.journal.log(
            "CAPTURE", "ignored", level="warn", details={"reason": "leftover", "path": str(path)}
        )
        duplicates.append(path.name)
    return duplicates


def _finalize_in_review_image(session: CaptureSession) -> str | None:
    """IN_REVIEW at crash time, RAW present, `todo` row → validated by default.

    Builds its `ExportContext` straight from `current.framing`, not
    `core.recovery.rebuild_export_context` — no positive-review override can
    exist for this image yet, since it was still `IN_REVIEW` (never
    exported once, let alone manually reviewed) when the crash happened."""
    current = session.state.current_image
    if current is None or current.state != "IN_REVIEW":
        return None
    name = current.assigned_name
    raw_path = session.paths.raw_dir / f"{name}{current.extension}"
    if not session.fs.exists(raw_path):
        return None  # should never happen (verified ingestion); assert nothing, just be safe

    context = ExportContext(
        raw_path=raw_path,
        extension=current.extension,
        source_file=current.source_file,
        rotation_deg=current.rotation_deg,
        x=current.framing.x,
        y=current.framing.y,
        width=current.framing.width,
        height=current.framing.height,
        angle_deg=current.framing.angle_deg,
    )
    session.enqueue_export_context(name, list(EXPORT_TASK_KINDS), context)
    session.validate_current()
    return name


def _rebuild_pending_export_queue(
    session: CaptureSession, journal_entries: list[dict], *, exclude: set[str]
) -> list[str]:
    """Non-empty `export_queue` (tasks without context) → context rebuilt and re-queued.

    `exclude`: names already re-queued with a known context by another
    recovery step (`_finalize_in_review_image`) — left untouched, otherwise
    they'd be overwritten by a journal-based reconstruction that can fail
    (no FRAMING event logged yet for a just-ingested image).
    """
    stale_entries = [e for e in session.export_queue.to_state_entries() if e.name not in exclude]
    if not stale_entries:
        return []
    for entry in stale_entries:
        session.export_queue.cancel(entry.name)
    rebuilt: list[str] = []
    for entry in stale_entries:
        context = rebuild_export_context(
            entry.name, session.paths, session.fs, entries=journal_entries
        )
        if context is None:
            continue  # nothing to rebuild: task lost, but no corruption either
        session.enqueue_export_context(entry.name, entry.tasks, context)
        rebuilt.append(entry.name)
    return rebuilt


def _requeue_incomplete_done_rows(
    session: CaptureSession, journal_entries: list[dict], *, exclude: set[str]
) -> list[str]:
    """`done` row with incomplete exports → queued for regeneration."""
    current_name = (
        session.state.current_image.assigned_name if session.state.current_image else None
    )
    already_queued = {t.name for t in session.export_queue.to_state_entries()} | exclude
    requeued: list[str] = []
    for row in session.inventory.rows:
        name = row[session.inventory.name_column]
        if row.get(STATUS_COLUMN) != "done" or name == current_name or name in already_queued:
            continue
        missing = missing_export_kinds(session, name)
        if not missing:
            continue
        context = rebuild_export_context(name, session.paths, session.fs, entries=journal_entries)
        if context is None:
            continue
        session.enqueue_export_context(name, missing, context)
        requeued.append(name)
    return requeued
