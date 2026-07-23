"""Print-engine settings panel for the positive calibration screen
(specifications/13_INVERSION_NEGATIFS.md §9, 06_INTERFACE.md §8ter).

Four groups, each starting on **Auto** (the automatic estimate, shown
read-only) with its own toggle to switch to **Manual** (editable) and back
— never a single combined on/off for the whole panel. No control for the
film-model group (toe/shoulder): DECISIONS.md I-178 fixed it as a property
of the film stock, not recomputed per image; shown here as a read-only
caption, not a dead slider.

Deliberately does *not* re-render live while dragging (`live_value_changed`,
per SliderField): a full `imaging.print_engine.render_print` measured
~16.7s on a real image (RAW decode + density-domain render, DECISIONS.md
I-182) — re-rendering on every slider tick would be unusable. Only
`committed` (drag released, field edited, or reset) requests a refresh,
via this panel's own `settled_changed`.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from scanassistant.gui.widgets.slider_field import SliderField
from scanassistant.gui.widgets.toggle_switch import ToggleSwitch
from scanassistant.i18n import t
from scanassistant.imaging.print_engine import ManualPrintOverrides
from scanassistant.project.positive_overrides import PositiveOverride

_DMIN_RANGE = (0.05, 1.0, 3)
_EXPOSURE_RANGE = (-0.5, 0.5, 3)
_CONTRAST_RANGE = (0.3, 4.0, 2)
_PAPER_BLACK_RANGE = (0.0, 0.15, 3)
_PAPER_SOFT_CLIP_RANGE = (0.5, 0.98, 2)


@dataclass(frozen=True)
class AutoValues:
    """The engine's own automatic estimate for the image currently loaded —
    shown read-only in each group while it's on Auto, so switching to
    Manual starts from a sensible value instead of a blank default."""

    dmin: tuple[float, float, float]
    exposure_shift: float
    contrast: float
    paper_black: float
    paper_soft_clip: float


class _Group(QWidget):
    """One Auto/Manual group: a toggle plus a form of `SliderField`s,
    enabled only in Manual. `committed`: any field's value became final."""

    committed = Signal()
    live = Signal()

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.auto_switch = ToggleSwitch()
        self.auto_switch.setChecked(True)
        self.auto_switch.toggled.connect(self._on_toggled)

        auto_row = QHBoxLayout()
        auto_row.addWidget(self.auto_switch)
        auto_row.addSpacing(8)
        auto_row.addWidget(QLabel(t("positive_calibration.auto")))
        auto_row.addStretch(1)

        self.form = QFormLayout()
        self._form_widget = QWidget()
        self._form_widget.setLayout(self.form)
        self._form_widget.setEnabled(False)

        layout = QVBoxLayout()
        layout.addLayout(auto_row)
        layout.addWidget(self._form_widget)
        self.group_box = QGroupBox(title)
        self.group_box.setLayout(layout)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.group_box)

    def _on_toggled(self, auto: bool) -> None:
        # The switch's own checked state *is* "Auto" (matches the "Auto"
        # label next to it, and its default-on start state) — sliders are
        # editable exactly when it's switched off. Previously this method
        # (mis-named `manual` for its bool param) enabled the form when the
        # switch turned ON, i.e. when "Auto" was activated — backwards from
        # what the label promised, and confirmed as a real bug in practice.
        self._form_widget.setEnabled(not auto)
        self.committed.emit()

    def is_manual(self) -> bool:
        return not self.auto_switch.isChecked()

    def set_manual(self, manual: bool) -> None:
        self.auto_switch.setChecked(not manual)
        self._form_widget.setEnabled(manual)

    def add_slider(self, label: str, minimum: float, maximum: float, decimals: int) -> SliderField:
        slider = SliderField(minimum, maximum, decimals=decimals, default=minimum)
        slider.live_value_changed.connect(lambda _v: self.live.emit())
        slider.committed.connect(lambda _v: self.committed.emit())
        self.form.addRow(label, slider)
        return slider


class PrintCalibrationPanel(QWidget):
    """`settled_changed`: request a full-quality preview refresh (any group
    toggled or a field committed). `live_changed`: request a cheap,
    reduced-cost refresh if the caller has one — this panel has none of
    its own (see module docstring), so callers may simply ignore it."""

    settled_changed = Signal()
    live_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.dmin_group = _Group(t("positive_calibration.group_dmin"))
        self.dmin_r = self.dmin_group.add_slider(t("positive_calibration.dmin_r"), *_DMIN_RANGE)
        self.dmin_g = self.dmin_group.add_slider(t("positive_calibration.dmin_g"), *_DMIN_RANGE)
        self.dmin_b = self.dmin_group.add_slider(t("positive_calibration.dmin_b"), *_DMIN_RANGE)

        self.exposure_group = _Group(t("positive_calibration.group_exposure"))
        self.exposure_shift = self.exposure_group.add_slider(
            t("positive_calibration.exposure_shift"), *_EXPOSURE_RANGE
        )

        # Film model: no override (DECISIONS.md I-178) — a read-only caption,
        # not a group with a dead toggle.
        film_model_box = QGroupBox(t("positive_calibration.group_film_model"))
        film_model_layout = QVBoxLayout(film_model_box)
        film_model_caption = QLabel(t("positive_calibration.film_model_caption"))
        film_model_caption.setWordWrap(True)
        film_model_caption.setProperty("role", "secondary")
        film_model_layout.addWidget(film_model_caption)

        self.paper_group = _Group(t("positive_calibration.group_paper"))
        self.contrast = self.paper_group.add_slider(
            t("positive_calibration.contrast"), *_CONTRAST_RANGE
        )
        self.advanced_toggle = QPushButton(t("positive_calibration.advanced"))
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.toggled.connect(self._on_advanced_toggled)
        self.paper_group.form.addRow(self.advanced_toggle)
        self.paper_black = self.paper_group.add_slider(
            t("positive_calibration.paper_black"), *_PAPER_BLACK_RANGE
        )
        self.paper_soft_clip = self.paper_group.add_slider(
            t("positive_calibration.paper_soft_clip"), *_PAPER_SOFT_CLIP_RANGE
        )
        self._set_advanced_visible(False)

        for group in (self.dmin_group, self.exposure_group, self.paper_group):
            group.committed.connect(self.settled_changed.emit)
            group.live.connect(self.live_changed.emit)

        layout = QVBoxLayout(self)
        layout.addWidget(self.dmin_group)
        layout.addWidget(self.exposure_group)
        layout.addWidget(film_model_box)
        layout.addWidget(self.paper_group)
        layout.addStretch(1)

    def _on_advanced_toggled(self, expanded: bool) -> None:
        self._set_advanced_visible(expanded)

    def _set_advanced_visible(self, visible: bool) -> None:
        self.paper_black.setVisible(visible)
        self.paper_soft_clip.setVisible(visible)
        label_black = self.paper_group.form.labelForField(self.paper_black)
        if label_black is not None:
            label_black.setVisible(visible)
        label_soft_clip = self.paper_group.form.labelForField(self.paper_soft_clip)
        if label_soft_clip is not None:
            label_soft_clip.setVisible(visible)

    def load(self, auto: AutoValues, override: PositiveOverride | None) -> None:
        """(Re)binds the panel to the image currently loaded — safe to call
        again on every navigation. `advanced_toggle`'s expanded/collapsed
        state is deliberately left untouched here (06_INTERFACE.md §8ter:
        persists across images within the screen session).

        Every group's switch is set with its own `committed`/`live` signals
        blocked: `set_manual` below can flip a switch's checked state
        relative to whatever the *previous* image left it on (e.g. the prior
        image had a manual override, this one doesn't) — without blocking,
        that flip alone fires `settled_changed` as if the operator had just
        edited *this* freshly-loaded image, triggering a wasted extra
        render and (once the operator's own edits start a debounced
        auto-confirm, 06_INTERFACE.md §8ter) could even mark it dirty before
        they have touched anything."""
        override = override or PositiveOverride()
        blockers = [
            QSignalBlocker(group.auto_switch)
            for group in (self.dmin_group, self.exposure_group, self.paper_group)
        ]

        self.dmin_group.set_manual(override.print_dmin is not None)
        r, g, b = override.print_dmin or auto.dmin
        self.dmin_r.setValue(r)
        self.dmin_g.setValue(g)
        self.dmin_b.setValue(b)

        self.exposure_group.set_manual(override.print_exposure_shift is not None)
        self.exposure_shift.setValue(
            override.print_exposure_shift
            if override.print_exposure_shift is not None
            else auto.exposure_shift
        )

        paper_manual = override.print_contrast is not None
        self.paper_group.set_manual(paper_manual)
        self.contrast.setValue(
            override.print_contrast if override.print_contrast is not None else auto.contrast
        )
        self.paper_black.setValue(
            override.print_paper_black
            if override.print_paper_black is not None
            else auto.paper_black
        )
        self.paper_soft_clip.setValue(
            override.print_paper_soft_clip
            if override.print_paper_soft_clip is not None
            else auto.paper_soft_clip
        )

        for blocker in blockers:
            blocker.unblock()

    def current_overrides(self) -> ManualPrintOverrides:
        """Reads the panel's current state — `None` per group still on Auto."""
        return ManualPrintOverrides(
            dmin=(
                (self.dmin_r.value(), self.dmin_g.value(), self.dmin_b.value())
                if self.dmin_group.is_manual()
                else None
            ),
            exposure_shift=(
                self.exposure_shift.value() if self.exposure_group.is_manual() else None
            ),
            contrast=(self.contrast.value() if self.paper_group.is_manual() else None),
            paper_black=(self.paper_black.value() if self.paper_group.is_manual() else None),
            paper_soft_clip=(
                self.paper_soft_clip.value() if self.paper_group.is_manual() else None
            ),
        )
