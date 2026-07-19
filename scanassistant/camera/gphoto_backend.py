"""Real `CameraBackend` implementation, backed by `python-gphoto2`.

Only imported when `config.json:camera.enabled` is true (wired in
`app_context.py`) — never at module load time from anywhere else in
`scanassistant.camera`, so a user without a tethered camera never pays for
(or hits install/USB-permission issues from) `libgphoto2`.
"""

from __future__ import annotations

import io
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
# IMPLEMENTATION_NOTES.md). Not yet exercised against a real D750
# (open point, IMPLEMENTATION_NOTES.md §5).
_NOT_FOUND_MARKERS = ("could not claim the usb device", "no camera found", "model not found")
_DEVICE_BUSY_MARKERS = ("0x2019", "device busy")


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

    def _require_camera(self) -> gphoto2.Camera:
        if self._camera is None:
            raise CameraNotFoundError("not connected")
        return self._camera
