"""Content-frame review: which images need a manual look.

An image is flagged when its most recent `POSITIVE_FRAMING` journal entry
(logged on every `jpeg_positive` export, `core.session._log_positive_framing`)
is `deferred` — the automatic detector (`imaging.content_framing`) wasn't
confident enough to apply a crop. Never a dangerous state on its own: the
positive simply keeps the support frame's own crop until reviewed. Never
depends on a running session/queue: re-reads the journal from disk, same
principle as `core.completeness`.
"""

from __future__ import annotations

from scanassistant.core.fs import FileSystem
from scanassistant.core.recovery import read_journal_entries
from scanassistant.project.layout import CampaignPaths


def list_deferred_positives(paths: CampaignPaths, fs: FileSystem) -> list[str]:
    """Names whose most recent `POSITIVE_FRAMING` entry is `deferred`, in
    the order they first appear in the journal."""
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
    return [name for name in order if latest_action[name] == "deferred"]
