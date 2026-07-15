"""Draggable slider paired with an editable numeric field, used by the
Positive settings panel (exposure/contrast/shadows/highlights).

A plain `QSlider` fills from one edge only, which is wrong for a signed
range whose default sits at the middle (exposure, contrast) rather than at
an extremity (shadows, highlights) — hence a small custom-painted track
instead. Dragging emits `live_value_changed` on every step (cheap: no disk
I/O, just a redraw of whatever preview is already in memory) and
`committed` once the gesture ends, which is what actually persists and
gets journaled — matching the spinbox it replaces, which only committed
on `editingFinished` too.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QDoubleValidator, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QWidget

from scanassistant.gui.theme import ACCENT, BORDER, BORDER_STRONG, TEXT_SECONDARY

_TRACK_HEIGHT = 4
_HANDLE_DIAMETER = 10
_HANDLE_MARGIN = _HANDLE_DIAMETER / 2  # room for the handle at both ends of the track


class _SliderTrack(QWidget):
    dragged = Signal(float)
    released = Signal()
    reset_requested = Signal()

    def __init__(
        self, minimum: float, maximum: float, *, bipolar: bool, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._minimum = minimum
        self._maximum = maximum
        self._bipolar = bipolar
        self._value = minimum
        self.setFixedHeight(16)
        self.setMinimumWidth(60)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_value(self, value: float) -> None:
        self._value = max(self._minimum, min(self._maximum, value))
        self.update()

    def _fraction(self, value: float) -> float:
        return (value - self._minimum) / (self._maximum - self._minimum)

    def _usable_width(self) -> float:
        return max(1.0, self.width() - 2 * _HANDLE_MARGIN)

    def _handle_x(self, value: float) -> float:
        """Pixel x for `value`'s handle centre — inset by `_HANDLE_MARGIN` on
        both ends so the handle circle is never clipped by the widget edge."""
        return _HANDLE_MARGIN + self._fraction(value) * self._usable_width()

    def _value_from_x(self, x: float) -> float:
        fraction = max(0.0, min(1.0, (x - _HANDLE_MARGIN) / self._usable_width()))
        return self._minimum + fraction * (self._maximum - self._minimum)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self.isEnabled():
            super().mousePressEvent(event)
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.reset_requested.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.set_value(self._value_from_x(event.position().x()))
            self.dragged.emit(self._value)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.isEnabled() and event.buttons() & Qt.MouseButton.LeftButton:
            self.set_value(self._value_from_x(event.position().x()))
            self.dragged.emit(self._value)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.released.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        mid = self.height() / 2
        track_rect = QRectF(
            _HANDLE_MARGIN, mid - _TRACK_HEIGHT / 2, self._usable_width(), _TRACK_HEIGHT
        )
        fill_color = QColor(ACCENT) if self.isEnabled() else QColor(TEXT_SECONDARY)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(BORDER))
        painter.drawRoundedRect(track_rect, _TRACK_HEIGHT / 2, _TRACK_HEIGHT / 2)

        handle_x = self._handle_x(self._value)
        if self._bipolar:
            zero_x = self._handle_x(0)
            fill_rect = QRectF(
                min(zero_x, handle_x), track_rect.top(), abs(handle_x - zero_x), _TRACK_HEIGHT
            )
            if self.isEnabled():
                painter.setPen(QColor(BORDER_STRONG))
                painter.drawLine(int(zero_x), 0, int(zero_x), self.height())
                painter.setPen(Qt.PenStyle.NoPen)
        else:
            fill_rect = QRectF(
                track_rect.left(), track_rect.top(), handle_x - track_rect.left(), _TRACK_HEIGHT
            )
        painter.setBrush(fill_color)
        painter.drawRoundedRect(fill_rect, _TRACK_HEIGHT / 2, _TRACK_HEIGHT / 2)

        painter.drawEllipse(
            QRectF(
                handle_x - _HANDLE_DIAMETER / 2,
                mid - _HANDLE_DIAMETER / 2,
                _HANDLE_DIAMETER,
                _HANDLE_DIAMETER,
            )
        )


class SliderField(QWidget):
    """`live_value_changed`: fires continuously while dragging (preview-only,
    never persisted). `committed`: fires once a change is final (drag
    released, field edited, or right-click reset to `default`) — this is
    the one the caller should persist and journal."""

    live_value_changed = Signal(float)
    committed = Signal(float)

    def __init__(
        self,
        minimum: float,
        maximum: float,
        *,
        decimals: int = 0,
        default: float = 0.0,
        bipolar: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._decimals = decimals
        self._default = default
        self._value = default

        self._track = _SliderTrack(minimum, maximum, bipolar=bipolar)
        self._track.dragged.connect(self._on_dragged)
        self._track.released.connect(self._on_released)
        self._track.reset_requested.connect(self._on_reset_requested)

        self._field = QLineEdit()
        self._field.setFixedWidth(56)
        self._field.setAlignment(Qt.AlignmentFlag.AlignRight)
        validator = QDoubleValidator(minimum, maximum, decimals, self._field)
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        self._field.setValidator(validator)
        self._field.editingFinished.connect(self._on_field_edited)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._track, 1)
        layout.addWidget(self._field)

        self._apply(default, notify=None)

    def value(self) -> float:
        return self._value

    def setValue(self, value: float) -> None:
        """Programmatic set (binding a campaign's config) — never emits."""
        self._apply(value, notify=None)

    def _apply(self, value: float, *, notify: str | None) -> None:
        value = round(value, self._decimals) if self._decimals else float(round(value))
        self._value = value
        self._track.set_value(value)
        text = f"{value:.{self._decimals}f}" if self._decimals else str(int(value))
        if self._field.text() != text:
            self._field.setText(text)
        if notify == "live":
            self.live_value_changed.emit(value)
        elif notify == "committed":
            self.committed.emit(value)

    def _on_dragged(self, value: float) -> None:
        self._apply(value, notify="live")

    def _on_released(self) -> None:
        self._apply(self._value, notify="committed")

    def _on_reset_requested(self) -> None:
        self._apply(self._default, notify="committed")

    def _on_field_edited(self) -> None:
        text = self._field.text().strip().replace(",", ".")
        try:
            value = float(text)
        except ValueError:
            self._apply(self._value, notify=None)  # revert the field to the last valid value
            return
        self._apply(value, notify="committed")
