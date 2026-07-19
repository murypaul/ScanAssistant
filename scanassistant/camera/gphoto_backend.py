"""Real `CameraBackend` implementation, backed by `python-gphoto2`.

Only imported when `config.json:camera.enabled` is true (wired in
`app_context.py`) — never at module load time from anywhere else in
`scanassistant.camera`, so a user without a tethered camera never pays for
(or hits install/USB-permission issues from) `libgphoto2`.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import time
from pathlib import Path
from typing import NoReturn

import gphoto2
from PIL import Image

from scanassistant.camera.backend import LiveViewFrame
from scanassistant.camera.errors import CameraBusyError, CameraIOError, CameraNotFoundError

# `python-gphoto2` folds the PTP response code into the exception's own
# message text (e.g. "-53: Could not claim the USB device", "0x2019: PTP
# Device Busy") rather than exposing a stable set of named constants for
# every protocol-level condition — matching on well-known substrings is
# the same approach `gphoto2` CLI users rely on in practice (see
# IMPLEMENTATION_NOTES.md). Exercised against a real D750: the "Could not
# claim the USB device" case below is by far the most common one in
# practice, not a rare edge case — see `release_gvfs_claim`.
_NOT_FOUND_MARKERS = ("could not claim the usb device", "no camera found", "model not found")
_DEVICE_BUSY_MARKERS = ("0x2019", "device busy")

# GNOME/Nemo auto-mounts any PTP-mode camera the instant it's plugged in
# (Nemo ▸ Devices, desktop notifications...); the resulting exclusive USB
# claim is what `_NOT_FOUND_MARKERS` above actually catches most of the
# time — `libgphoto2` reports it identically to the camera being genuinely
# absent. Killing the daemons (rather than just unmounting) is deliberate:
# gvfs mounts are D-Bus-activatable, so anything that enumerates volumes
# afterwards (a file-open dialog, Nemo's own background poll...) would
# just respawn the monitor and reclaim the camera again if it were merely
# unmounted.
_GVFS_GPHOTO2_PROCESSES = ("gvfs-gphoto2-volume-monitor", "gvfsd-gphoto2")
_RELEASE_TIMEOUT_S = 5


def release_gvfs_claim() -> None:
    """Menu action (Capture ▸ Release camera from file manager). Never
    raises: `killall` finding nothing to kill is the normal case once this
    has already been done once, not an error worth surfacing — the
    connect attempt right after this call is the real signal of whether
    it helped."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["killall", *_GVFS_GPHOTO2_PROCESSES],
            capture_output=True,
            timeout=_RELEASE_TIMEOUT_S,
        )

# How long `download_captured_files` waits, in total, for the RAW produced
# by the trigger just sent — comfortably under the GUI's own 15 s
# capture-trigger deadline (`gui/screens/capture.py`) so that deadline
# still gets to fire on genuine silence instead of this wait swallowing it.
_DOWNLOAD_WAIT_TIMEOUT_S = 10.0
_DOWNLOAD_POLL_MS = 500


def _raise_for_gphoto_error(exc: gphoto2.GPhoto2Error) -> NoReturn:
    message = str(exc).lower()
    if any(marker in message for marker in _NOT_FOUND_MARKERS):
        raise CameraNotFoundError(str(exc)) from exc
    if any(marker in message for marker in _DEVICE_BUSY_MARKERS):
        raise CameraBusyError(str(exc)) from exc
    raise CameraIOError(str(exc)) from exc


class GphotoCameraBackend:
    """One instance = one camera handle, used from exactly one thread
    (`CameraController`'s background thread) — never shared."""

    def __init__(self) -> None:
        self._camera: gphoto2.Camera | None = None
        self._context = gphoto2.Context()

    def connect(self) -> None:
        camera = gphoto2.Camera()
        try:
            camera.init(self._context)
        except gphoto2.GPhoto2Error as exc:
            _raise_for_gphoto_error(exc)
        self._camera = camera

    def disconnect(self) -> None:
        if self._camera is not None:
            try:
                self._camera.exit(self._context)
            finally:
                self._camera = None

    def set_capture_target(self, target: str) -> str:
        camera = self._require_camera()
        try:
            config = camera.get_config(self._context)
            widget = config.get_child_by_name("capturetarget")
            widget.set_value(target)
            camera.set_config(config, self._context)
            # Re-read: some bodies silently ignore an unsupported value.
            config = camera.get_config(self._context)
            return str(config.get_child_by_name("capturetarget").get_value())
        except gphoto2.GPhoto2Error as exc:
            _raise_for_gphoto_error(exc)

    def start_live_view(self) -> None:
        camera = self._require_camera()
        try:
            config = camera.get_config(self._context)
            widget = config.get_child_by_name("viewfinder")
            widget.set_value(1)
            camera.set_config(config, self._context)
        except gphoto2.GPhoto2Error as exc:
            _raise_for_gphoto_error(exc)

    def stop_live_view(self) -> None:
        camera = self._require_camera()
        try:
            config = camera.get_config(self._context)
            widget = config.get_child_by_name("viewfinder")
            widget.set_value(0)
            camera.set_config(config, self._context)
        except gphoto2.GPhoto2Error:
            pass  # best-effort: about to disconnect or already torn down

    def read_preview_frame(self) -> LiveViewFrame:
        camera = self._require_camera()
        try:
            camera_file = camera.capture_preview(self._context)
            jpeg_bytes = camera_file.get_data_and_size()
        except gphoto2.GPhoto2Error as exc:
            _raise_for_gphoto_error(exc)
        image = Image.open(io.BytesIO(bytes(jpeg_bytes))).convert("RGB")
        return LiveViewFrame(width=image.width, height=image.height, rgb_bytes=image.tobytes())

    def trigger_capture(self) -> None:
        camera = self._require_camera()
        try:
            camera.trigger_capture(self._context)
        except gphoto2.GPhoto2Error as exc:
            _raise_for_gphoto_error(exc)

    def download_captured_files(self, destination_dir: Path) -> list[Path]:
        camera = self._require_camera()
        written: list[Path] = []
        deadline = time.monotonic() + _DOWNLOAD_WAIT_TIMEOUT_S
        try:
            while True:
                remaining_s = deadline - time.monotonic()
                if remaining_s <= 0:
                    break
                wait_ms = min(_DOWNLOAD_POLL_MS, max(1, int(remaining_s * 1000)))
                event_type, event_data = camera.wait_for_event(wait_ms, self._context)
                if event_type == gphoto2.GP_EVENT_FILE_ADDED:
                    camera_file = camera.file_get(
                        event_data.folder,
                        event_data.name,
                        gphoto2.GP_FILE_TYPE_NORMAL,
                        self._context,
                    )
                    local_path = destination_dir / event_data.name
                    camera_file.save(str(local_path))
                    written.append(local_path)
                elif event_type == gphoto2.GP_EVENT_CAPTURE_COMPLETE and written:
                    # The D750 reports FILE_ADDED before CAPTURE_COMPLETE —
                    # once at least one file is down and the camera signals
                    # it's done, there's nothing left worth waiting for.
                    break
        except gphoto2.GPhoto2Error as exc:
            _raise_for_gphoto_error(exc)
        return written

    def _require_camera(self) -> gphoto2.Camera:
        if self._camera is None:
            raise CameraNotFoundError("not connected")
        return self._camera
