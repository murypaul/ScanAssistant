"""Content-frame review: which images an operator can look at, by category.

An image's category is its most recent `POSITIVE_FRAMING` journal entry's
action (logged on every `jpeg_positive` export,
`core.session._log_positive_framing`): `deferred` (the automatic detector,
`imaging.content_framing`, wasn't confident enough to apply a crop),
`applied` (applied automatically, never seen by an operator), or `manual`
(already confirmed by an operator). None of these is a dangerous state on
its own: the positive simply keeps whichever crop was last applied until
reviewed again. Never depends on a running session/queue: re-reads the
journal from disk, same principle as `core.completeness`.
"""

from __future__ import annotations

from scanassistant.core.fs import FileSystem
from scanassistant.core.recovery import read_journal_entries
from scanassistant.project.layout import CampaignPaths
from scanassistant.project.state import ContentFramingState


def list_positives_by_category(
    paths: CampaignPaths,
    fs: FileSystem,
    categories: frozenset[str],
    *,
    entries: list[dict] | None = None,
) -> list[str]:
    """Names whose most recent `POSITIVE_FRAMING` entry's action is in
    `categories` (`deferred`/`applied`/`manual`), in the order they first
    appear in the journal.

    `entries` lets a caller that already has a fresh read of the journal
    (or, for a call where staleness genuinely doesn't matter, a cached
    one) skip a second full re-read/re-parse of every `LOGS/events_*.jsonl`
    file — `None` (the default) re-reads from disk, same as before this
    parameter existed."""
    latest_action: dict[str, str] = {}
    order: list[str] = []
    for entry in entries if entries is not None else read_journal_entries(paths, fs):
        if entry.get("type") != "POSITIVE_FRAMING":
            continue
        name = entry.get("image")
        if not isinstance(name, str):
            continue
        if name not in latest_action:
            order.append(name)
        latest_action[name] = entry.get("action", "deferred")
    return [name for name in order if latest_action[name] in categories]


def reconstruct_content_frame_fraction(
    paths: CampaignPaths, fs: FileSystem, name: str
) -> tuple[float, float, float, float] | None:
    """The content frame last applied/confirmed for `name`, as fractions of
    `master.pixels`' own width/height — `None` for a `deferred` image (no
    real crop was ever applied) or one journaled before this field existed."""
    fraction: tuple[float, float, float, float] | None = None
    for entry in read_journal_entries(paths, fs):
        if entry.get("type") != "POSITIVE_FRAMING" or entry.get("image") != name:
            continue
        details = entry.get("details") or {}
        raw_fraction = details.get("content_frame_fraction")
        fraction = tuple(raw_fraction) if raw_fraction is not None else None  # type: ignore[assignment]
    return fraction


def reconstruct_content_framing_state(
    paths: CampaignPaths, fs: FileSystem, name: str
) -> ContentFramingState | None:
    """The full content-frame state last applied/confirmed for `name` — same
    source and fields as `core.session._log_positive_framing` journals,
    all of them instead of just the fraction (`reconstruct_content_frame_fraction`).
    `None` if `name` has no `POSITIVE_FRAMING` entry at all (never exported
    as `jpeg_positive`). Used to restore what an operator would already see
    on screen when reopening an already-reviewed image, e.g.
    `CaptureSession.reopen_for_correction`."""
    state: ContentFramingState | None = None
    for entry in read_journal_entries(paths, fs):
        if entry.get("type") != "POSITIVE_FRAMING" or entry.get("image") != name:
            continue
        details = entry.get("details") or {}
        raw_fraction = details.get("content_frame_fraction")
        state = ContentFramingState(
            x=int(details.get("x", 0)),
            y=int(details.get("y", 0)),
            width=int(details.get("width", 0)),
            height=int(details.get("height", 0)),
            fill=float(details.get("fill", 0.0)),
            area_ratio=float(details.get("area_ratio", 0.0)),
            outcome=str(entry.get("action", "deferred")),
            content_frame_fraction=tuple(raw_fraction) if raw_fraction is not None else None,
            angle_deg=float(details.get("angle_deg", 0.0)),
        )
    return state
