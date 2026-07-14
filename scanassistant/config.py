"""Global user configuration (`config.json`).

Distinct from `campaign.json` (campaign settings, `project/campaign.py`):
this module covers preferences valid for the whole application,
regardless of which campaign is open.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import platformdirs

from scanassistant.utils.atomic import atomic_write_text

SCHEMA_VERSION = 1
APP_NAME = "scanassistant"
MAX_RECENT_PROJECTS = 10


@dataclass
class GeneralConfig:
    reopen_last: bool = True
    recent_projects: list[str] = field(default_factory=list)

    def with_recent_project(self, path: str) -> GeneralConfig:
        """Moves `path` to the front of recent projects (deduplicated, max 10)."""
        entries = [p for p in self.recent_projects if p != path]
        entries.insert(0, path)
        return GeneralConfig(
            reopen_last=self.reopen_last, recent_projects=entries[:MAX_RECENT_PROJECTS]
        )


@dataclass
class UiConfig:
    brightness: str = "normal"  # normal | dimmed | minimal
    language: str = "en"  # en for now; fr planned


@dataclass
class ProcessingConfig:
    workers: int = 1  # [1;4]
    drain_on_exit: bool = True


@dataclass
class PathsConfig:
    exiftool: str = ""  # empty = search the PATH automatically


@dataclass
class WatchConfig:
    poll_interval_s: float = 1.0  # [0.5;10]


@dataclass
class ThresholdsConfig:
    disk_warn_gb: int = 10  # [1;500]
    disk_critical_gb: int = 2  # [1;100], must be < disk_warn_gb


@dataclass
class GlobalConfig:
    schema_version: int = SCHEMA_VERSION
    general: GeneralConfig = field(default_factory=GeneralConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    thresholds: ThresholdsConfig = field(default_factory=ThresholdsConfig)

    def validate(self) -> None:
        """Checks the normative bounds. Raises `ValueError` otherwise."""
        if not 1 <= self.processing.workers <= 4:
            raise ValueError("processing.workers must be within [1, 4]")
        if not 0.5 <= self.watch.poll_interval_s <= 10:
            raise ValueError("watch.poll_interval_s must be within [0.5, 10]")
        if not 1 <= self.thresholds.disk_warn_gb <= 500:
            raise ValueError("thresholds.disk_warn_gb must be within [1, 500]")
        if not 1 <= self.thresholds.disk_critical_gb <= 100:
            raise ValueError("thresholds.disk_critical_gb must be within [1, 100]")
        if not self.thresholds.disk_critical_gb < self.thresholds.disk_warn_gb:
            raise ValueError("thresholds.disk_critical_gb must be < thresholds.disk_warn_gb")
        if self.ui.brightness not in {"normal", "dimmed", "minimal"}:
            raise ValueError("ui.brightness must be one of: normal, dimmed, minimal")
        if self.ui.language not in {"en"}:
            raise ValueError("ui.language must be one of: en")
        if len(self.general.recent_projects) > MAX_RECENT_PROJECTS:
            raise ValueError(
                f"general.recent_projects must contain at most {MAX_RECENT_PROJECTS} entries"
            )


def config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME))


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config(path: Path | None = None) -> GlobalConfig:
    """Loads `config.json`; returns defaults if absent."""
    path = path or config_path()
    if not path.exists():
        return GlobalConfig()
    data = json.loads(path.read_text(encoding="utf-8"))
    config = _from_dict(data)
    config.validate()
    return config


def save_config(config: GlobalConfig, path: Path | None = None) -> None:
    """Writes `config.json` atomically."""
    config.validate()
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(asdict(config), indent=2, ensure_ascii=False) + "\n")


def _from_dict(data: dict) -> GlobalConfig:
    return GlobalConfig(
        schema_version=data.get("schema_version", SCHEMA_VERSION),
        general=GeneralConfig(**data.get("general", {})),
        ui=UiConfig(**data.get("ui", {})),
        processing=ProcessingConfig(**data.get("processing", {})),
        paths=PathsConfig(**data.get("paths", {})),
        watch=WatchConfig(**data.get("watch", {})),
        thresholds=ThresholdsConfig(**data.get("thresholds", {})),
    )
