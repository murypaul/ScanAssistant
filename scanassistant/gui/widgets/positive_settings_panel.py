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
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from scanassistant.i18n import t
from scanassistant.project.campaign import JpegPositiveExportConfig


class PositiveSettingsPanel(QWidget):
    """`setting_changed(key, before, after)`: the caller persists + journals it."""

    setting_changed = Signal(str, object, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config: JpegPositiveExportConfig | None = None
        self._loading = False

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(t("wizard.step5.mode_simple"), "simple")
        self.mode_combo.addItem(t("wizard.step5.mode_auto"), "auto")
        self.mode_combo.addItem(t("wizard.step5.mode_manual"), "manual")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self.flip_check = QCheckBox(t("wizard.step5.horizontal_flip"))
        self.flip_check.toggled.connect(self._on_flip_changed)

        self.exposure_spin = QDoubleSpinBox()
        self.exposure_spin.setRange(-3.0, 3.0)
        self.exposure_spin.setSingleStep(0.1)
        self.exposure_spin.editingFinished.connect(self._on_exposure_changed)

        self.contrast_spin = QSpinBox()
        self.contrast_spin.setRange(-100, 100)
        self.contrast_spin.editingFinished.connect(self._on_contrast_changed)

        self.shadows_spin = QSpinBox()
        self.shadows_spin.setRange(0, 100)
        self.shadows_spin.editingFinished.connect(self._on_shadows_changed)

        self.highlights_spin = QSpinBox()
        self.highlights_spin.setRange(0, 100)
        self.highlights_spin.editingFinished.connect(self._on_highlights_changed)

        manual_form = QFormLayout()
        manual_form.addRow(t("wizard.step5.exposure_ev"), self.exposure_spin)
        manual_form.addRow(t("wizard.step5.contrast"), self.contrast_spin)
        manual_form.addRow(t("wizard.step5.shadows"), self.shadows_spin)
        manual_form.addRow(t("wizard.step5.highlights"), self.highlights_spin)
        self.manual_group = QGroupBox(t("wizard.step5.manual_group"))
        self.manual_group.setLayout(manual_form)

        form = QFormLayout()
        form.addRow(t("wizard.step5.mode"), self.mode_combo)
        form.addRow(self.flip_check)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.manual_group)
        layout.addStretch(1)

    def load(self, config: JpegPositiveExportConfig) -> None:
        """(Re)binds the panel to a campaign's live config — safe to call again."""
        self._config = config
        self._loading = True
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(config.mode))
        self.flip_check.setChecked(config.horizontal_flip)
        self.exposure_spin.setValue(config.manual_settings.exposure_ev)
        self.contrast_spin.setValue(config.manual_settings.contrast)
        self.shadows_spin.setValue(config.manual_settings.shadows)
        self.highlights_spin.setValue(config.manual_settings.highlights)
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
        self.setting_changed.emit("exports.jpeg_positive.mode", before, after)

    def _on_flip_changed(self, checked: bool) -> None:
        if self._loading or self._config is None:
            return
        before = self._config.horizontal_flip
        if checked == before:
            return
        self._config.horizontal_flip = checked
        self.setting_changed.emit("exports.jpeg_positive.horizontal_flip", before, checked)

    def _on_exposure_changed(self) -> None:
        if self._loading or self._config is None:
            return
        before = self._config.manual_settings.exposure_ev
        after = self.exposure_spin.value()
        if after == before:
            return
        self._config.manual_settings.exposure_ev = after
        self.setting_changed.emit(
            "exports.jpeg_positive.manual_settings.exposure_ev", before, after
        )

    def _on_contrast_changed(self) -> None:
        if self._loading or self._config is None:
            return
        before = self._config.manual_settings.contrast
        after = self.contrast_spin.value()
        if after == before:
            return
        self._config.manual_settings.contrast = after
        self.setting_changed.emit("exports.jpeg_positive.manual_settings.contrast", before, after)

    def _on_shadows_changed(self) -> None:
        if self._loading or self._config is None:
            return
        before = self._config.manual_settings.shadows
        after = self.shadows_spin.value()
        if after == before:
            return
        self._config.manual_settings.shadows = after
        self.setting_changed.emit("exports.jpeg_positive.manual_settings.shadows", before, after)

    def _on_highlights_changed(self) -> None:
        if self._loading or self._config is None:
            return
        before = self._config.manual_settings.highlights
        after = self.highlights_spin.value()
        if after == before:
            return
        self._config.manual_settings.highlights = after
        self.setting_changed.emit("exports.jpeg_positive.manual_settings.highlights", before, after)
