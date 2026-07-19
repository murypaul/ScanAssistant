"""Backend abstraction over the physical camera.

`CameraBackend` is the pluggable interface: `FakeCameraBackend` (below) is
used by every test; the real implementation
(`camera.gphoto_backend.GphotoCameraBackend`) wraps `python-gphoto2` and
lives in its own module so that importing this one — or any other part of
`scanassistant.camera` — never imports `gphoto2`: the dependency must not
even be touched unless `camera.enabled` is true.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from scanassistant.camera.errors import CameraBusyError, CameraNotFoundError


@dataclass(frozen=True)
class LiveViewFrame:
    """One live view frame, already decoded to raw RGB — never re-decoded by
    the GUI (a `QImage` can wrap `rgb_bytes` directly)."""

    width: int
    height: int
    rgb_bytes: bytes  # tightly packed RGB888, row-major, no padding


class CameraBackend(Protocol):
    def connect(self) -> None:
        """Raises `CameraNotFoundError` or `CameraBusyError`/`CameraIOError`."""
        ...

    def disconnect(self) -> None: ...

    def set_capture_target(self, target: str) -> str:
        """Applies `target` ("card"/"sdram") and returns the value actually
        read back — some bodies silently ignore an unsupported value."""
        ...

    def start_live_view(self) -> None:
        """Raises `CameraBusyError` on the well-known transient PTP busy
        error — the caller is expected to retry with backoff."""
        ...

    def stop_live_view(self) -> None: ...

    def read_preview_frame(self) -> LiveViewFrame:
        """Blocking call — throughput is dictated by the camera/USB link."""
        ...

    def trigger_capture(self) -> None:
        """Fires the shutter. Does not wait for the resulting file: that
        file arrives through the watched folder like any other capture."""
        ...


class FakeCameraBackend:
    """Test double — no hardware, no `gphoto2` import.

    `busy_count` simulates the camera refusing `start_live_view()` that
    many times with `CameraBusyError` before succeeding, exercising the
    controller's retry/backoff. `reported_capture_target` lets a test
    simulate a body that ignores the requested value.
    """

    def __init__(
        self,
        *,
        connect_error: Exception | None = None,
        busy_count: int = 0,
        trigger_error: Exception | None = None,
        reported_capture_target: str | None = None,
    ) -> None:
        self.connect_error = connect_error
        self.busy_count = busy_count
        self.trigger_error = trigger_error
        self.reported_capture_target = reported_capture_target
        self.connected = False
        self.live_view_active = False
        self.triggered_count = 0
        self.frames_read = 0
        self._start_live_view_attempts = 0

    def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False
        self.live_view_active = False

    def set_capture_target(self, target: str) -> str:
        if not self.connected:
            raise CameraNotFoundError("not connected")
        return self.reported_capture_target or target

    def start_live_view(self) -> None:
        if not self.connected:
            raise CameraNotFoundError("not connected")
        self._start_live_view_attempts += 1
        if self._start_live_view_attempts <= self.busy_count:
            raise CameraBusyError("0x2019 device busy")
        self.live_view_active = True

    def stop_live_view(self) -> None:
        self.live_view_active = False

    def read_preview_frame(self) -> LiveViewFrame:
        if not self.connected or not self.live_view_active:
            raise CameraNotFoundError("live view not active")
        self.frames_read += 1
        shade = self.frames_read % 256
        return LiveViewFrame(width=4, height=4, rgb_bytes=bytes([shade]) * (4 * 4 * 3))

    def trigger_capture(self) -> None:
        if not self.connected:
            raise CameraNotFoundError("not connected")
        if self.trigger_error is not None:
            raise self.trigger_error
        self.triggered_count += 1
