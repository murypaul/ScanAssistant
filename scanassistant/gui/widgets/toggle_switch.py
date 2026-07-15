"""Animated on/off switch, used in place of a plain checkbox where a single
binary setting benefits from being readable at a glance (Positive settings ▸
Horizontal flip)."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from scanassistant.gui.theme import ACCENT, BORDER_STRONG

_WIDTH = 34
_HEIGHT = 18
_KNOB_COLOR = QColor("#eaf3fb")


class ToggleSwitch(QWidget):
    toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._checked = False
        self._offset = 0.0  # 0 = off, 1 = on; animated between the two
        self.setFixedSize(_WIDTH, _HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._animation = QVariantAnimation(self)
        self._animation.setDuration(120)
        self._animation.valueChanged.connect(self._on_animation_step)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool) -> None:
        if checked == self._checked:
            return
        self._checked = checked
        self._animation.stop()
        self._animation.setStartValue(self._offset)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()
        self.toggled.emit(checked)

    def _on_animation_step(self, value: float) -> None:
        self._offset = value
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        track = QColor(ACCENT) if self._offset > 0.5 else QColor(BORDER_STRONG)
        painter.setBrush(track)
        rect = QRectF(0, 0, self.width(), self.height())
        painter.drawRoundedRect(rect, self.height() / 2, self.height() / 2)

        knob_diameter = self.height() - 4
        travel = self.width() - knob_diameter - 4
        knob_x = 2 + self._offset * travel
        painter.setBrush(_KNOB_COLOR)
        painter.drawEllipse(QRectF(knob_x, 2, knob_diameter, knob_diameter))
