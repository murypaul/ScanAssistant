"""Events emitted by `core.session`.

`CaptureSession.pump()` returns a list of these dataclasses on every
call: the GUI consumes them to update the screen via
`scanassistant.i18n.t()`. Deliberately a plain returned list rather than
a subscriber registry: there's only ever one possible consumer (the Qt
loop), a full pub-sub bus would be unwarranted complexity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ImageDetected:
    path: Path
    size: int


@dataclass(frozen=True)
class ImageStabilized:
    path: Path
    duration_s: float


@dataclass(frozen=True)
class StabilizationTimedOut:
    """E-03: file never stabilized."""

    path: Path


@dataclass(frozen=True)
class ImageIngested:
    name: str
    source_file: str
    via: str


@dataclass(frozen=True)
class ImageStateChanged:
    name: str
    previous: str
    new: str


@dataclass(frozen=True)
class NameConflictDetected:
    name: str
    existing_path: str


@dataclass(frozen=True)
class NameConflictResolved:
    option: int
    old: str
    new: str


@dataclass(frozen=True)
class OrientationToggled:
    """Portrait/landscape toggle (V key)."""

    name: str
    orientation: str  # horizontal | vertical


@dataclass(frozen=True)
class FramingApplied:
    """Detected/recomputed/edited frame applied to the current image."""

    name: str
    source: str  # auto | manual | raw
    level: str | None  # reliable | review | impossible | None (manual edit)
    confidence: float


@dataclass(frozen=True)
class ImageRejected:
    name: str


@dataclass(frozen=True)
class Warning:
    code: str
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CriticalError:
    code: str
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CriticalResolved:
    """The cause of a critical suspension has been resolved ("Resume processing")."""

    code: str


@dataclass(frozen=True)
class ImageErrored:
    """Error tied to a single image (E-04 after retry, E-05, E-06)."""

    name: str
    code: str
    message: str


SessionEvent = (
    ImageDetected
    | ImageStabilized
    | StabilizationTimedOut
    | ImageIngested
    | ImageStateChanged
    | NameConflictDetected
    | NameConflictResolved
    | OrientationToggled
    | FramingApplied
    | ImageRejected
    | Warning
    | CriticalError
    | CriticalResolved
    | ImageErrored
)
