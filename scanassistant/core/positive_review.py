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


def list_positives_by_category(
    paths: CampaignPaths, fs: FileSystem, categories: frozenset[str]
) -> list[str]:
    """Names whose most recent `POSITIVE_FRAMING` entry's action is in
    `categories` (`deferred`/`applied`/`manual`), in the order they first
    appear in the journal."""
    latest_action: dict[str, str] = {}
    order: list[str] = []
    for entry in read_journal_entries(paths, fs):
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
