"""Reusable "pin on top" checkbox for standalone auxiliary windows.

Windows like the keyboard-shortcuts help or the statistics screen are
usable alongside the rest of the app rather than modal; some users want to
keep them visible above the main window while they work, others don't —
this checkbox lets them choose, rather than forcing either behavior.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QWidget

from scanassistant.i18n import t


def make_pin_checkbox(window: QWidget) -> QCheckBox:
    """A checkbox that keeps `window` above the rest of the UI while checked.

    `window` must be a real top-level window (no parent, or an explicit
    `Qt.Window` flag) — pinning only makes sense for a standalone window,
    never for a child widget embedded in another one.
    """
    checkbox = QCheckBox(t("common.pin_on_top"))

    def _on_toggled(checked: bool) -> None:
        was_visible = window.isVisible()
        window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, checked)
        if was_visible:
            window.show()  # setWindowFlag() hides the window; re-show applies it

    checkbox.toggled.connect(_on_toggled)
    return checkbox
