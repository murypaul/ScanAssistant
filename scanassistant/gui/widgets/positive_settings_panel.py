"""Live positive-mode settings side panel.

`10_PARAMETRES.md` marks the JPEG positive mode, manual settings, and
horizontal flip as editable "à chaud" (live) — but the only place that
actually let an operator change them was the project screen, outside
capture. This panel closes that gap: an inline, non-modal side panel (no
floating dialog during capture, per `06_INTERFACE.md` §3), consistent with
the existing "Export queue" / "Session history" docks.

Changes apply to the shared, mutable `Campaign` object directly: any export
task not yet drained (including the current image's, once validated) picks
them up automatically (`core.export_runner.MasterExportRunner` reads
`campaign.exports.jpeg_positive` at task-execution time, not from a frozen
snapshot) — already-written positives are not regenerated retroactively.

`live_changed` fires on every in-memory update, including mid-drag on a
slider, so the caller can refresh an on-screen preview immediately;
`setting_changed` only fires once a value is final, and is what should
actually be persisted and journaled.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from scanassistant.gui.widgets.slider_field import SliderField
from scanassistant.gui.widgets.toggle_switch import ToggleSwitch
from scanassistant.i18n import t
from scanassistant.project.campaign import JpegPositiveExportConfig


class PositiveSettingsPanel(QWidget):
    """`setting_changed(key, before, after)`: the caller persists + journals it.
    `live_changed()`: the caller refreshes whatever preview is on screen."""

    setting_changed = Signal(str, object, object)
    live_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config: JpegPositiveExportConfig | None = None
        self._loading = False
        # Last value actually persisted — the "before" side of a commit's
        # diff. Distinct from `self._config`, which a live (mid-drag) update
        # already mutated by the time the commit arrives.
        self._committed_exposure_ev = 0.0
        self._committed_contrast = 0
        self._committed_shadows = 0
        self._committed_highlights = 0

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(t("wizard.step5.mode_simple"), "simple")
        self.mode_combo.addItem(t("wizard.step5.mode_auto"), "auto")
        self.mode_combo.addItem(t("wizard.step5.mode_manual"), "manual")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self.flip_switch = ToggleSwitch()
        self.flip_switch.toggled.connect(self._on_flip_changed)
        flip_row = QHBoxLayout()
        flip_row.addWidget(self.flip_switch)
        flip_row.addSpacing(8)
        flip_row.addWidget(QLabel(t("wizard.step5.horizontal_flip")))
        flip_row.addStretch(1)

        self.exposure_slider = SliderField(-3.0, 3.0, decimals=1, default=0.0, bipolar=True)
        self.exposure_slider.live_value_changed.connect(self._on_exposure_live)
        self.exposure_slider.committed.connect(self._on_exposure_committed)

        self.contrast_slider = SliderField(-100, 100, decimals=0, default=0, bipolar=True)
        self.contrast_slider.live_value_changed.connect(self._on_contrast_live)
        self.contrast_slider.committed.connect(self._on_contrast_committed)

        self.shadows_slider = SliderField(0, 100, decimals=0, default=0, bipolar=False)
        self.shadows_slider.live_value_changed.connect(self._on_shadows_live)
        self.shadows_slider.committed.connect(self._on_shadows_committed)

        self.highlights_slider = SliderField(0, 100, decimals=0, default=0, bipolar=False)
        self.highlights_slider.live_value_changed.connect(self._on_highlights_live)
        self.highlights_slider.committed.connect(self._on_highlights_committed)

        manual_form = QFormLayout()
        manual_form.addRow(t("wizard.step5.exposure_ev"), self.exposure_slider)
        manual_form.addRow(t("wizard.step5.contrast"), self.contrast_slider)
        manual_form.addRow(t("wizard.step5.shadows"), self.shadows_slider)
        manual_form.addRow(t("wizard.step5.highlights"), self.highlights_slider)
        self.manual_group = QGroupBox(t("wizard.step5.manual_group"))
        self.manual_group.setLayout(manual_form)

        form = QFormLayout()
        form.addRow(t("wizard.step5.mode"), self.mode_combo)
        form.addRow(flip_row)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.manual_group)
        layout.addStretch(1)

    def load(self, config: JpegPositiveExportConfig) -> None:
        """(Re)binds the panel to a campaign's live config — safe to call again."""
        self._config = config
        self._loading = True
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(config.mode))
        self.flip_switch.setChecked(config.horizontal_flip)
        manual = config.manual_settings
        self.exposure_slider.setValue(manual.exposure_ev)
        self.contrast_slider.setValue(manual.contrast)
        self.shadows_slider.setValue(manual.shadows)
        self.highlights_slider.setValue(manual.highlights)
        self._committed_exposure_ev = manual.exposure_ev
        self._committed_contrast = manual.contrast
        self._committed_shadows = manual.shadows
        self._committed_highlights = manual.highlights
        self.manual_group.setEnabled(config.mode == "manual")
        self._loading = False

    def clear_panel(self) -> None:
        self._config = None

    def _on_mode_changed(self, _index: int) -> None:
        if self._loading or self._config is None:
            return
        before = self._config.mode
        after = self.mode_combo.currentData()
        self.manual_group.setEnabled(after == "manual")
        if after == before:
            return
        self._config.mode = after
        self.live_changed.emit()
        self.setting_changed.emit("exports.jpeg_positive.mode", before, after)

    def _on_flip_changed(self, checked: bool) -> None:
        if self._loading or self._config is None:
            return
        before = self._config.horizontal_flip
        if checked == before:
            return
        self._config.horizontal_flip = checked
        self.live_changed.emit()
        self.setting_changed.emit("exports.jpeg_positive.horizontal_flip", before, checked)

    # --- manual settings: live (mid-drag, preview only) + committed (persisted) ---

    def _on_exposure_live(self, value: float) -> None:
        if self._loading or self._config is None:
            return
        self._config.manual_settings.exposure_ev = value
        self.live_changed.emit()

    def _on_exposure_committed(self, value: float) -> None:
        if self._loading or self._config is None:
            return
        self._config.manual_settings.exposure_ev = value
        self.live_changed.emit()
        before = self._committed_exposure_ev
        if value == before:
            return
        self._committed_exposure_ev = value
        self.setting_changed.emit(
            "exports.jpeg_positive.manual_settings.exposure_ev", before, value
        )

    def _on_contrast_live(self, value: float) -> None:
        if self._loading or self._config is None:
            return
        self._config.manual_settings.contrast = int(value)
        self.live_changed.emit()

    def _on_contrast_committed(self, value: float) -> None:
        if self._loading or self._config is None:
            return
        value = int(value)
        self._config.manual_settings.contrast = value
        self.live_changed.emit()
        before = self._committed_contrast
        if value == before:
            return
        self._committed_contrast = value
        self.setting_changed.emit("exports.jpeg_positive.manual_settings.contrast", before, value)

    def _on_shadows_live(self, value: float) -> None:
        if self._loading or self._config is None:
            return
        self._config.manual_settings.shadows = int(value)
        self.live_changed.emit()

    def _on_shadows_committed(self, value: float) -> None:
        if self._loading or self._config is None:
            return
        value = int(value)
        self._config.manual_settings.shadows = value
        self.live_changed.emit()
        before = self._committed_shadows
        if value == before:
            return
        self._committed_shadows = value
        self.setting_changed.emit("exports.jpeg_positive.manual_settings.shadows", before, value)

    def _on_highlights_live(self, value: float) -> None:
        if self._loading or self._config is None:
            return
        self._config.manual_settings.highlights = int(value)
        self.live_changed.emit()

    def _on_highlights_committed(self, value: float) -> None:
        if self._loading or self._config is None:
            return
        value = int(value)
        self._config.manual_settings.highlights = value
        self.live_changed.emit()
        before = self._committed_highlights
        if value == before:
            return
        self._committed_highlights = value
        self.setting_changed.emit("exports.jpeg_positive.manual_settings.highlights", before, value)
