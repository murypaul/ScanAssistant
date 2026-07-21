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

# Both `capturetarget` and `liveviewimagezoomratio` are vendor-worded PTP
# enum widgets — `get_choice(i)`/`get_value()` return whatever label
# libgphoto2's gettext catalog produces for the *host's* locale (confirmed
# on this machine: French gives "Carte mémoire"/"Affichage entier"/"100 %"
# — that last one with a non-breaking space — not the English labels a
# quick manual check without `locale.setlocale()` first turns up, which is
# `LC_ALL`-dependent and silently returns the English labels regardless of
# the desktop's actual locale). A hardcoded label, in any one language,
# is therefore never safe to `set_value()` with — it either no-ops
# silently (target language doesn't match) or, worse, "coincidentally"
# looks like it worked because the widget was already left in that state
# by an earlier call. Choice **position** is what's actually stable
# (confirmed identical ordering in both English and French), so every
# lookup here goes through it instead of any label text.
_CAPTURE_TARGET_CHOICE_INDEX = {"sdram": 0, "card": 1}  # Internal RAM, Memory card
# Confirmed against the real D750: `liveviewimagezoomratio` offers more
# than a plain on/off toggle — "Entire Display", 25 %, 50 %, 100 %, 200 %
# at choice positions 0/2/4/6/7 (1/3/5 are reserved values this body lists
# but never actually offers). Level 0 here means unzoomed; each further
# level asks the camera for a progressively tighter camera-side crop.
_LIVE_VIEW_ZOOM_CHOICE_INDEX = (0, 2, 4, 6, 7)

# Empirical scale from dragging against the real D750 (light table, 200 %
# crop): converts on-screen drag pixels into `changeafarea` units. Small
# values already move the crop noticeably (200 units shifted visible dust
# specks on the light table by a large fraction of the frame; 1000 units
# reached the edge of the holder) — this constant is a first approximation
# for a controllable drag feel, not a measured physical distance, and may
# need adjusting after real capture sessions.
_ZOOM_AREA_UNITS_PER_PIXEL = 3


def _find_choice_index(widget: gphoto2.CameraWidget, value: str) -> int | None:
    for i in range(widget.count_choices()):
        if widget.get_choice(i) == value:
            return i
    return None


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
        # Cheap and harmless if gvfs's gphoto2 monitor isn't running (the
        # normal case — see `release_gvfs_claim`): every connect attempt
        # gets this for free rather than only the operator's manual
        # Capture ▸ Release camera action, since the same USB-claim
        # conflict can just as easily hit the very first automatic
        # connect at session start.
        release_gvfs_claim()
        camera = gphoto2.Camera()
        try:
            camera.init(self._context)
        except gphoto2.GPhoto2Error as exc:
            # `init()` failing partway (e.g. right after claiming the USB
            # interface but before the PTP handshake completes) can leave
            # that claim held at the OS level — relying on `camera` simply
            # going out of scope here does *not* release it (observed:
            # the process keeps the USB device open, blocking every
            # subsequent attempt, including a fresh external one). Explicit
            # best-effort `exit()` before re-raising, so a failed connect
            # never leaks a claim into the next retry.
            with contextlib.suppress(gphoto2.GPhoto2Error):
                camera.exit(self._context)
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
            index = _CAPTURE_TARGET_CHOICE_INDEX.get(target)
            if index is not None and index < widget.count_choices():
                widget.set_value(widget.get_choice(index))
            camera.set_config(config, self._context)
            # Re-read: some bodies silently ignore an unsupported value.
            config = camera.get_config(self._context)
            widget = config.get_child_by_name("capturetarget")
            effective_label = str(widget.get_value())
            effective_index = _find_choice_index(widget, effective_label)
            for name, i in _CAPTURE_TARGET_CHOICE_INDEX.items():
                if i == effective_index:
                    return name
            return effective_label
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

    def set_live_view_zoom_level(self, level: int) -> None:
        # `liveviewimagezoomratio` — the same property the body's own
        # rear-screen zoom button drives — genuinely crops/zooms at the
        # sensor/ISP level rather than this app blowing up the same
        # low-res preview pixels. Best-effort and silent: a body without
        # this widget, an out-of-range level, or not currently in live
        # view, just keeps showing whatever it last had, not worth its
        # own error banner for a focus-check convenience.
        try:
            camera = self._require_camera()
            config = camera.get_config(self._context)
            widget = config.get_child_by_name("liveviewimagezoomratio")
            if not 0 <= level < len(_LIVE_VIEW_ZOOM_CHOICE_INDEX):
                return
            index = _LIVE_VIEW_ZOOM_CHOICE_INDEX[level]
            if index < widget.count_choices():
                widget.set_value(widget.get_choice(index))
                camera.set_config(config, self._context)
        except (gphoto2.GPhoto2Error, CameraNotFoundError):
            pass

    def move_live_view_zoom_area(self, dx: int, dy: int) -> None:
        # `changeafarea` — the Nikon AF-area coordinate field, repurposed
        # here for panning the zoom crop while it's engaged — behaves as a
        # nudge rather than an absolute position: confirmed against the
        # real D750, it reads back "0x0" right after being applied, and
        # setting it while unzoomed raises rather than silently no-oping
        # (hence the same best-effort/silent handling as the zoom level
        # above). No-op for a (0, 0) delta: not worth a round trip to the
        # camera for a drag event that didn't actually move the pointer.
        if dx == 0 and dy == 0:
            return
        try:
            camera = self._require_camera()
            config = camera.get_config(self._context)
            widget = config.get_child_by_name("changeafarea")
            area_x = round(dx * _ZOOM_AREA_UNITS_PER_PIXEL)
            area_y = round(dy * _ZOOM_AREA_UNITS_PER_PIXEL)
            widget.set_value(f"{area_x}x{area_y}")
            camera.set_config(config, self._context)
        except (gphoto2.GPhoto2Error, CameraNotFoundError):
            pass

    def read_preview_frame(self) -> LiveViewFrame:
        camera = self._require_camera()
        try:
            camera_file = gphoto2.CameraFile()
            camera.capture_preview(camera_file, self._context)
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
                    camera_file = gphoto2.CameraFile()
                    camera.file_get(
                        event_data.folder,
                        event_data.name,
                        gphoto2.GP_FILE_TYPE_NORMAL,
                        camera_file,
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
