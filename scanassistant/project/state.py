"""Model and persistence for `state.json`."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from scanassistant.utils.atomic import atomic_write_text

SCHEMA_VERSION = 1


@dataclass
class FramingState:
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    angle_deg: float = 0.0
    confidence: float = 0.0
    source: str = "auto"  # auto | manual | raw


@dataclass
class ContentFramingState:
    """Content frame within the support frame (`imaging.content_framing`),
    for the reading positive only — never applied to the master. Always
    axis-aligned (no `angle_deg`: the support frame's own deskew already
    resolves this)."""

    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    fill: float = 0.0
    area_ratio: float = 0.0
    outcome: str = "deferred"  # applied | deferred


@dataclass
class CurrentImageState:
    assigned_name: str
    source_file: str = ""
    extension: str = ""
    state: str = "IN_REVIEW"
    rotation_deg: int = 0  # 0 | 90 | 180 | 270, clockwise (V key)
    framing: FramingState = field(default_factory=FramingState)
    # None until the jpeg_positive export has run at least once for this
    # image — distinct from a present-but-"deferred" entry (tried, not
    # confident enough to apply): a reviewer tool needs to tell "never
    # processed" apart from "processed, nothing to flag".
    content_framing: ContentFramingState | None = None
    exports: dict[str, str] = field(default_factory=dict)


@dataclass
class ExportQueueEntry:
    name: str
    tasks: list[str] = field(default_factory=list)


@dataclass
class IgnoredFile:
    name: str
    size: int
    mtime: float
    reason: str


@dataclass
class ErrorImage:
    """Error tied to a single image (E-04 after retry, E-05, E-06)."""

    name: str
    code: str  # E-04 | E-05 | E-06
    message: str
    kind: str | None = None  # export kind affected (E-06 only)


@dataclass
class ProjectState:
    schema_version: int = SCHEMA_VERSION
    mode: str = "preparation"  # preparation | capture | pause
    csv_cursor: int = 0
    current_image: CurrentImageState | None = None
    export_queue: list[ExportQueueEntry] = field(default_factory=list)
    ignored_files: list[IgnoredFile] = field(default_factory=list)
    pause_queue: list[str] = field(default_factory=list)
    error_images: list[ErrorImage] = field(default_factory=list)


def load_state(path: Path) -> ProjectState:
    """Loads `state.json`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return _from_dict(data)


def save_state(state: ProjectState, path: Path) -> None:
    """Writes `state.json` atomically, on every structuring event."""
    atomic_write_text(Path(path), json.dumps(_to_dict(state), indent=2, ensure_ascii=False) + "\n")


def _to_dict(state: ProjectState) -> dict[str, Any]:
    return asdict(state)


def _from_dict(data: dict[str, Any]) -> ProjectState:
    current_image_data = data.get("current_image")
    current_image = None
    if isinstance(current_image_data, dict):
        framing_data = current_image_data.get("framing", {})
        content_framing_data = current_image_data.get("content_framing")
        content_framing = (
            ContentFramingState(**content_framing_data)
            if isinstance(content_framing_data, dict)
            else None
        )
        current_image = CurrentImageState(
            assigned_name=current_image_data["assigned_name"],
            source_file=current_image_data.get("source_file", ""),
            extension=current_image_data.get("extension", ""),
            state=current_image_data.get("state", "IN_REVIEW"),
            rotation_deg=current_image_data.get(
                "rotation_deg",
                # Reads a state.json written before the orientation -> rotation_deg
                # migration: "vertical" becomes a 90° rotation, "horizontal" 0°.
                90 if current_image_data.get("orientation") == "vertical" else 0,
            ),
            framing=FramingState(**framing_data),
            content_framing=content_framing,
            exports=current_image_data.get("exports", {}),
        )
    return ProjectState(
        schema_version=data.get("schema_version", SCHEMA_VERSION),
        mode=data.get("mode", "preparation"),
        csv_cursor=data.get("csv_cursor", 0),
        current_image=current_image,
        export_queue=[ExportQueueEntry(**e) for e in data.get("export_queue", [])],
        ignored_files=[IgnoredFile(**f) for f in data.get("ignored_files", [])],
        pause_queue=list(data.get("pause_queue", [])),
        error_images=[ErrorImage(**e) for e in data.get("error_images", [])],
    )
