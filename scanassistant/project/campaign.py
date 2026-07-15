"""Model, validation, and persistence for `campaign.json`.

This module also handles campaign creation/opening
(`create_campaign`, `open_campaign`), tying together `layout`,
`inventory`, `state`, and `journal`.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from scanassistant.journal.journal import Journal
from scanassistant.project.errors import InvalidCampaignError, MissingInventoryError
from scanassistant.project.inventory import (
    MAX_NAME_LENGTH,
    Inventory,
    import_csv,
    load_inventory,
)
from scanassistant.project.layout import CampaignPaths, create_campaign_tree
from scanassistant.project.state import ProjectState, load_state, save_state
from scanassistant.utils.atomic import atomic_write_text

SCHEMA_VERSION = 1

DEFAULT_RAW_EXTENSIONS = [".nef", ".nrw", ".cr2", ".cr3", ".arw", ".dng", ".raf", ".orf", ".rw2"]

_INVALID_FOLDER_NAME_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


@dataclass
class EquipmentConfig:
    camera_body: str = ""
    lens: str = ""
    column_height_cm: float | None = None


@dataclass
class CaptureConfig:
    watched_folder: str = ""
    extensions: list[str] = field(default_factory=lambda: list(DEFAULT_RAW_EXTENSIONS))
    stabilization_delay_s: float = 2.0
    stabilization_timeout_s: int = 120
    watch_mode: str = "auto"  # auto | native | polling
    verify_checksum: bool = True
    # Filename suffixes to ignore in the watched folder, on top of the
    # built-in ones (watcher.monitor.IGNORED_NAME_SUFFIXES) — never a
    # replacement for them. For camera/card software producing junk-file
    # patterns the built-in list doesn't already know about.
    extra_ignored_suffixes: list[str] = field(default_factory=list)


@dataclass
class NamingConfig:
    csv_column: str = "filename"


@dataclass
class FramingConfig:
    enabled: bool = True
    default_orientation: str = "horizontal"  # horizontal | vertical
    margin_pct: float = 2.0
    reliable_threshold: float = 0.90
    review_threshold: float = 0.60
    max_deskew_deg: float = 5.0
    threshold_bias: int = 0
    size_mode: str = "native"  # native | fixed
    final_dimensions_px: list[int] = field(default_factory=lambda: [6016, 4016])


@dataclass
class TiffExportConfig:
    enabled: bool = True
    bits: int = 16  # 8 | 16
    compression: str = "lzw"  # none | lzw
    colorspace: str = "srgb"  # srgb | gray


@dataclass
class JpegMasterExportConfig:
    enabled: bool = True
    quality: int = 92
    long_edge_px: int = 0  # 0 = full size


@dataclass
class ManualPositiveSettings:
    exposure_ev: float = 0.0
    contrast: int = 0
    shadows: int = 0
    highlights: int = 0


@dataclass
class JpegPositiveExportConfig:
    enabled: bool = True
    quality: int = 90
    long_edge_px: int = 0  # 0 = full size
    mode: str = "auto"  # simple | auto | manual
    horizontal_flip: bool = True
    suffix: str = "-POS"  # appended to <NAME> in JPEG_POSITIVE/<NAME><suffix>.jpg
    manual_settings: ManualPositiveSettings = field(default_factory=ManualPositiveSettings)


@dataclass
class ExportsConfig:
    tiff: TiffExportConfig = field(default_factory=TiffExportConfig)
    jpeg_master: JpegMasterExportConfig = field(default_factory=JpegMasterExportConfig)
    jpeg_positive: JpegPositiveExportConfig = field(default_factory=JpegPositiveExportConfig)


@dataclass
class IptcConfig:
    creator: str = ""
    institution: str = ""
    copyright: str = ""
    collection: str = ""
    keywords: list[str] = field(default_factory=list)


@dataclass
class OptionsConfig:
    raw_xmp_sidecar: bool = True
    error_beep: bool = False


@dataclass
class Campaign:
    name: str
    schema_version: int = SCHEMA_VERSION
    description: str = ""
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    operator: str = ""
    institution: str = ""
    media_type: str = "photo_negative"
    negative_format: str = ""
    equipment: EquipmentConfig = field(default_factory=EquipmentConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    naming: NamingConfig = field(default_factory=NamingConfig)
    framing: FramingConfig = field(default_factory=FramingConfig)
    exports: ExportsConfig = field(default_factory=ExportsConfig)
    iptc: IptcConfig = field(default_factory=IptcConfig)
    options: OptionsConfig = field(default_factory=OptionsConfig)

    def validate(self) -> None:
        """Validates `campaign.json`. Raises `InvalidCampaignError` (E-10)."""
        if self.schema_version != SCHEMA_VERSION:
            raise InvalidCampaignError(
                "schema_version", f"unsupported schema version {self.schema_version}"
            )
        if not self.name or _INVALID_FOLDER_NAME_CHARS.search(self.name):
            raise InvalidCampaignError("name", f"not a valid folder name: {self.name!r}")

        if self.capture.watch_mode not in {"auto", "native", "polling"}:
            raise InvalidCampaignError(
                "capture.watch_mode",
                f"must be one of auto, native, polling: {self.capture.watch_mode!r}",
            )
        if not self.capture.extensions:
            raise InvalidCampaignError("capture.extensions", "must not be empty")
        for ext in self.capture.extensions:
            if not ext.startswith("."):
                raise InvalidCampaignError(
                    "capture.extensions", f"extension must start with '.': {ext!r}"
                )
        for suffix in self.capture.extra_ignored_suffixes:
            if not suffix:
                raise InvalidCampaignError(
                    "capture.extra_ignored_suffixes", "suffixes must not be empty"
                )
            if suffix.lower() in {ext.lower() for ext in self.capture.extensions}:
                raise InvalidCampaignError(
                    "capture.extra_ignored_suffixes",
                    f"{suffix!r} matches a RAW extension — would hide captured files",
                )
        if not 0.5 <= self.capture.stabilization_delay_s <= 30:
            raise InvalidCampaignError("capture.stabilization_delay_s", "must be within [0.5, 30]")
        if not 10 <= self.capture.stabilization_timeout_s <= 3600:
            raise InvalidCampaignError(
                "capture.stabilization_timeout_s", "must be within [10, 3600]"
            )

        if not self.naming.csv_column:
            raise InvalidCampaignError("naming.csv_column", "must not be empty")

        if self.framing.size_mode not in {"native", "fixed"}:
            raise InvalidCampaignError(
                "framing.size_mode", f"must be one of native, fixed: {self.framing.size_mode!r}"
            )
        if self.framing.default_orientation not in {"horizontal", "vertical"}:
            raise InvalidCampaignError(
                "framing.default_orientation", "must be one of horizontal, vertical"
            )
        if not 0 <= self.framing.margin_pct <= 20:
            raise InvalidCampaignError("framing.margin_pct", "must be within [0, 20]")
        if not 0 <= self.framing.review_threshold < self.framing.reliable_threshold <= 1:
            raise InvalidCampaignError(
                "framing.reliable_threshold",
                "reliable_threshold must be > review_threshold, both within [0, 1]",
            )
        if not 0 <= self.framing.max_deskew_deg <= 15:
            raise InvalidCampaignError("framing.max_deskew_deg", "must be within [0, 15]")
        if not -80 <= self.framing.threshold_bias <= 80:
            raise InvalidCampaignError("framing.threshold_bias", "must be within [-80, 80]")
        if len(self.framing.final_dimensions_px) != 2 or not all(
            512 <= v <= 20000 for v in self.framing.final_dimensions_px
        ):
            raise InvalidCampaignError(
                "framing.final_dimensions_px", "each dimension must be within [512, 20000]"
            )

        if self.exports.tiff.bits not in {8, 16}:
            raise InvalidCampaignError("exports.tiff.bits", "must be one of 8, 16")
        if self.exports.tiff.compression not in {"none", "lzw"}:
            raise InvalidCampaignError("exports.tiff.compression", "must be one of none, lzw")
        if self.exports.tiff.colorspace not in {"srgb", "gray"}:
            raise InvalidCampaignError("exports.tiff.colorspace", "must be one of srgb, gray")

        for label, jpeg in (
            ("jpeg_master", self.exports.jpeg_master),
            ("jpeg_positive", self.exports.jpeg_positive),
        ):
            if not 1 <= jpeg.quality <= 100:
                raise InvalidCampaignError(f"exports.{label}.quality", "must be within [1, 100]")
            if jpeg.long_edge_px != 0 and not 512 <= jpeg.long_edge_px <= 20000:
                raise InvalidCampaignError(
                    f"exports.{label}.long_edge_px", "must be 0 or within [512, 20000]"
                )

        if self.exports.jpeg_positive.mode not in {"simple", "auto", "manual"}:
            raise InvalidCampaignError(
                "exports.jpeg_positive.mode", "must be one of simple, auto, manual"
            )
        if _INVALID_FOLDER_NAME_CHARS.search(self.exports.jpeg_positive.suffix):
            raise InvalidCampaignError(
                "exports.jpeg_positive.suffix",
                "must not contain path separators or control characters",
            )
        manual = self.exports.jpeg_positive.manual_settings
        if not -3 <= manual.exposure_ev <= 3:
            raise InvalidCampaignError(
                "exports.jpeg_positive.manual_settings.exposure_ev", "must be within [-3, 3]"
            )
        if not -100 <= manual.contrast <= 100:
            raise InvalidCampaignError(
                "exports.jpeg_positive.manual_settings.contrast", "must be within [-100, 100]"
            )
        if not 0 <= manual.shadows <= 100:
            raise InvalidCampaignError(
                "exports.jpeg_positive.manual_settings.shadows", "must be within [0, 100]"
            )
        if not 0 <= manual.highlights <= 100:
            raise InvalidCampaignError(
                "exports.jpeg_positive.manual_settings.highlights", "must be within [0, 100]"
            )


def load_campaign(path: Path) -> Campaign:
    """Loads and validates `campaign.json` (E-10 if invalid, blocks opening)."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InvalidCampaignError("(file)", f"not valid JSON: {exc}") from exc
    campaign = _from_dict(data)
    campaign.validate()
    return campaign


def save_campaign(campaign: Campaign, path: Path) -> None:
    """Writes `campaign.json` atomically."""
    campaign.validate()
    atomic_write_text(
        Path(path), json.dumps(_to_dict(campaign), indent=2, ensure_ascii=False) + "\n"
    )


@dataclass
class CreatedCampaign:
    campaign: Campaign
    state: ProjectState
    inventory: Inventory
    paths: CampaignPaths


def create_campaign(
    root: Path,
    campaign: Campaign,
    csv_source: Path,
    *,
    has_header: bool = True,
    max_name_length: int = MAX_NAME_LENGTH,
) -> CreatedCampaign:
    """Creates a complete campaign on disk.

    The source CSV is fully validated (in memory, `import_csv`) before
    anything is created on disk: a rejected CSV (E-11) leaves no trace.

    `has_header`: only relevant for this one-time import of `csv_source` — the
    internal `inventory.csv` this creates always has a header row (written by
    `Inventory.save`), so reloading it later never needs this parameter.

    `max_name_length`: operator-configurable (config.json:csv.max_name_length).
    """
    campaign.validate()
    imported = import_csv(
        csv_source,
        campaign.naming.csv_column,
        has_header=has_header,
        max_name_length=max_name_length,
    )

    paths = create_campaign_tree(root)
    save_campaign(campaign, paths.campaign_json)
    imported.inventory.save(paths.inventory_csv)

    state = ProjectState(csv_cursor=imported.inventory.cursor)
    save_state(state, paths.state_json)

    journal = Journal(paths.logs_dir)
    journal.log("PROJECT", "created", details={"name": campaign.name})
    journal.log(
        "CSV",
        "imported",
        details={"rows": imported.rows_imported, "fixes": len(imported.character_fixes)},
    )

    return CreatedCampaign(
        campaign=campaign, state=state, inventory=imported.inventory, paths=paths
    )


@dataclass
class OpenedCampaign:
    campaign: Campaign
    state: ProjectState
    inventory: Inventory
    paths: CampaignPaths


def open_campaign(root: Path) -> OpenedCampaign:
    """Reloads an existing campaign (`campaign.json` + `state.json` + `inventory.csv`).

    Does not acquire the instance lock (`project.lock`, a separate
    concern) nor perform crash recovery: the caller orchestrates those
    steps around this call.
    """
    paths = CampaignPaths(Path(root))
    campaign = load_campaign(paths.campaign_json)
    state = load_state(paths.state_json)
    if not paths.inventory_csv.exists():
        raise MissingInventoryError(has_backup=paths.inventory_csv_bak.exists())
    inventory = load_inventory(paths.inventory_csv, campaign.naming.csv_column)
    inventory.cursor = state.csv_cursor
    return OpenedCampaign(campaign=campaign, state=state, inventory=inventory, paths=paths)


# --- (de)serialization -------------------------------------------------------


def _to_dict(campaign: Campaign) -> dict[str, Any]:
    return asdict(campaign)


def _from_dict(data: dict[str, Any]) -> Campaign:
    try:
        return Campaign(
            name=data["name"],
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            description=data.get("description", ""),
            created=data.get("created", ""),
            operator=data.get("operator", ""),
            institution=data.get("institution", ""),
            media_type=data.get("media_type", "photo_negative"),
            negative_format=data.get("negative_format", ""),
            equipment=EquipmentConfig(**data.get("equipment", {})),
            capture=CaptureConfig(**data.get("capture", {})),
            naming=NamingConfig(**data.get("naming", {})),
            framing=FramingConfig(**data.get("framing", {})),
            exports=_exports_from_dict(data.get("exports", {})),
            iptc=IptcConfig(**data.get("iptc", {})),
            options=OptionsConfig(**data.get("options", {})),
        )
    except KeyError as exc:
        raise InvalidCampaignError("(file)", f"missing required field: {exc}") from exc


def _exports_from_dict(data: dict[str, Any]) -> ExportsConfig:
    jpeg_positive_data = dict(data.get("jpeg_positive", {}))
    manual_settings_data = jpeg_positive_data.pop("manual_settings", {})
    return ExportsConfig(
        tiff=TiffExportConfig(**data.get("tiff", {})),
        jpeg_master=JpegMasterExportConfig(**data.get("jpeg_master", {})),
        jpeg_positive=JpegPositiveExportConfig(
            manual_settings=ManualPositiveSettings(**manual_settings_data), **jpeg_positive_data
        ),
    )
