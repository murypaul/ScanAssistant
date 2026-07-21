"""Serializes all access to a `CameraBackend` onto one dedicated thread.

A `gphoto2.Camera` handle is not safe to use from more than one thread at
once — live view frame reads and the remote trigger must therefore share
a single command queue rather than run on separate threads. Callbacks
(`on_connected`, `on_disconnected`, `on_frame`, `on_capture_triggered`,
`on_capture_downloaded`, `on_error`) are invoked from that background
thread; a GUI adapter is
responsible for marshalling them onto the Qt thread (e.g. via a `Signal`,
safe to emit cross-thread), never this module — it must not import
PySide6.
"""

from __future__ import annotations

import queue as queue_module
import threading
import time
from collections.abc import Callable
from enum import Enum, auto
from pathlib import Path

from scanassistant.camera.backend import CameraBackend, LiveViewFrame
from scanassistant.camera.errors import (
    CODE_LIVE_VIEW_FAILED,
    CODE_NOT_DETECTED,
    CODE_TRIGGER_FAILED,
    CODE_USB_BUSY,
    CameraBusyError,
    CameraError,
    CameraIOError,
    CameraNotFoundError,
)
from scanassistant.journal.techlog import get_logger

# Backoff before each retry of `start_live_view()` after a transient PTP
# "device busy" error — the last `None` means "give up".
_LIVE_VIEW_RETRY_DELAYS_S: tuple[float | None, ...] = (0.5, 1.0, 2.0, None)

# Same idea for `connect()` — a fresh USB device (just replugged, or just
# released from a gvfs claim) can take a moment before `libgphoto2` can
# actually talk to it.
_CONNECT_RETRY_DELAYS_S: tuple[float | None, ...] = (0.5, 1.0, 2.0, None)

_IDLE_POLL_INTERVAL_S = 0.05  # while connected but live view is off
_LIVE_VIEW_POLL_INTERVAL_S = 0.01  # while live view is on, between frame attempts

# How long a run of consecutive frame-read failures is tolerated before
# actually disconnecting. Confirmed on the real D750: the read right after
# a trigger_capture() (mirror cycling, the event-driven download wait)
# fails outright a few times before the stream picks back up on its own —
# indistinguishable, from a single failure, from the camera having actually
# left the USB port. A short grace window rides out the former without
# meaningfully delaying detection of the latter.
_READ_FAILURE_GRACE_S = 3.0


class _Command(Enum):
    CONNECT = auto()
    DISCONNECT = auto()
    START_LIVE_VIEW = auto()
    STOP_LIVE_VIEW = auto()
    SET_LIVE_VIEW_ZOOM = auto()
    TRIGGER_CAPTURE = auto()
    SHUTDOWN = auto()


class CameraController:
    def __init__(
        self,
        backend: CameraBackend,
        *,
        capture_target: str = "card",
        on_connected: Callable[[], None] | None = None,
        on_disconnected: Callable[[], None] | None = None,
        on_frame: Callable[[LiveViewFrame], None] | None = None,
        on_capture_triggered: Callable[[], None] | None = None,
        on_capture_downloaded: Callable[[list[Path]], None] | None = None,
        on_error: Callable[[CameraError], None] | None = None,
    ) -> None:
        self._backend = backend
        self._capture_target = capture_target
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._on_frame = on_frame
        self._on_capture_triggered = on_capture_triggered
        self._on_capture_downloaded = on_capture_downloaded
        self._on_error = on_error

        self._commands: queue_module.Queue[_Command] = queue_module.Queue()
        self._connected = False
        self._fps_lock = threading.Lock()
        self._fps: int | None = None
        self._last_frame_at = 0.0
        self._read_failures_since: float | None = None
        self._download_folder_lock = threading.Lock()
        self._download_folder: Path | None = None
        self._zoom_level_lock = threading.Lock()
        self._zoom_level_requested = 0
        self._zoom_level_pending = False
        self._thread = threading.Thread(target=self._run, name="scanassistant-camera", daemon=True)
        self._thread.start()

    # --- public API: each call only enqueues a command, never blocks on
    # the hardware itself, so it is always safe to call from the Qt thread.

    def connect(self) -> None:
        self._commands.put(_Command.CONNECT)

    def disconnect(self) -> None:
        self._commands.put(_Command.DISCONNECT)

    def start_live_view(self) -> None:
        self._commands.put(_Command.START_LIVE_VIEW)

    def stop_live_view(self) -> None:
        self._commands.put(_Command.STOP_LIVE_VIEW)

    def set_live_view_zoom_level(self, level: int) -> None:
        # Only the latest request ever matters (a rapid succession of wheel
        # notches only needs the backend to end up at the last level asked
        # for) — `_zoom_level_pending` keeps a fast scroll from piling up
        # redundant queue entries once one is already in flight. Matters
        # more here than it first looks: each of these is a real PTP round
        # trip (get_config + set_config), slow enough over USB2 to
        # noticeably stall live view frame delivery if several pile up
        # back-to-back.
        with self._zoom_level_lock:
            self._zoom_level_requested = level
            if self._zoom_level_pending:
                return
            self._zoom_level_pending = True
        self._commands.put(_Command.SET_LIVE_VIEW_ZOOM)

    def trigger_capture(self) -> None:
        self._commands.put(_Command.TRIGGER_CAPTURE)

    def set_live_view_fps(self, fps: int | None) -> None:
        with self._fps_lock:
            self._fps = fps

    def set_download_folder(self, folder: Path | None) -> None:
        """Where a captured file gets downloaded to, over the same PTP
        session, right after the shutter fires — normally the campaign's
        watched folder, set on capture start and cleared back to `None` on
        shutdown. `None` means "don't even try": `trigger_capture` still
        fires the shutter, but nothing is downloaded."""
        with self._download_folder_lock:
            self._download_folder = folder

    def is_connected(self) -> bool:
        """Best-effort snapshot — set only from the camera thread; a caller
        may briefly observe a stale value, never a torn one (plain `bool`
        assignment)."""
        return self._connected

    def shutdown(self) -> None:
        """Blocks until the camera thread has released the device."""
        self._commands.put(_Command.SHUTDOWN)
        self._thread.join(timeout=5.0)

    # --- background thread

    def _run(self) -> None:
        live_view_active = False
        try:
            while True:
                timeout = _LIVE_VIEW_POLL_INTERVAL_S if live_view_active else None
                try:
                    command = self._commands.get(timeout=timeout)
                except queue_module.Empty:
                    command = None

                if command is not None:
                    if command is _Command.SHUTDOWN:
                        return
                    live_view_active = self._handle_command(command, live_view_active)
                    continue

                if live_view_active:
                    live_view_active = self._read_and_emit_frame()
                else:
                    time.sleep(_IDLE_POLL_INTERVAL_S)
        finally:
            if self._connected:
                self._safe_disconnect()

    def _handle_command(self, command: _Command, live_view_active: bool) -> bool:
        try:
            if command is _Command.CONNECT:
                self._handle_connect()
                return live_view_active
            if command is _Command.DISCONNECT:
                self._safe_disconnect()
                return False
            if command is _Command.START_LIVE_VIEW:
                return self._handle_start_live_view()
            if command is _Command.STOP_LIVE_VIEW:
                if live_view_active:
                    self._safe_call(self._backend.stop_live_view)
                return False
            if command is _Command.SET_LIVE_VIEW_ZOOM:
                with self._zoom_level_lock:
                    level = self._zoom_level_requested
                    self._zoom_level_pending = False
                if self._connected:
                    self._safe_call(lambda: self._backend.set_live_view_zoom_level(level))
                return live_view_active
            if command is _Command.TRIGGER_CAPTURE:
                self._handle_trigger()
                return live_view_active
        except Exception:  # defensive: a bug here must not kill the thread
            get_logger().exception("camera command %s crashed unexpectedly", command)
        return live_view_active

    def _handle_connect(self) -> None:
        if self._connected:
            # Idempotent: callers (the initial connect attempt, `start()`
            # calling it again on every capture-session entry, the
            # periodic auto-reconnect poll) don't have to track whether a
            # previous attempt already succeeded — reconnecting for real
            # would create a second `gphoto2.Camera()` without the first
            # one ever being released.
            return
        # Same reasoning as `_handle_start_live_view`'s retry: a single
        # failed attempt right after the USB device shows up (still
        # settling, a just-killed gvfs claim not fully released yet...) is
        # far more common in practice than the camera being genuinely
        # absent — retrying here is what used to require the operator to
        # notice the error and click Capture ▸ Release camera themselves.
        for delay in _CONNECT_RETRY_DELAYS_S:
            try:
                self._backend.connect()
            except CameraBusyError:
                if delay is None:
                    self._emit_error(CODE_USB_BUSY, {})
                    return
                time.sleep(delay)
                continue
            except CameraNotFoundError:
                if delay is None:
                    self._emit_error(CODE_NOT_DETECTED, {})
                    return
                time.sleep(delay)
                continue
            except CameraIOError as exc:
                if delay is None:
                    self._emit_error(CODE_NOT_DETECTED, {"reason": str(exc)})
                    return
                time.sleep(delay)
                continue
            self._connected = True
            self._confirm_capture_target()
            if self._on_connected is not None:
                self._on_connected()
            return

    def _confirm_capture_target(self) -> None:
        try:
            effective = self._backend.set_capture_target(self._capture_target)
        except CameraIOError:
            get_logger().warning("could not set capturetarget=%s on connect", self._capture_target)
            return
        if effective != self._capture_target:
            get_logger().warning(
                "camera reports capturetarget=%s after requesting %s — "
                "captured files may not follow the usual ingestion path",
                effective,
                self._capture_target,
            )

    def _handle_start_live_view(self) -> bool:
        if not self._connected:
            self._emit_error(CODE_NOT_DETECTED, {})
            return False
        for delay in _LIVE_VIEW_RETRY_DELAYS_S:
            try:
                self._backend.start_live_view()
                self._last_frame_at = 0.0
                return True
            except CameraBusyError:
                if delay is None:
                    self._emit_error(CODE_LIVE_VIEW_FAILED, {"reason": "device_busy"})
                    return False
                time.sleep(delay)
            except CameraIOError as exc:
                self._emit_error(CODE_LIVE_VIEW_FAILED, {"reason": str(exc)})
                return False
        return False

    def _handle_trigger(self) -> None:
        if not self._connected:
            self._emit_error(CODE_NOT_DETECTED, {})
            return
        try:
            self._backend.trigger_capture()
        except CameraIOError as exc:
            self._emit_error(CODE_TRIGGER_FAILED, {"reason": str(exc)})
            return
        if self._on_capture_triggered is not None:
            self._on_capture_triggered()
        self._download_after_trigger()

    def _download_after_trigger(self) -> None:
        with self._download_folder_lock:
            folder = self._download_folder
        if folder is None:
            return
        try:
            downloaded = self._backend.download_captured_files(folder)
        except CameraIOError as exc:
            # Not surfaced as its own error banner: a silent "nothing
            # downloaded" is indistinguishable, from the GUI's side, from
            # the camera never having produced a file at all — the
            # existing watched-folder deadline already covers both.
            get_logger().warning("download after capture failed: %s", exc)
            return
        if downloaded and self._on_capture_downloaded is not None:
            self._on_capture_downloaded(downloaded)

    def _read_and_emit_frame(self) -> bool:
        """Returns whether live view is still active — a run of failures
        sustained past `_READ_FAILURE_GRACE_S` ends it (see below),
        everything else keeps it going."""
        with self._fps_lock:
            fps = self._fps
        interval = 0.0 if fps is None else 1.0 / fps
        now = time.monotonic()
        if now - self._last_frame_at < interval:
            return True
        try:
            frame = self._backend.read_preview_frame()
        except CameraIOError as exc:
            self._emit_error(CODE_LIVE_VIEW_FAILED, {"reason": str(exc)})
            now = time.monotonic()
            if self._read_failures_since is None:
                self._read_failures_since = now
            elif now - self._read_failures_since >= _READ_FAILURE_GRACE_S:
                # Sustained failure, not a momentary blip right after a
                # trigger — the camera itself is gone from the USB port
                # (unplugged, re-enumerated to a new address, put itself to
                # sleep...). Retrying forever at the live-view poll rate
                # left the controller stuck believing it was still
                # connected — actually dropping the connection here lets
                # the existing reconnect poll
                # (`CaptureScreen._camera_reconnect_timer`) pick it back up
                # on its own once the device is reachable again, instead of
                # requiring the operator to restart the app.
                self._read_failures_since = None
                self._safe_disconnect()
                return False
            return True
        self._read_failures_since = None
        self._last_frame_at = time.monotonic()
        if self._on_frame is not None:
            self._on_frame(frame)
        return True

    def _safe_disconnect(self) -> None:
        self._safe_call(self._backend.disconnect)
        self._connected = False
        if self._on_disconnected is not None:
            self._on_disconnected()

    def _safe_call(self, action: Callable[[], None]) -> None:
        try:
            action()
        except CameraIOError:
            get_logger().warning("camera backend call failed during cleanup", exc_info=True)

    def _emit_error(self, code: str, details: dict[str, object]) -> None:
        get_logger().warning("camera error %s: %s", code, details)
        if self._on_error is not None:
            self._on_error(CameraError(code=code, details=details))
