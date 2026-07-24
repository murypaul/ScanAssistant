"""Directory layout of a campaign."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SUBDIRECTORIES = ("RAW", "TIFF", "JPEG_MASTER", "JPEG_POSITIVE", "REJECTED", "BACKUP", "LOGS")


@dataclass(frozen=True)
class CampaignPaths:
    """Standard paths of a campaign, derived from its root."""

    root: Path

    @property
    def campaign_json(self) -> Path:
        return self.root / "campaign.json"

    @property
    def state_json(self) -> Path:
        return self.root / "state.json"

    @property
    def inventory_csv(self) -> Path:
        return self.root / "inventory.csv"

    @property
    def inventory_csv_bak(self) -> Path:
        return self.root / "inventory.csv.bak"

    @property
    def lock_file(self) -> Path:
        return self.root / ".lock"

    @property
    def positive_overrides_json(self) -> Path:
        return self.root / "positive_overrides.json"

    @property
    def raw_dir(self) -> Path:
        return self.root / "RAW"

    @property
    def tiff_dir(self) -> Path:
        return self.root / "TIFF"

    @property
    def jpeg_master_dir(self) -> Path:
        return self.root / "JPEG_MASTER"

    @property
    def jpeg_positive_dir(self) -> Path:
        return self.root / "JPEG_POSITIVE"

    @property
    def rejected_dir(self) -> Path:
        return self.root / "REJECTED"

    @property
    def backup_dir(self) -> Path:
        return self.root / "BACKUP"

    @property
    def logs_dir(self) -> Path:
        return self.root / "LOGS"

    @property
    def print_cache_dir(self) -> Path:
        """Downsampled linear-RAW cache for the print_engine positive
        engine, keyed by image name — regenerable, not a deliverable
        (unlike the other subdirectories, deliberately not part of
        `SUBDIRECTORIES`/`create_campaign_tree`: created lazily on first
        write, and safe to delete entirely at any time)."""
        return self.root / ".print_cache"


def create_campaign_tree(root: Path) -> CampaignPaths:
    """Creates a campaign's full directory layout.

    `.lock` (acquired on open), `inventory.csv.bak` (created before the
    first rewrite), and `positive_overrides.json` (created on the first
    manual positive override) are deliberately not created here.
    """
    paths = CampaignPaths(Path(root))
    paths.root.mkdir(parents=True, exist_ok=True)
    for name in SUBDIRECTORIES:
        (paths.root / name).mkdir(exist_ok=True)
    return paths
