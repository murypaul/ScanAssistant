"""Camera subsystem error codes and internal backend exceptions.

`CameraError` carries a normative code + raw details, same convention as
`project.errors.ScanAssistantError` (never an already-translated message —
`gui.errors` owns the catalogued text). The internal exceptions below are
raised by a `CameraBackend` implementation and translated into a
`CameraError` by `CameraController`; they never reach the GUI directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CODE_NOT_DETECTED = "E-17"  # camera not found / not connected
CODE_USB_BUSY = "E-18"  # USB claimed by another process (e.g. gvfs on Linux Mint)
CODE_SEEN_AS_STORAGE = "E-19"  # camera set to Mass Storage instead of PTP/MTP
CODE_TRIGGER_FAILED = "E-20"  # the remote-trigger PTP call itself failed
CODE_LIVE_VIEW_FAILED = "E-21"  # live view could not be started/kept running
CODE_CAPTURE_TIMEOUT = "E-22"  # trigger succeeded camera-side, no file ever arrived


@dataclass(frozen=True)
class CameraError:
    code: str
    details: dict[str, object] = field(default_factory=dict)


class CameraBackendError(Exception):
    """Base for exceptions a `CameraBackend` implementation may raise."""


class CameraNotFoundError(CameraBackendError):
    """No camera detected, or seen as a USB storage device rather than PTP."""


class CameraBusyError(CameraBackendError):
    """PTP device busy (e.g. Nikon `0x2019` while starting live view) — transient."""


class CameraIOError(CameraBackendError):
    """Any other backend failure (disconnected mid-call, protocol error, ...)."""
