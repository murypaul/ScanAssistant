"""Tethered camera control: remote trigger + framing live view (PTP/USB).

Strictly opt-in (`config.py:CameraConfig.enabled`) and narrow in scope —
never full tethering (no remote exposure/focus control, no Wi-Fi). Never
imports PySide6: the GUI subscribes to `CameraController` through plain
callbacks, same dependency direction as `watcher`/`core`.
"""

from __future__ import annotations
