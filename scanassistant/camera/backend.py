"""Backend abstraction over the physical camera.

`CameraBackend` is the pluggable interface: `FakeCameraBackend` (below) is
used by every test; the real implementation
(`camera.gphoto_backend.GphotoCameraBackend`) wraps `python-gphoto2` and
lives in its own module so that importing this one — or any other part of
`scanassistant.camera` — never imports `gphoto2`: the dependency must not
even be touched unless `camera.enabled` is true.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from scanassistant.camera.errors import CameraBusyError, CameraNotFoundError


def is_available() -> bool:
    """Whether the `gphoto2` binding is installed in this venv.

    `find_spec` only locates the module, never imports it — same "not even
    touched unless enabled" guarantee as the rest of this file, so this is
    safe to call before `camera.enabled` is actually turned on (that's the
    point: `preferences.py` calls it right when the operator flips the
    switch, to decide whether to offer installing it first)."""
    return importlib.util.find_spec("gphoto2") is not None


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

    def set_live_view_zoom_level(self, level: int) -> None:
        """Best-effort camera-side live view zoom/crop step (the same
        feature a Nikon body's own rear-screen zoom button drives) —
        genuinely more detail to judge focus by, not this app's own
        digital zoom/pan blowing up the same low-res preview pixels. `0`
        is unzoomed; higher levels ask for progressively tighter crops, up
        to whatever the body actually offers. Never raises: a body that
        doesn't support a given level, or isn't in live view yet, just
        keeps showing whatever it last had."""
        ...

    def read_preview_frame(self) -> LiveViewFrame:
        """Blocking call — throughput is dictated by the camera/USB link."""
        ...

    def trigger_capture(self) -> None:
        """Fires the shutter. Does not wait for the resulting file — see
        `download_captured_files` for that."""
        ...

    def download_captured_files(self, destination_dir: Path) -> list[Path]:
        """Waits for the file(s) produced by the capture just triggered and
        downloads each into `destination_dir` under its camera-side name,
        over the same USB/PTP session — no second connection to the camera
        is ever opened. Returns the local paths written, in arrival order.

        A plain "nothing arrived" is not an error: it returns `[]` and lets
        the caller's own watched-folder deadline surface the silence,
        exactly as it would for a manually transferred file."""
        ...


class FakeCameraBackend:
    """Test double — no hardware, no `gphoto2` import.

    `busy_count` simulates the camera refusing `start_live_view()` that
    many times with `CameraBusyError` before succeeding, exercising the
    controller's retry/backoff. `reported_capture_target` lets a test
    simulate a body that ignores the requested value. `captured_file_names`
    is what `download_captured_files` "produces" after each trigger — empty
    by default, i.e. nothing ever arrives (matches a fresh double not wired
    for the download path).
    """

    def __init__(
        self,
        *,
        connect_error: Exception | None = None,
        busy_count: int = 0,
        trigger_error: Exception | None = None,
        reported_capture_target: str | None = None,
        captured_file_names: tuple[str, ...] = (),
        download_error: Exception | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.connect_error = connect_error
        self.busy_count = busy_count
        self.trigger_error = trigger_error
        self.reported_capture_target = reported_capture_target
        self.captured_file_names = captured_file_names
        self.download_error = download_error
        self.read_error = read_error
        self.connected = False
        self.live_view_active = False
        self.triggered_count = 0
        self.frames_read = 0
        self.downloaded_files: list[Path] = []
        self.zoom_level_requests: list[int] = []
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

    def set_live_view_zoom_level(self, level: int) -> None:
        self.zoom_level_requests.append(level)

    def read_preview_frame(self) -> LiveViewFrame:
        if not self.connected or not self.live_view_active:
            raise CameraNotFoundError("live view not active")
        if self.read_error is not None:
            raise self.read_error
        self.frames_read += 1
        shade = self.frames_read % 256
        return LiveViewFrame(width=4, height=4, rgb_bytes=bytes([shade]) * (4 * 4 * 3))

    def trigger_capture(self) -> None:
        if not self.connected:
            raise CameraNotFoundError("not connected")
        if self.trigger_error is not None:
            raise self.trigger_error
        self.triggered_count += 1

    def download_captured_files(self, destination_dir: Path) -> list[Path]:
        if not self.connected:
            raise CameraNotFoundError("not connected")
        if self.download_error is not None:
            raise self.download_error
        written = []
        for name in self.captured_file_names:
            path = destination_dir / name
            path.write_bytes(b"fake-raw-data")
            written.append(path)
        self.downloaded_files.extend(written)
        return written
